"""State shared by the launcher, the project servers and the Home page.

Everything lives under one state directory (PRISM_STATE_DIR, otherwise
%LOCALAPPDATA%\\prism-local or ~/.local/state/prism-local):

    projects.json          the project list shown on the Home page
    instances/<key>.json   {pid, port, url, root} of each running server
    instances/home.json    the same for the Home page server
    logs/<key>.log         server output of launcher-started servers

Only the Python standard library is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
import zlib
from pathlib import Path

WIN = os.name == "nt"
PORT_BASE, PORT_SPAN, PORT_TRIES = 8800, 1000, 20
HOME_PORT, HOME_PORT_TRIES = 8790, 10          # below the per-project range
HOME_KEY = "home"

# Never send requests for 127.0.0.1 through a system proxy.
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def state_dir() -> Path:
    if os.environ.get("PRISM_STATE_DIR"):
        return Path(os.environ["PRISM_STATE_DIR"])
    if WIN:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "prism-local"


def norm(path) -> str:
    return os.path.normcase(str(path))


def project_key(project: Path) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in project.name) or "project"
    return f"{safe}-{hashlib.sha1(norm(project).encode('utf-8')).hexdigest()[:10]}"


def preferred_port(project: Path) -> int:
    """A stable port per project, so the browser keeps its per-origin state (tabs, chat)."""
    return PORT_BASE + zlib.crc32(norm(project).encode("utf-8")) % PORT_SPAN


def instance_file(key: str) -> Path:
    return state_dir() / "instances" / f"{key}.json"


def log_file(key: str) -> Path:
    return state_dir() / "logs" / f"{key}.log"


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    for attempt in range(40):      # Windows refuses while another process is reading the file
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


class FileLock:
    """Exclusive lock between processes (two quick clicks, launcher vs. Home page)."""

    def __init__(self, path: Path, timeout: float = 60.0):
        self.path, self.timeout, self.fh = path, timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._lock(True)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    self.fh.close()
                    raise TimeoutError(f"another Prism process holds {self.path}")
                time.sleep(0.1)

    def __exit__(self, *exc):
        try:
            self._lock(False)
        finally:
            self.fh.close()

    def _lock(self, on: bool) -> None:
        if WIN:
            import msvcrt
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK if on else msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.fh, (fcntl.LOCK_EX | fcntl.LOCK_NB) if on else fcntl.LOCK_UN)


# ---------------------------------------------------------------- running servers

def ping(url: str, app: str = "prism-local", root: Path | None = None,
         timeout: float = 2.0) -> dict | None:
    """The /api/ping answer of the server at `url`, if it is `app` (serving `root`)."""
    try:
        with HTTP.open(url + "api/ping", timeout=timeout) as r:
            info = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if info.get("app") != app:
        return None
    if root is not None and norm(info.get("root", "")) != norm(root):
        return None
    return info


def server_alive(pid) -> bool:
    """Whether `pid` is a running Python process (a prism-local server), not a process
    that got the number of a server that ended long ago."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if WIN:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(0x1000 | 0x00100000, False, pid)  # query limited info | synchronize
        if not h:
            return ctypes.get_last_error() == 5                # access denied: it exists
        try:
            if k32.WaitForSingleObject(h, 0) != 0x102:         # not WAIT_TIMEOUT: it has ended
                return False
            buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return "python" in Path(buf.value).name.lower()
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return "python" in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode(
            "utf-8", "replace").lower()
    except OSError:
        return True                     # no /proc (macOS): trust the process table


def running_instance(inst: Path, app: str = "prism-local", root: Path | None = None,
                     timeout: float = 2.0, cleanup: bool = True) -> dict | None:
    """The instance file's contents if that server answers. A file whose server has
    ended (it did not exit cleanly) is removed; a server that is only slow keeps it."""
    info = read_json(inst)
    if info and info.get("url"):
        live = ping(info["url"], app, root, timeout)
        if live:
            return {**info, "pages": live.get("pages", 0)}
    if cleanup and inst.exists() and not server_alive((info or {}).get("pid")):
        try:
            inst.unlink()
        except OSError:
            pass
    return None


# ---------------------------------------------------------------- project list

def _registry_path() -> Path:
    return state_dir() / "projects.json"


