"""Shared CLI project tools, checked publication, and Claude's SDK MCP bridge.

The CLI has no filesystem write authority. These parent-process tools reuse the
API's existing project tools and ResearchAccess rather than another approval
policy. The bridge never accepts human decisions from the generating process.
"""
from __future__ import annotations

import copy
import base64
import mimetypes
import json
import threading

from backends import Job, attachment_type
from backend_openai import (COMPILE_TOOL, PROPOSE_TOOL, TOPIC_TOOL, READ_TOOLS, WRITE_TOOLS,
                            OpenAICompat)
from research_access import NotationStop

MAX_TEXT = 2 * 1024 * 1024
SERVER = 'prism'
PREFIX = 'mcp__prism__'
WEB_HOOK = 'prism-web-guard'


def user_content(job, prompt):
    """Transport attached project files as data, never as human-authority text."""
    if not job.attachments:
        return prompt
    from guarded_compile import _read_plain
    blocks = [{"type": "text", "text": prompt}]
    for rel in job.attachments:
        raw = _read_plain(job.root, rel, 25 * 1024 * 1024)
        kind = attachment_type(job.root / rel)
        if kind in ("image", "pdf"):
            media = "application/pdf" if kind == "pdf" else mimetypes.guess_type(rel)[0]
            blocks.append({"type": "document" if kind == "pdf" else "image",
                "source": {"type": "base64", "media_type": media,
                           "data": base64.b64encode(raw).decode("ascii")}})
        else:
            blocks.append({"type": "text", "text": "[Attached project file " + rel + "]\n" + raw.decode("utf-8")})
    return blocks


class _CheckedJob:
    def __init__(self, controller):
        self.controller = controller

    def __getattr__(self, key):
        return getattr(self.controller.job, key)

    def emit(self, event):
        self.controller.event(event)

    def tool(self, *args, **kwargs):
        Job.tool(self, *args, **kwargs)

    def tool_result(self, *args, **kwargs):
        Job.tool_result(self, *args, **kwargs)


class Controller:
    def __init__(self, job, *, provider='claude'):
        self.job = job
        job.controlled_cli = True
        self.provider = provider
        self.label = {'codex': 'Codex', 'deepcode': 'Deep Code'}.get(provider, 'Claude Code')
        self.proxy = _CheckedJob(self)
        self.lock = threading.RLock()
        self.halted = False
        self.outcome = {}
        self.text, self.thinking = '', ''
        self.seen = set()
        self.denials = []
        # Only its local file-tool evaluator is used. No API run/request is made.
        self.tools = OpenAICompat(provider + '-project-tools', {'base_url': 'http://unused.invalid'})
        self.job.research_access.proposal_channel = provider + '_proposal'

    def definitions(self, *, all_tools=False):
        functions = [*READ_TOOLS, PROPOSE_TOOL]
        if self.job.research_access.initialized:
            functions.append(TOPIC_TOOL)
        if all_tools or self.job.mode == 'edit':
            functions += WRITE_TOOLS
            if all_tools or self.job.may_compile:
                functions.append(COMPILE_TOOL)
        result = [{'name': x['function']['name'], 'description': x['function']['description'],
                   'inputSchema': x['function']['parameters']} for x in functions]
        result += [
            {'name': 'get_research', 'description': 'Read current project research knowledge and human decisions.',
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
            {'name': 'propose_notations', 'description': 'Save a batch of separate pending notation proposals and end this Turn. This grants no adoption.',
             'inputSchema': {'type': 'object', 'properties': {'proposals': {'type': 'array', 'minItems': 1,
                'maxItems': 100, 'items': PROPOSE_TOOL['function']['parameters']}}, 'required': ['proposals'],
                'additionalProperties': False}},
        ]
        return result

    def names(self):
        return {x['name'] for x in self.definitions()}

    def halt(self, stop):
        if self.halted:
            return
        self.halted = True
        self.text = self.thinking = ''
        OpenAICompat._notation_stop(self.job, self.outcome, stop)
        if self.outcome.get('notation_pending'):
            self.outcome.update(exit=0, is_error=False, subtype='notation pending')

    def answer(self, message):
        """Answer one SDK-MCP JSON-RPC message; it has no human-authority route."""
        with self.lock:
            if not isinstance(message, dict):
                return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid MCP message'}}
            mid, method = message.get('id'), message.get('method')
            params = message.get('params') or {}
            if not isinstance(params, dict):
                return {'jsonrpc':'2.0','id':mid,'error':{'code':-32602,'message':'Invalid MCP parameters'}}
            if mid is None:
                return None
            if method == 'initialize':
                result = {'protocolVersion': params.get('protocolVersion') or '2025-06-18',
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': SERVER, 'version': '1'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': self.definitions()}
            elif method == 'tools/call':
                name, arguments = params.get('name'), params.get('arguments', {})
                text, failed = 'Not run: notation review ended this Turn.', True
                if not self.halted and not self.job.cancel.is_set():
                    if not isinstance(name, str) or name not in self.names() or not isinstance(arguments, dict):
                        text = 'Unknown or malformed protected project tool.'
                    else:
                        try:
                            if name == 'get_research':
                                text = json.dumps(self.job.research_access.knowledge.status(), ensure_ascii=False)
                            elif name == 'propose_notations':
                                proposals = self.job.research_access.propose_many(arguments.get('proposals'))
                                raise NotationStop(proposals)
                            else:
                                text = self.tools._call(self.job, {'id': f'{self.provider}-mcp-{mid}', 'name': name,
                                    'arguments': json.dumps(arguments)}, self.denials)
                            failed = text.startswith('Error:')
                        except NotationStop as stop:
                            self.halt(stop)
                            text = 'Notation review failed; the original expression was kept.' if stop.error else 'Notation proposals saved. This Turn is complete; await a later human reply.'
                            failed = bool(stop.error)
                        except (OSError, ValueError, TypeError) as error:
                            text, failed = 'Protected project tool failed: ' + type(error).__name__, True
                result = {'content': [{'type': 'text', 'text': text}], 'isError': failed}
            else:
                return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'Unknown MCP method'}}
            return {'jsonrpc': '2.0', 'id': mid, 'result': result}

    def _publish(self, text):
        if not text or self.halted or self.job.cancel.is_set():
            return
        if len(text) > MAX_TEXT:
            raise NotationStop(error='The protected reply exceeded the checking limit; no unchecked text was published.')
        checked = self.job.research_access.review_output(text, channel='chat')
        if checked not in self.seen:
            self.seen.add(checked)
            self.job.emit({'t': 'text', 'text': checked})

    def flush(self):
        if self.text:
            text, self.text = self.text, ''
            self._publish(text)
        if self.thinking and not self.halted and not self.job.cancel.is_set():
            thought, self.thinking = self.thinking, ''
            checked = self.job.research_access.review_auxiliary(thought, channel='thinking')
            self.job.emit({'t': 'thinking', 'text': checked})

    def event(self, event):
        """All legacy event emitters pass here before a protected UI sees content."""
        if self.halted or self.job.cancel.is_set():
            return
        kind = event.get('t')
        if kind in ('delta', 'thinking'):
            field = 'text' if kind == 'delta' else 'thinking'
            value = getattr(self, field) + str(event.get('text') or '')
            if len(value) > MAX_TEXT:
                raise NotationStop(error='The protected reply exceeded the checking limit; no unchecked text was published.')
            setattr(self, field, value)
            return
        if kind == 'tool_live':
            return   # native write drafts never become visible mathematical text
        if kind == 'text':
            self.text = ''
            self._publish(str(event.get('text') or ''))
            return
        if kind == 'message_start':
            self.flush()
        event = dict(event)
        for field in ('name', 'summary', 'preview', 'path', 'message'):
            if isinstance(event.get(field), str):
                event[field] = self.job.research_access.review_auxiliary(event[field], channel='tool')
        self.job.emit(event)

    def handle(self, frame, backend, state):
        """Buffer fragmented text, then reuse the native adapter's metadata parser."""
        with self.lock:
            if self.job.cancel.is_set():
                self.text = self.thinking = ''
                return
            kind = frame.get('type')
            if self.halted:
                if kind == 'result':
                    # Preserve billing/session metadata, not the interrupted draft.
                    backend.handle(frame, self.proxy, state)
                return
            try:
                if kind == 'assistant' and frame.get('parent_tool_use_id') is None:
                    message = frame.get('message') or {}
                    content = message.get('content') or []
                    if not isinstance(content, list):
                        raise TypeError('Invalid assistant content')
                    text = ''.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
                    if text:
                        self.text = ''
                        self._publish(text)
                    else:
                        self.flush()
                    frame = copy.deepcopy(frame)
                    frame['message']['content'] = [b for b in content if not isinstance(b, dict) or b.get('type') != 'text']
                if kind == 'result':
                    self.flush()
                    if not frame.get('is_error'):
                        self._publish(str(frame.get('result') or ''))
                backend.handle(frame, self.proxy, state)
            except NotationStop as stop:
                self.halt(stop)
            except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
                self.halt(NotationStop(error='Protected output could not be checked: ' + type(error).__name__))

    def public_input(self, value, depth=0):
        """A checked display copy; the broker keeps the untouched Web request."""
        if depth > 12:
            return "Preview nesting limit reached."
        if isinstance(value, str):
            return self.job.research_access.review_auxiliary(value, channel="tool")
        if isinstance(value, dict):
            return {self.public_input(str(k), depth + 1): self.public_input(v, depth + 1)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self.public_input(v, depth + 1) for v in value]
        return value

    def sanitize_result(self, state, stderr):
        for field in ('error', 'subtype'):
            if state.get(field):
                state[field] = OpenAICompat._diagnostic(self.job, str(state[field]), self.label + ' failed')
        for denied in state.get('denied', []):
            for field in ('tool', 'what'):
                if isinstance(denied.get(field), str):
                    denied[field] = self.job.research_access.review_auxiliary(denied[field], channel='error')
        if stderr:
            # stderr is an arbitrary cropped byte tail, with no source context.
            state['stderr'] = self.label + ' diagnostic output was withheld because it has no complete checking context.'
        state['denials'] = list(dict.fromkeys(self.job.research_access.review_auxiliary(str(name), channel='error')
            for name in [*state.get('denials', []), *self.denials]))
        if self.halted:
            state.update(self.outcome)
