"""Which browser pages are open, for `--exit-when-idle`.

The pages of one server (editor, PDF, Home) keep one event stream open to it
(`hold` / `serve_stream`): one page holds it, and when that page closes the
next takes it over (common.js; a browser keeps at most six connections to one
server, so not one per page). When the last page, its tab or the whole browser
is closed, the connection drops and the server knows at once. Every page also
sends a heartbeat with a random page id and says goodbye
(navigator.sendBeacon) when it is closed; that is how the pages without the
stream are counted. The server is idle when no page is left:

- before the first page has connected, after `first_wait` seconds
  (a cold browser start can be slow);
- after the last page said goodbye or went silent, after `grace` seconds
  (a reload says goodbye and then connects again right away);
- a page without an open stream that sends no heartbeat for `stale` seconds
  counts as closed. Browsers throttle timers in hidden tabs to about once a
  minute, so this must be well above 60 seconds. Open streams are not
  throttled, which is why they can report a closed page right away.
"""
from __future__ import annotations

import select
import threading
import time
from typing import Callable


class Presence:
    def __init__(self, first_wait: float = 60.0, grace: float = 10.0, stale: float = 120.0,
                 clock: Callable[[], float] = time.monotonic):
        self.first_wait, self.grace, self.stale, self.clock = first_wait, grace, stale, clock
        self.lock = threading.Lock()
        self.clients: dict[str, float] = {}     # page id -> last heartbeat
        self.held: dict[str, int] = {}          # page id -> open streams
        self.seen_any = False
        self.empty_since: float | None = clock()

    def beat(self, cid: str) -> None:
        with self.lock:
            self.clients[cid] = self.clock()
            self.seen_any = True
            self.empty_since = None

    def bye(self, cid: str) -> bool:
        """Forget a page. Only ids that sent a heartbeat are accepted."""
        with self.lock:
            return self._forget(cid)

    def _forget(self, cid: str) -> bool:
        if cid not in self.clients:
            return False
        del self.clients[cid]
        self.held.pop(cid, None)
        if not self.clients:
            self.empty_since = self.clock()
        return True

    def hold(self, cid: str) -> None:
        """A stream from page `cid` opened: it stays present while the stream is open."""
        with self.lock:
            self.held[cid] = self.held.get(cid, 0) + 1
            self.clients[cid] = self.clock()
            self.seen_any = True
            self.empty_since = None

    def release(self, cid: str) -> None:
        """The stream closed: the page is gone (a reload connects again with a new id)."""
        with self.lock:
            n = self.held.get(cid, 0) - 1
            if n > 0:
                self.held[cid] = n
            else:
                self._forget(cid)

    def _prune(self, now: float) -> None:
        dead = [c for c, t in self.clients.items()
                if now - t > self.stale and not self.held.get(c)]
        for c in dead:
            del self.clients[c]
        if dead and not self.clients:
            self.empty_since = now

    def count(self) -> int:
        with self.lock:
            self._prune(self.clock())
            return len(self.clients)

    def idle(self) -> bool:
        with self.lock:
            now = self.clock()
            self._prune(now)
            if self.clients:
                return False
            wait = self.grace if self.seen_any else self.first_wait
            return now - (self.empty_since if self.empty_since is not None else now) >= wait

    def resume(self) -> None:
        """The machine was asleep: give every page a fresh window before judging it."""
        with self.lock:
            now = self.clock()
            self.clients = {c: now for c in self.clients}
            if self.empty_since is not None:
                self.empty_since = now


def valid_id(cid) -> bool:
    return isinstance(cid, str) and 8 <= len(cid) <= 100


def serve_stream(handler, presence: Presence, cid: str, interval: float = 15.0) -> None:
    """Answer GET /api/presence/stream: an event stream that keeps page `cid` present
    until the browser closes the connection. `handler` is a BaseHTTPRequestHandler."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.close_connection = True
    sock = handler.connection
    presence.hold(cid)
    try:
        handler.wfile.write(b"retry: 3000\n\n")
        handler.wfile.flush()
        while True:
            # A closed connection shows up as readable with no data.
            readable, _, _ = select.select([sock], [], [], interval)
            if readable and not sock.recv(64):
                return
            handler.wfile.write(b": ping\n\n")         # also notices a dead connection
            handler.wfile.flush()
    except (OSError, ValueError):
        return
    finally:
        presence.release(cid)
