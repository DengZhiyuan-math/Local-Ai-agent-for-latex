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

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import traceback
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from errors import Conflict, LockBusy, NotInstalled, Params, Refused, say
from presence import Presence, serve_stream, valid_id

STATIC = Path(__file__).resolve().parent / "static"
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".pdf": "application/pdf",
        ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2"}


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
        e = sys.exc_info()[1]
        if isinstance(e, (ConnectionError, TimeoutError)) and not isinstance(e, LockBusy):
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
        self._answered = True
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

    def _err(self, code, msg, details=None):
        self._json({**(details or {}), "error": msg}, code)

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
        return self._answer(self.get, u.path, q)

    def do_POST(self):
        u = urlparse(self.path)
        # A closing page says goodbye with navigator.sendBeacon, which cannot set the
        # custom header below. It only removes a page id that sent a heartbeat, and
        # other sites cannot know those random ids.
        if u.path == "/api/bye":
            if not self._host_ok() or not self._same_origin():
                return self._err(403, "forbidden")
            try:
                n = max(0, min(int(self.headers.get("Content-Length") or 0), 200))
            except ValueError:
                return self._err(400, "bad request")
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
        return self._answer(self.post, u.path, body)

    def _answer(self, handle, path: str, arg):
        """handle(path, Params(arg)). Whatever it raises that it did not answer itself
        becomes a JSON error the page can show, instead of a connection dropped without an
        answer, unless part of an answer has already gone out. The one place where errors
        become statuses: a Refused says its own (not logged), a program that is not
        installed (NotInstalled) is a 424 that says where to get it; anything not expected
        (a bug) is a 500 with its traceback in the log."""
        self._answered = False
        try:
            return handle(path, Params(arg))
        except Exception as e:
            if isinstance(e, LockBusy) and not self._answered:
                return self._err(e.status, str(e))      # another window holds a lock: say so
            if self._answered or isinstance(e, (ConnectionError, TimeoutError)):
                raise                       # the page went away, or the answer was started
            if isinstance(e, Refused):
                return self._err(e.status, str(e), e.details)
            if isinstance(e, subprocess.TimeoutExpired):       # in the words the sync uses too
                said = say(e)
                return self._err(504, said if said.startswith("GitHub") else said + "; try again")
            if isinstance(e, UnicodeDecodeError):
                return self._err(415, "This file is not UTF-8 text; prism-local edits UTF-8 files only.")
            if isinstance(e, NotInstalled):     # a FileNotFoundError too: say which program, not 404
                return self._err(424, str(e))
            if isinstance(e, FileNotFoundError):
                return self._err(404, "file not found")
            if isinstance(e, ValueError):
                # Most are a request that cannot be done (a bad path, a bad name); a line
                # in the log tells them apart from a bug until each says so (Refused).
                log(f"refused {self.command} {path}: {type(e).__name__}: {e}")
                return self._err(400, str(e))
            log(f"error in {self.command} {path}:\n" + traceback.format_exc().rstrip())
            return self._err(500, f"{type(e).__name__}: {e}")

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
    # A console or pipe that cannot show a character (a Chinese folder name in cp1252) shows
    # "?": printing a path must never stop a server.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def log(msg: str) -> None:
    """A line in the server's output. Never fails: a console that cannot show a character
    (a Chinese folder name in a cp1252 console) gets "?" instead."""
    line = time.strftime("%H:%M:%S ") + msg
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "replace").decode(enc), flush=True)
    except (OSError, ValueError):       # no console at all
        pass


def stop_on_signals(srv: Server) -> None:
    """Let SIGTERM (kill, a logout, systemd) and SIGHUP (a closed terminal) end the server
    the way the idle watchdog does, so its cleanup runs: the instance file goes and
    running work is stopped. (Called from the main thread, before serve_forever.)"""
    import signal

    def handler(signum, frame):
        threading.Thread(target=srv.shutdown, daemon=True).start()   # not from serve_forever's thread

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


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


CODE_DIR = Path(__file__).resolve().parent
# Every open page asks every two seconds; the code changes once per update.
CODE_STAMP_EVERY = 5.0
_CODE_STAMP: dict = {"at": None, "stamp": 0.0}


def code_stamp() -> float:
    """When prism-local's code on disk last changed. A server keeps running the code it
    started with; once this is newer (selfupdate.py), its page offers a restart. Looked
    at again at most every CODE_STAMP_EVERY seconds."""
    now = time.monotonic()
    if _CODE_STAMP["at"] is not None and now - _CODE_STAMP["at"] < CODE_STAMP_EVERY:
        return _CODE_STAMP["stamp"]
    try:
        stamp = max(p.stat().st_mtime for p in (*CODE_DIR.glob("*.py"), *(CODE_DIR / "static").glob("*.*")))
    except (OSError, ValueError):
        stamp = 0.0
    _CODE_STAMP.update(at=now, stamp=stamp)
    return stamp


def own_argv() -> list[str]:
    """This process's command line."""
    return list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])


# ---------------------------------------------------------------- one server's life

