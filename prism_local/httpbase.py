"""What the editor server (server.py) and the Home page server (hub.py) share.

Every request passes the same checks:

- the Host header must be 127.0.0.1 or localhost (no DNS rebinding);
- a request that another website makes the browser send (an <img> or <script> tag, a
  form, a fetch) is refused for everything except the pages themselves and their static
  files, so no other site can start builds or programs, or read project data;
- every state-changing request (POST) needs the header X-Prism-Local: 1, which browsers
  send cross-origin only after a CORS preflight that is never answered. The one
  exception is the goodbye of a closing page (navigator.sendBeacon), which can only
  remove a page id that has sent a heartbeat.

Also here: page presence (presence.py), JSON and static answers, listening on a port,
and stopping once no page is open.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from presence import Presence, serve_stream, valid_id

STATIC = Path(__file__).resolve().parent / "static"
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".pdf": "application/pdf",
        ".svg": "image/svg+xml", ".png": "image/png"}


class Server(ThreadingHTTPServer):
    # http.server sets SO_REUSEADDR, which on Windows lets a second server bind a port
    # that is already in use. Ask for exclusive use there instead.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # A page that goes away in the middle of an answer (a reload, a closed tab) is
        # not worth a traceback in the log.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    """Base of both servers. A subclass sets `presence` and `pages` ({path: static file})
    and implements get(path, query) and post(path, body)."""

    server_version = "prism-local/1"
    presence: Presence
    pages: dict[str, str] = {}

    def log_message(self, fmt, *args):  # quiet
        pass

    # ------------------------------------------------------------ checks
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc == self.headers.get("Host")

    def _cross_site(self) -> bool:
        """Sent by another website (all current browsers say so in Sec-Fetch-Site)."""
        site = self.headers.get("Sec-Fetch-Site")
        return (site is not None and site not in ("same-origin", "none")) or not self._same_origin()

    # ------------------------------------------------------------ answers
    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")          # no framing by other sites
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _static(self, rel):
        p = (STATIC / rel).resolve()
        if STATIC not in p.parents or not p.is_file():
            return self._err(404, "not found")
        self._send(200, p.read_bytes(), MIME.get(p.suffix, "application/octet-stream"))

    # ------------------------------------------------------------ requests
    def do_GET(self):
        if not self._host_ok():
            return self._err(403, "bad host")
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in self.pages:
            return self._static(self.pages[u.path])
        if u.path.startswith("/static/"):
            return self._static(u.path[len("/static/"):])
        if self._cross_site():
            return self._err(403, "forbidden")
        if u.path == "/api/presence/stream":           # only this app's own pages hold it open
            if not valid_id(q.get("client")):
                return self._err(400, "bad client id")
            return serve_stream(self, self.presence, q["client"])
        return self.get(u.path, q)

    def do_POST(self):
        u = urlparse(self.path)
        # A closing page says goodbye with navigator.sendBeacon, which cannot set the
        # custom header below. It only removes a page id that sent a heartbeat, and
        # other sites cannot know those random ids.
        if u.path == "/api/bye":
            if not self._host_ok() or not self._same_origin():
                return self._err(403, "forbidden")
            n = max(0, min(int(self.headers.get("Content-Length") or 0), 200))
            cid = self.rfile.read(n).decode("utf-8", "replace").strip()
            return self._json({"ok": self.presence.bye(cid)})
        if not self._host_ok() or self.headers.get("X-Prism-Local") != "1":
            return self._err(403, "forbidden")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("the request body must be a JSON object")
        except ValueError as e:
            return self._err(400, str(e))
        if u.path == "/api/presence":
            if not valid_id(body.get("client")):
                return self._err(400, "bad client id")
            self.presence.beat(body["client"])
            return self._json({"ok": True})
        return self.post(u.path, body)

    def get(self, path: str, q: dict):
        return self._err(404, "not found")

    def post(self, path: str, body: dict):
        return self._err(404, "not found")


# ---------------------------------------------------------------- running a server

def quiet_stdio() -> None:
    """Started without a console (pythonw), there is no stdout to print to."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = sys.stdout


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def listen(handler, port: int, tries: int) -> Server:
    """A server on 127.0.0.1:port, or on one of the next tries-1 ports (0: any free port)."""
    err = None
    for p in range(port, port + max(1, tries)) if port else [0]:
        try:
            return Server(("127.0.0.1", p), handler)
        except OSError as e:
            err = e
    raise OSError(f"cannot listen on 127.0.0.1:{port}: {err}")


RESUME_GAP = 20.0      # seconds between watchdog ticks that mean the machine slept
BUSY_CAP = 600.0       # longest wait for running work (a build, an agent turn) before exiting


def idle_watchdog(srv: Server, presence: Presence, busy=lambda: False, what="work") -> None:
    """Shut the server down once no page has been open for a while (see presence.py).
    Work that `busy()` reports may finish first, for at most BUSY_CAP seconds."""
    last_m, last_w, busy_since = time.monotonic(), time.time(), None
    while True:
        time.sleep(1.0)
        m, w = time.monotonic(), time.time()
        # Some systems stop the monotonic clock during sleep, others do not: watch both.
        if m - last_m > RESUME_GAP or w - last_w > RESUME_GAP:
            log("resumed after sleep; waiting for pages to check in again")
            presence.resume()
        last_m, last_w = m, w
        if not presence.idle():
            busy_since = None
            continue
        if busy():
            if busy_since is None:
                busy_since = m
                log(f"no open pages; waiting for the running {what}")
            if m - busy_since < BUSY_CAP:
                continue
        log("no open pages; shutting down")
        srv.shutdown()
        return


def write_ready_file(path: Path, info: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(info), encoding="utf-8")
    os.replace(tmp, path)


def remove_ready_file(path: Path) -> None:
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass
