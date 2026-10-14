"""Server instances: inspection, startup, restart handover and exclusive folder changes.

inspect() is read-only. ensure() returns the launcher's existing JSON result. Serving
registers a server and coordinates its shutdown/replacement. exclusive() prevents Open
from starting an editor while Home moves a project. All coordination uses the same
per-project OS locks; operation records say why a project is busy, not who owns a port.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import registry
from gitenv import git_env
from proc import kill_tree

# Loopback HTTP must bypass system proxies.
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def ping(url: str, app: str = "prism-local", root: Path | None = None,
         timeout: float = 2.0) -> dict | None:
    """The /api/ping answer of the server at `url`, if it is `app` (serving `root`)."""
    return _probe(url, app, root, timeout)[0]


def _probe(url: str, app: str, root: Path | None, timeout: float) -> tuple[dict | None, bool]:
    """ping(), and whether nothing listens at `url` at all (the connection refused)."""
    try:
        with HTTP.open(url + "api/ping", timeout=timeout) as r:
            info = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        if hasattr(e, "close"):                     # an HTTP error answer: its connection goes
            e.close()
        return None, isinstance(getattr(e, "reason", e), ConnectionRefusedError)
    if not isinstance(info, dict) or info.get("app") != app:
        return None, False
    if root is not None and registry.norm(info.get("root", "")) != registry.norm(root):
        return None, False
    return info, False


def _kernel32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD))
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return k32


def _created(k32, h) -> int | None:
    import ctypes
    from ctypes import wintypes
    times = [wintypes.FILETIME() for _ in range(4)]    # creation, exit, kernel, user
    if not k32.GetProcessTimes(h, *map(ctypes.byref, times)):
        return None
    return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime


def process_started(pid) -> int | None:
    """When process `pid` started, as a number to compare (not a time to show), or None
    where the system does not say. The instance file records it: once a server has ended,
    Windows may give its pid to a new process, which started later."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        k32 = _kernel32()
        h = k32.OpenProcess(0x1000, False, pid)                 # query limited info
        if not h:
            return None
        try:
            return _created(k32, h)
        finally:
            k32.CloseHandle(h)
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes()
        return int(stat[stat.rindex(b")") + 2:].split()[19])   # field 22: starttime
    except (OSError, ValueError, IndexError):
        return None                     # no /proc (macOS)