class App:
    """What the editor's server and the Home page's share around their routes: the
    command line they have in common, the instance (instances.serving: one per project,
    one Home), the page's check that it talks to this server (/api/ping), the restart
    once prism-local is updated (the successor gets the port and the pages reload), the
    exit when no page is left, and the end, where what is running is stopped. Each keeps
    its own routes and what it starts and stops."""

    def __init__(self, name: str, presence: Presence):
        self.name = name                     # in /api/ping, and before its messages
        self.presence = presence
        self.started_code = code_stamp()     # a server keeps running the code it started with
        self.life = None                     # instances.Serving, while it serves
        self.server: Server | None = None
        self.args = None
        self.again = False                   # start the successor once serving ends

    @staticmethod
    def arguments(ap, port: int, pages: str) -> None:
        """The command-line options both servers take."""
        ap.add_argument("--port", type=int, default=port, help="port (0: any free port)")
        ap.add_argument("--port-tries", type=int, default=1,
                        help="if the port is taken, try this many ports upwards")
        ap.add_argument("--no-browser", action="store_true")
        ap.add_argument("--exit-when-idle", action="store_true",
                        help=f"exit shortly after the last {pages} is closed")
        ap.add_argument("--ready-file", type=Path,
                        help="once listening, write {pid, port, url, root} as JSON to this file")
        ap.add_argument("--idle-timings", help=argparse.SUPPRESS)   # "first,grace,stale" for tests
        ap.add_argument("--instance-token", help=argparse.SUPPRESS)

    def run(self, a, project, serve):
        """serve(life) as the instance of `project` (None: the Home page); a server already
        there, or one starting, ends this one with its message."""
        import instances
        self.args = a
        try:
            with instances.serving(project, a.ready_file, a.instance_token) as life:
                self.life = life
                return serve(life)
        except (instances.Busy, TimeoutError) as exc:
            sys.exit(f"{self.name}: {exc}")

    def listen(self, handler) -> Server:
        """The server, on the port the command line asked for (the idle timings set)."""
        a = self.args
        if a.idle_timings:
            p = self.presence
            p.first_wait, p.grace, p.stale = (float(x) for x in a.idle_timings.split(","))
        try:
            self.server = listen(handler, a.port, a.port_tries)
        except OSError as e:
            sys.exit(f"{self.name}: {e}")
        return self.server

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    def ping(self, **more) -> dict:
        """/api/ping: whose server this is (a page and instances.inspect check it)."""
        return {"app": self.name, **more, "pid": os.getpid(), "pages": self.presence.count(),
                "token": self.life.token if self.life else None}

    def updated(self) -> bool:
        """prism-local's code changed since this server started: its page offers a restart."""
        return code_stamp() > self.started_code + 1

    def restart(self, to: Path | None = None):
        """Stop serving, and start the successor on the same port (at `to`, a renamed
        project's folder). Its transition is begun now: Conflict when another one is (a
        restart, Home moving the project). Returns it."""
        import instances
        if self.server is None or self.life is None:
            raise Conflict("not ready to restart")
        try:
            transition = self.life.transition(*([to] if to is not None else []))
        except (instances.Busy, TimeoutError, OSError) as exc:
            raise Conflict(str(exc)) from exc
        return transition

    def stop_for_restart(self) -> None:
        self.again = True
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def serve(self, *, busy=lambda: False, what="work", started=lambda: None, stopping=lambda: None,
              successor=lambda: (own_argv(), os.getcwd())) -> None:
        """Serve until the last page has gone (--exit-when-idle), a signal or a restart:
        published as this instance, the page opened (unless --no-browser), prism-local
        updated in the background, `started()` once listening. At the end `stopping()`
        stops what runs, and a restart hands the instance to successor(): (its command
        line, its folder)."""
        import selfupdate
        a, srv, life = self.args, self.server, self.life
        life.publish(self.url, srv.server_address[1])
        if not a.no_browser:
            url = self.url
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        if a.exit_when_idle:
            threading.Thread(target=idle_watchdog, daemon=True, args=(srv, self.presence, busy, what)).start()
        stop_on_signals(srv)
        started()
        threading.Thread(target=selfupdate.update, daemon=True).start()   # the page then offers a restart
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            transition = life.pending or life.transition()
            try:
                srv.server_close()
                life.unpublish()
                stopping()
                if self.again:
                    argv, cwd = successor()
                    result = transition.handover(argv, cwd)
                    log("restart failed: " + result["error"] if "error" in result
                        else "restart handed to the successor")
                else:
                    log("stopped")
            finally:
                transition.close()

    def successor_argv(self, script: Path, *head: str) -> list[str]:
        """This server's command line for the successor: `script` with `head` (a project),
        on the port it has now, with the options it was started with."""
        a = self.args
        argv = [sys.executable, *(["-u"] if "-u" in (getattr(sys, "orig_argv", None) or []) else []),
                str(script), *head, "--port", str(self.server.server_address[1]), "--port-tries", "1", "--no-browser"]
        if a.exit_when_idle:
            argv.append("--exit-when-idle")
        if a.idle_timings:
            argv += ["--idle-timings", a.idle_timings]
        return argv
