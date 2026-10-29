"""Legacy Codex MCP stdio transport for the editor's checked parent tools.

The child has no file or decision authority. The parent keeps already-open FIFO
handles; replacing their paths cannot redirect a trusted write to another file.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import stat
import sys
import tempfile
import threading
import time
import uuid

MAX_FRAME = 8 * 1024 * 1024
IO_TIMEOUT = 30
TOOL_TIMEOUT = 1200
MAX_REQUESTS = 4096


class TransportError(Exception):
    pass


def _open_fifo(path, mode=os.O_RDWR):
    descriptor = os.open(path, mode | os.O_NONBLOCK | os.O_NOFOLLOW)
    if not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise TransportError('The protected MCP transport is not a FIFO.')
    return descriptor


def _write(descriptor, message, stopped):
    data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode() + b'\n'
    if len(data) > MAX_FRAME:
        raise TransportError('The protected MCP response exceeded its size limit.')
    deadline = time.monotonic() + IO_TIMEOUT
    offset = 0
    while offset < len(data):
        if stopped.is_set():
            raise TransportError('The protected MCP transport stopped.')
        if time.monotonic() >= deadline:
            raise TransportError('The protected MCP transport timed out.')
        if not select.select([], [descriptor], [], .05)[1]:
            continue
        try:
            offset += os.write(descriptor, data[offset:offset + 65536])
        except BlockingIOError:
            continue


class _Reader:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.pending = bytearray()
        self.started = None

    def read(self, stopped, *, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while not stopped.is_set():
            end = self.pending.find(b'\n')
            if end >= 0:
                line = bytes(self.pending[:end])
                del self.pending[:end + 1]
                self.started = time.monotonic() if self.pending else None
                try:
                    return json.loads(line)
                except (ValueError, UnicodeError):
                    raise TransportError('Invalid protected MCP JSON.') from None
            now = time.monotonic()
            if (deadline is not None and now >= deadline) or (self.started is not None and now - self.started >= IO_TIMEOUT):
                raise TransportError('The protected MCP transport timed out.')
            if not select.select([self.descriptor], [], [], .05)[0]:
                continue
            try:
                data = os.read(self.descriptor, 65536)
            except BlockingIOError:
                continue
            if not data:
                raise TransportError('The protected MCP transport ended.')
            if self.started is None:
                self.started = now
            self.pending.extend(data)
            if len(self.pending) > MAX_FRAME:
                raise TransportError('The protected MCP request exceeded its size limit.')
        raise TransportError('The protected MCP transport stopped.')


def _valid_id(value):
    return type(value) is int or isinstance(value, str) and 0 < len(value) <= 128


class Relay:
    def __init__(self, guard, scratch):
        self.guard = guard
        self.active = threading.Event()
        self.stopped = threading.Event()
        self.busy = threading.Event()
        self.error = None
        self.forced_cancel = False
        self.name = 'prism_' + uuid.uuid4().hex[:16]
        directory = Path(tempfile.mkdtemp(prefix='mcp-', dir=scratch))
        directory.chmod(0o700)
        self.request_pipe, self.response_pipe = directory/'request', directory/'response'
        self._descriptors = []
        try:
            for path in (self.request_pipe, self.response_pipe):
                os.mkfifo(path, 0o600)
                self._descriptors.append(_open_fifo(path))
        except BaseException:
            for descriptor in self._descriptors:
                os.close(descriptor)
            raise
        self.config = {'command': sys.executable, 'args': [str(Path(__file__).resolve()),
            '--request-pipe', str(self.request_pipe), '--response-pipe', str(self.response_pipe)],
            'enabled': True, 'required': True, 'default_tools_approval_mode': 'approve',
            'tool_timeout_sec': TOOL_TIMEOUT}
        self.thread = threading.Thread(target=self._serve, name='codex-checked-mcp', daemon=True)
        self.thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.active.clear()
        self.stopped.set()
        self.thread.join(.1)
        if self.thread.is_alive() and self.busy.is_set():
            self.error = 'The protected MCP Turn ended with an unfinished operation.'
            if not self.guard.job.cancel.is_set():
                self.forced_cancel = True
                self.guard.job.cancel.set()
            self.thread.join(1)
        if self.thread.is_alive():
            self.error = 'The protected MCP transport did not stop in time.'

    def _serve(self):
        reader = _Reader(self._descriptors[0])
        seen = set()
        try:
            while not self.stopped.is_set() and not self.guard.job.cancel.is_set():
                message = reader.read(self.stopped)
                mid = message.get('id') if isinstance(message, dict) else None
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not _valid_id(mid) or not isinstance(message.get('method'), str):
                    raise TransportError('Invalid protected MCP request.')
                if mid in seen or len(seen) >= MAX_REQUESTS:
                    raise TransportError('Repeated or excessive protected MCP request identifiers.')
                seen.add(mid)
                self.busy.set()
                try:
                    if message['method'] == 'tools/call' and not self.active.is_set():
                        answer = {'jsonrpc':'2.0','id':mid,'result':{'content':[{'type':'text',
                            'text':'The current protected Turn is not active.'}], 'isError':True}}
                    else:
                        answer = self.guard.answer(message)
                    if answer is not None:
                        _write(self._descriptors[1], answer, self.stopped)
                finally:
                    self.busy.clear()
        except (OSError, ValueError, TypeError, TransportError):
            if not self.stopped.is_set() and not self.guard.job.cancel.is_set():
                self.error = 'The protected MCP transport failed.'
        finally:
            self.stopped.set()
            for descriptor in self._descriptors:
                os.close(descriptor)


def serve(request_pipe, response_pipe):
    request = response = None
    stopped = threading.Event()
    try:
        request = _open_fifo(request_pipe, os.O_WRONLY)
        response = _open_fifo(response_pipe, os.O_RDONLY)
        reader = _Reader(response)
        for line in iter(lambda: sys.stdin.buffer.readline(MAX_FRAME + 1), b''):
            if len(line) > MAX_FRAME or not line.endswith(b'\n'):
                raise TransportError('Invalid protected MCP frame.')
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                raise TransportError('Invalid protected MCP JSON.') from None
            if not isinstance(message, dict):
                raise TransportError('Invalid protected MCP request.')
            if message.get('id') is None:
                continue
            if not _valid_id(message['id']):
                raise TransportError('Invalid protected MCP request identifier.')
            _write(request, message, stopped)
            timeout = TOOL_TIMEOUT if message.get('method') == 'tools/call' else IO_TIMEOUT
            answer = reader.read(stopped, timeout=timeout)
            if not isinstance(answer, dict) or answer.get('jsonrpc') != '2.0' or answer.get('id') != message['id']:
                raise TransportError('The protected MCP response did not match its request.')
            sys.stdout.write(json.dumps(answer, ensure_ascii=False) + '\n')
            sys.stdout.flush()
        return 0
    except (OSError, ValueError, TypeError, TransportError):
        # Child diagnostics do not reproduce arbitrary frames or source material.
        print('The protected MCP transport failed.', file=sys.stderr)
        return 1
    finally:
        for descriptor in (request, response):
            if descriptor is not None:
                os.close(descriptor)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--request-pipe', required=True)
    parser.add_argument('--response-pipe', required=True)
    args = parser.parse_args()
    raise SystemExit(serve(args.request_pipe, args.response_pipe))