def server_alive(pid, started: int | None = None) -> bool:
    """Whether `pid` is a running Python process (a prism-local server), not a process
    that got the number of a server that ended long ago. With `started` (the instance
    file's, see process_started) the process must also have started then; instance files
    written before that was recorded are judged by the program's name alone."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = _kernel32()
        h = k32.OpenProcess(0x1000 | 0x00100000, False, pid)  # query limited info | synchronize
        if not h:
            # Access denied: it exists, but a server of yours can always be looked at.
            return ctypes.get_last_error() == 5 and started is None
        try:
            if k32.WaitForSingleObject(h, 0) != 0x102:         # not WAIT_TIMEOUT: it has ended
                return False
            now = _created(k32, h) if started is not None else None
            if now is not None:
                return now == started
            buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return "python" in Path(buf.value).name.lower()
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        other_user = False
    except ProcessLookupError:
        return False
    except PermissionError:
        other_user = True
    now = process_started(pid) if started is not None else None
    if now is not None:
        return now == started
    if other_user:
        return True
    try:
        return "python" in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode(
            "utf-8", "replace").lower()
    except OSError:
        return True                     # no /proc (macOS): trust the process table


HOME_PORT = 8790
READY_TIMEOUT = 30.0
PKG = Path(__file__).resolve().parent


class Busy(ValueError):
    """A project is running, starting, restarting or being moved."""


@dataclass(frozen=True)
class State:
    state: str                         # ready | unresponsive | busy | absent
    info: dict = field(default_factory=dict)
    message: str = ""

    @property
    def occupied(self) -> bool:
        return self.state != "absent"


def _project(project):
    return Path(project).resolve() if project is not None else None


def _key(project) -> str:
    return registry.project_key(project) if project is not None else "home"


def path(project) -> Path:
    return registry.state_dir() / "instances" / (_key(_project(project)) + ".json")


def logfile(project) -> Path:
    return registry.state_dir() / "logs" / (_key(_project(project)) + ".log")


def _op_path(project) -> Path:
    return path(project).with_suffix(".operation.json")


def _operation(project) -> dict:
    op = registry.read_json(_op_path(project))
    if isinstance(op, dict) and server_alive(op.get("pid"), op.get("started")):
        return op
    return {}


def _record(file: Path) -> dict:
    value = registry.read_json(file)
    return value if isinstance(value, dict) else {}


def _discard(file: Path, expected: dict) -> None:
    if registry.read_json(file) == expected:
        try:
            file.unlink()
        except FileNotFoundError:
            pass


def _inspect(project, timeout: float) -> State:
    file = path(project)
    info = registry.read_json(file)
    if not isinstance(info, dict):
        return State("unresponsive", message="instance record cannot be read") if file.exists() else State("absent")
    if not server_alive(info.get("pid"), info.get("started")):
        return State("absent", info)
    app = "prism-local" if project is not None else "prism-home"
    live, refused = _probe(str(info.get("url") or ""), app, project, timeout)
    if live:
        if (info.get("token") and live.get("token") != info["token"]) or (
                live.get("pid") is not None and live["pid"] != info.get("pid")):
            return State("unresponsive", info, "instance identity does not match its server")
        return State("ready", {**info, "pages": live.get("pages", 0)})
    return State("absent", info) if refused else State("unresponsive", info, "server is not responding")


def inspect(project=None, timeout: float = 0.8) -> State:
    """Look without deleting records. A slow live process remains occupied."""
    project = _project(project)
    op = _operation(project)
    if op:
        # A successor may already answer while its parent still holds the handover lock.
        current = _record(path(project))
        if op.get("operation") in ("starting", "restarting") and current.get("token") == op.get("token"):
            result = _inspect(project, timeout)
            if result.state == "ready":
                return result
        return State("busy", current, "project is " + str(op.get("operation", "busy")))
    return _inspect(project, timeout)


class _Reservation:
    def __init__(self, projects, operation, timeout=0.2, structure=False, handoff=False):
        unique = {registry.norm(path(p)): _project(p) for p in projects}
        self.projects = [unique[k] for k in sorted(unique)]
        self.operation, self.timeout, self.structure = operation, timeout, structure
        self.handoff = handoff
        self.token = secrets.token_hex(20)
        self.stack = ExitStack()
        self.records = {}

    def acquire(self):
        deadline = time.monotonic() + self.timeout
        try:
            if self.structure:
                self.stack.enter_context(registry.FileLock(registry.state_dir() / "instances/structure.lock",
                                                           timeout=max(0, deadline - time.monotonic())))
            for project in self.projects:
                self.stack.enter_context(registry.FileLock(path(project).with_suffix(".lock"),
                                                           timeout=max(0, deadline - time.monotonic())))
        except BaseException:
            self.close()
            raise
        return self

    def mark(self):
        for project in self.projects:
            info = {"pid": os.getpid(), "started": process_started(os.getpid()),
                    "operation": self.operation, "token": self.token}
            if self.handoff:
                info.update(handoff=True, structure=self.structure,
                            projects=[str(p) if p is not None else None for p in self.projects])
            file = _op_path(project)
            self.records[file] = info
            registry.write_json(file, info)

    def close(self):
        try:
            for file, info in self.records.items():
                try:
                    _discard(file, info)
                except OSError:
                    pass
        finally:
            self.records.clear()
            self.stack.close()


@contextmanager
def exclusive(projects, targets=()):
    """Reserve old and new paths together. A slow editor is not safe to move."""
    reservation = _Reservation([*projects, *targets], "being moved", structure=True)
    try:
        reservation.acquire()
        for project in reservation.projects:
            if inspect(project, 0.3).occupied:
                raise Busy(f"its editor is open or not responding: {project}")
        reservation.mark()
        yield
    except TimeoutError as exc:
        raise Busy("a project is starting, restarting or being moved; try again") from exc
    finally:
        reservation.close()


def _python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def _tail(file: Path) -> str:
    try:
        return "\n".join(file.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
    except OSError:
        return ""


def _spawn(argv, cwd, output):
    flags = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
             if os.name == "nt" else {"start_new_session": True})
    child = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output,
                             stderr=subprocess.STDOUT, close_fds=True,
                             env={**git_env(), "PYTHONIOENCODING": "utf-8"}, **flags)
    # Keep the Popen alive and reap it even when ensure() has returned to Home. No
    # wait in the request thread, and no unowned live Popen/ResourceWarning.
    threading.Thread(target=child.wait, daemon=True).start()
    return child


def _await(projects, token, child, deadline):
    while time.monotonic() < deadline:
        for project in projects:
            info = _record(path(project))
            if info.get("token") == token:
                state = _inspect(project, min(0.5, max(0.01, deadline - time.monotonic())))
                if state.state == "ready":
                    return state.info
        if child.poll() not in (None, 0):
            break
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    return None


def _await_handoff(projects, token, child, deadline):
    """The successor must claim every reserved path before its parent releases any.

    The parent then exits: on Windows its cwd (or a venv redirector's cwd) can hold
    the folder being moved. The child owns the busy records during the lock transfer,
    acquires those locks, and keeps them through moving, listening and publication.
    """
    while time.monotonic() < deadline:
        records = [_operation(project) for project in projects]
        pid = records[0].get("pid") if records else None
        if pid and pid != os.getpid() and all(
                r.get("pid") == pid and r.get("token") == token for r in records):
            return {"handed_over": True, "pid": pid}
        if child.poll() is not None:
            break
        time.sleep(min(0.05, max(0, deadline-time.monotonic())))
    return None


def ensure(project=None, port=None, extra=(), timeout=READY_TIMEOUT) -> dict:
    """Reuse a ready instance or start one, within a single startup time budget."""
    project = _project(project)
    log = logfile(project)
    deadline = time.monotonic() + timeout
    error = "the server did not start"
    while time.monotonic() < deadline:
        state = inspect(project, min(0.5, max(0.01, deadline - time.monotonic())))
        if state.state == "ready":
            return {"url": state.info["url"], "started": False}
        if state.state == "unresponsive":
            error = state.message
        elif state.state == "busy":
            error = state.message
            if "moved" in error:
                break
            time.sleep(0.1)
            continue
        reservation = _Reservation([project], "starting", timeout=min(0.2, max(0, deadline-time.monotonic())))
        try:
            reservation.acquire()
            state = inspect(project, min(0.5, max(0.01, deadline-time.monotonic())))
            if state.state == "ready":
                return {"url": state.info["url"], "started": False}
            if state.state == "busy":
                error = state.message
                continue
            if state.occupied:
                error = state.message
                break
            if project is not None and not project.is_dir():
                error = f"folder not found: {project}"
                break
            if time.monotonic() >= deadline:
                break
            _discard(path(project), state.info)
            reservation.mark()
            log.parent.mkdir(parents=True, exist_ok=True)
            mode = "w"
            try:
                if log.exists():
                    os.replace(log, log.with_name(log.name + ".1"))
            except OSError:
                mode = "a"
            argv = [_python(), "-u", str(PKG / ("server.py" if project is not None else "hub.py")),
                    *([str(project)] if project is not None else []), "--port",
                    str(port if port is not None else registry.preferred_port(project) if project is not None else HOME_PORT),
                    "--port-tries", str(20 if project is not None else 10), "--no-browser", "--exit-when-idle",
                    "--ready-file", str(path(project)), "--instance-token", reservation.token, *extra]
            with open(log, mode, encoding="utf-8") as output:
                child = _spawn(argv, project if project is not None else Path.home(), output)
            info = _await([project], reservation.token, child, deadline)
            if info:
                return {"url": info["url"], "started": True}
            kill_tree(child)
            current = _record(path(project))
            if current.get("token") == reservation.token:
                _discard(path(project), current)
            break
        except TimeoutError:
            error = "project is starting or restarting"
            time.sleep(0.05)
        except (OSError, subprocess.SubprocessError) as exc:
            error = str(exc)
            break
        finally:
            reservation.close()
    return {"error": error, "log": _tail(log), "logfile": str(log)}


class Serving:
    def __init__(self, project, ready_file=None, token=None):
        self.project = _project(project)
        self.alias = Path(ready_file).resolve() if ready_file and Path(ready_file).resolve() != path(self.project).resolve() else None
        self.token = token or secrets.token_hex(20)
        self.reservation = None
        self.published = {}
        self.pending = None
        self.ending = False
        self.mutex = threading.Lock()
        self.delegated = bool(token)

    def __enter__(self):
        if self.delegated:
            op = _operation(self.project)
            if op.get("token") != self.token:
                raise Busy("startup reservation expired; open the project again")
            if op.get("handoff"):
                projects = [_project(p) for p in op["projects"]]
                if self.project not in projects or any(
                        _operation(p).get("token") != self.token for p in projects):
                    raise Busy("restart reservation expired; open the project again")
                self.reservation = _Reservation(projects, "restarting", timeout=READY_TIMEOUT,
                                                 structure=op.get("structure", False))
                self.reservation.token = self.token
                try:
                    # A token delegated by the lock holder permits this one claim before
                    # locking. Readers honor the live child owner even in the brief gap
                    # between the parent's unlock and the child's acquire.
                    self.reservation.mark()
                    self.reservation.acquire()
                except BaseException:
                    self.reservation.close()
                    raise
        else:
            self.reservation = _Reservation([self.project], "starting")
            try:
                self.reservation.acquire()
                if inspect(self.project, 0.3).occupied:
                    raise Busy("an editor is already open or not responding for this project")
                self.reservation.token = self.token
                self.reservation.mark()
            except BaseException:
                self.reservation.close()
                raise
        return self

    def retarget(self, project):
        """A failed folder move starts the successor at the old, also reserved, path."""
        project = _project(project)
        if _operation(project).get("token") != self.token:
            raise Busy("the fallback project was not reserved for this restart")
        self.project = project

    def publish(self, url, port):
        info = {"app": "prism-local" if self.project is not None else "prism-home",
                "pid": os.getpid(), "started": process_started(os.getpid()),
                "port": port, "url": url, "token": self.token}
        if self.project is not None:
            info["root"] = str(self.project)
        for file in dict.fromkeys([path(self.project), *([self.alias] if self.alias else [])]):
            self.published[file] = info
            registry.write_json(file, info)
        if self.reservation:
            self.reservation.close()
            self.reservation = None

    def unpublish(self):
        self.ending = True
        for file, info in self.published.items():
            try:
                _discard(file, info)
            except OSError:
                # A reader/antivirus may hold the file on Windows. The operation lock
                # still prevents takeover; a successor replaces it, or ensure later
                # discards the dead process's record. Cleanup must still stop workers.
                pass
        self.published.clear()

    def transition(self, project=...):
        with self.mutex:
            return self._transition(project)

    def _transition(self, project):
        if self.pending or self.ending:
            raise Busy("the server is already stopping or restarting")
        target = self.project if project is ... else _project(project)
        reservation = _Reservation([self.project, target], "restarting", timeout=2,
                                   structure=target != self.project, handoff=True)
        try:
            reservation.acquire()
            if target != self.project and inspect(target, 0.3).occupied:
                raise Busy("an editor is already open at the destination")
            reservation.mark()
        except BaseException:
            reservation.close()
            raise
        self.pending = Transition(self, reservation, target)
        return self.pending

    def __exit__(self, *exc):
        try:
            self.unpublish()
        finally:
            if self.reservation:
                self.reservation.close()
            if self.pending:
                self.pending.close()


class Transition:
    def __init__(self, serving, reservation, target):
        self.serving, self.reservation, self.target = serving, reservation, target

    def handover(self, argv, cwd, timeout=READY_TIMEOUT) -> dict:
        deadline = time.monotonic() + timeout
        argv = list(argv)
        if "--instance-token" in argv:
            i = argv.index("--instance-token")
            del argv[i:i+2]
        argv += ["--instance-token", self.reservation.token]
        if "--no-browser" not in argv:
            argv.append("--no-browser")
        log = logfile(self.target)
        child = None
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as output:
                child = _spawn(argv, cwd, output)
            info = _await_handoff(self.reservation.projects, self.reservation.token, child, deadline)
            if info:
                return info
            kill_tree(child)
            return {"error": "the replacement server did not take over", "log": _tail(log), "logfile": str(log)}
        except (OSError, subprocess.SubprocessError) as exc:
            if child:
                kill_tree(child)
            return {"error": str(exc), "log": _tail(log), "logfile": str(log)}

    def close(self):
        self.reservation.close()
        with self.serving.mutex:
            if self.serving.pending is self:
                self.serving.pending = None


def serving(project=None, ready_file=None, token=None) -> Serving:
    return Serving(project, ready_file, token)


def call(project, endpoint, timeout=10) -> None:
    """Best-effort notification of a ready editor, never ourselves."""
    state = inspect(project, timeout=1)
    if state.state != "ready" or state.info.get("pid") == os.getpid():
        return
    req = urllib.request.Request(state.info["url"] + endpoint.lstrip("/"), data=b"{}", method="POST",
                                          headers={"Content-Type": "application/json", "X-Prism-Local": "1"})
    try:
        HTTP.open(req, timeout=timeout).close()
    except OSError:
        pass