def load_projects() -> dict:
    data = read_json(_registry_path()) or {}
    data.setdefault("projects", [])
    return data


def update_projects(fn):
    """Run fn(data) on projects.json under a lock, save, and return fn's result."""
    path = _registry_path()
    with FileLock(path.with_suffix(".lock"), timeout=10):
        data = load_projects()
        result = fn(data)
        write_json(path, data)
    return result


def find(data: dict, project: Path) -> dict | None:
    n = norm(project)
    return next((p for p in data["projects"] if norm(p["path"]) == n), None)


def touch(project: Path, opened: bool = True) -> dict:
    """Add `project` to the list if needed and record that it was opened now."""
    project = Path(project).resolve()

    def fn(data):
        entry = find(data, project)
        if entry is None:
            entry = {"path": str(project), "added": time.time()}
            data["projects"].append(entry)
        if opened:
            entry["opened"] = time.time()
        return dict(entry)

    return update_projects(fn)


def safe_touch(project: Path) -> None:
    """touch(), for callers that must not fail because of the project list."""
    try:
        touch(project)
    except (OSError, TimeoutError, ValueError):
        pass


# ---------------------------------------------------------------- starting servers

PKG = Path(__file__).resolve().parent
SERVER, HUB = PKG / "server.py", PKG / "hub.py"
READY_TIMEOUT = 30.0


def server_python() -> str:
    """python.exe rather than pythonw.exe: the server then has a (hidden) console that
    its build and git subprocesses share, and a real stdout for the log."""
    import sys
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return sys.executable


def tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def ensure_server(project: Path | None, port: int | None = None, extra: list[str] = (),
                  timeout: float = READY_TIMEOUT) -> dict:
    """Make sure a server runs for `project` (None: the Home page).

    Returns {"url", "started"} or {"error", "log", "logfile"}. A new server is started
    detached with --exit-when-idle: nothing waits for it, it removes its own instance
    file and stops by itself shortly after its last page is closed (or after a minute
    if no page ever connects). The lock keeps two quick clicks, or the launcher and
    the Home page, from starting two servers for the same project."""
    import subprocess
    if project is None:
        key, app, script, target, cwd = HOME_KEY, "prism-home", HUB, [], Path.home()
        port, tries = port or HOME_PORT, HOME_PORT_TRIES
    else:
        project = Path(project).resolve()
        key, app, script, target, cwd = project_key(project), "prism-local", SERVER, [str(project)], project
        port, tries = port or preferred_port(project), PORT_TRIES
    inst, log = instance_file(key), log_file(key)
    with FileLock(inst.with_suffix(".lock")):
        info = running_instance(inst, app, project)
        if info:
            return {"url": info["url"], "started": False}
        old = read_json(inst)
        if old and server_alive(old.get("pid")):
            # It runs but did not answer in time (busy, or waking up): give it a little
            # longer rather than start a second server for the same project.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                info = running_instance(inst, app, project, timeout=2.0, cleanup=False)
                if info:
                    return {"url": info["url"], "started": False}
                time.sleep(0.5)
            return {"error": f"prism-local for this project runs (process {old.get('pid')}) but "
                             "does not answer. Close its pages and try again in a minute.",
                    "log": tail(log), "logfile": str(log)}
        log.parent.mkdir(parents=True, exist_ok=True)
        mode = "w"
        try:
            if log.exists():
                os.replace(log, log.with_name(log.name + ".1"))
        except OSError:                 # still open by a server that is just exiting
            mode = "a"
        cmd = [server_python(), "-u", str(script), *target, "--port", str(port),
               "--port-tries", str(tries), "--no-browser", "--exit-when-idle",
               "--ready-file", str(inst), *extra]
        kw = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
              if WIN else {"start_new_session": True})
        with open(log, mode, encoding="utf-8") as logf:     # the child keeps its own handle
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=logf,
                                    stderr=subprocess.STDOUT, close_fds=True,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **kw)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and proc.poll() is None:
            info = running_instance(inst, app, project, timeout=1.0, cleanup=False)
            if info and info.get("pid") == proc.pid:
                return {"url": info["url"], "started": True}
            time.sleep(0.1)
        if proc.poll() is None:
            proc.kill()
    return {"error": "the server did not start", "log": tail(log), "logfile": str(log)}
