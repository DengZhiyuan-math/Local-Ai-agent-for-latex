"""State shared by the launcher, the project servers and the Home page.

Everything lives under one state directory (PRISM_STATE_DIR, otherwise
%LOCALAPPDATA%\\prism-local or ~/.local/state/prism-local):

    projects.json          the project list shown on the Home page

Instance records, startup logs and process coordination are owned by instances.py.

Only the Python standard library is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import zlib
from pathlib import Path
from fsutil import replace_file

WIN = os.name == "nt"
PORT_BASE, PORT_SPAN = 8800, 1000


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


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class StateUnreadable(ValueError):
    """A state file is there but cannot be read: damaged, or held by another program."""


def read_state(path: Path, tries: int = 10) -> dict:
    """A state file prism-local keeps (projects.json, settings.json): {} when there is none
    yet. One that is there but cannot be read raises StateUnreadable, after about a second of
    trying while another program (an antivirus, a backup) holds it: taken for an empty one,
    it would be written back empty, and the project list or the settings lost."""
    for attempt in range(tries):
        try:
            text = path.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return {}
        except OSError as e:
            if attempt == tries - 1:
                raise StateUnreadable(f"{path} cannot be read ({e}); nothing was changed") from e
            time.sleep(0.1)
    try:
        data = json.loads(text)
    except ValueError as e:
        data = e
    if not isinstance(data, dict):
        raise StateUnreadable(f"{path} is damaged; nothing was changed. Fix it, or rename it "
                              "to start afresh.")
    return data


def write_json(path: Path, data) -> None:
    replace_file(path, json.dumps(data, indent=1, ensure_ascii=False).encode("utf-8"))


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


# ---------------------------------------------------------------- project list

def _registry_path() -> Path:
    return state_dir() / "projects.json"


def load_projects() -> dict:
    """The project list. Raises StateUnreadable rather than taking a damaged one for empty."""
    data = read_state(_registry_path())
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


def set_entry_name(entry: dict, name) -> None:
    """The name shown for a project; empty (or the folder's own name) shows the folder's."""
    name = str(name or "").strip()[:120]
    if name and name != Path(entry["path"]).name:
        entry["name"] = name
    else:
        entry.pop("name", None)


def display_name(project: Path) -> str:
    """The project's name in the list (Rename), else its folder's name."""
    entry = find(load_projects(), Path(project).resolve())
    return (entry or {}).get("name") or Path(project).name


def rename(project: Path, name) -> str:
    """Set the project's name in the list (adding it if needed); its folder keeps its name."""
    project = Path(project).resolve()

    def fn(data):
        entry = find(data, project)
        if entry is None:
            entry = {"path": str(project), "added": time.time()}
            data["projects"].append(entry)
        set_entry_name(entry, name)
        return entry.get("name") or project.name

    return update_projects(fn)


BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def folder_name(name) -> str:
    """A folder name Windows, macOS and Linux all accept, from a project's name
    ("Paper: draft" -> "Paper- draft"); "" when nothing is left."""
    return BAD_NAME.sub("-", str(name or "")).strip(" .")[:120]


def inside(p: Path, top: Path) -> bool:
    """Whether p is top or below it."""
    return norm(p) == norm(top) or norm(p).startswith(norm(top).rstrip("\\/") + os.sep)


class MoveIncomplete(OSError):
    """Files moved, but neither saving the list nor rolling the files back worked."""

    def __init__(self, actual: Path, journal: Path, cause):
        self.actual, self.journal = actual, journal
        super().__init__(f"files are in {actual}; the project list could not be saved ({cause}). "
                         f"Recovery details: {journal}")


def move_folder(old: Path, new: Path, wait: float = 20.0, *, across: bool = False) -> None:
    """Rename a project's folder, and make the list follow it (projects inside it, a
    folder's shared repository there). A folder still in use (a server that is just
    exiting, an Explorer window) is tried again for `wait` seconds."""
    old = Path(old).resolve()
    new = Path(new).parent.resolve() / Path(new).name
    same = norm(old) == norm(new)            # only the case changes
    if new.exists() and not same:
        raise FileExistsError(f"a folder named {new.name} already exists there")
    def repoint(data):
        for e in data["projects"]:
            p = Path(e["path"])
            if inside(p, old):
                e["path"] = str(new / p.relative_to(old))
                if e.get("name") == Path(e["path"]).name:
                    e.pop("name")
        for f in data.get("folders", []):
            sync = f.get("sync") or {}
            if sync.get("repo") and inside(Path(sync["repo"]), old):
                sync["repo"] = str(new / Path(sync["repo"]).relative_to(old))
    # Read and validate before touching the files. Keep the metadata lock through the
    # move, so another request cannot write an outdated project list back over it.
    file = _registry_path()
    journal = state_dir() / "moves" / (project_key(old) + ".json")
    mover = shutil.move if across else os.rename
    with FileLock(file.with_suffix(".lock"), timeout=10):
        data = load_projects()
        repoint(data)
        write_json(journal, {"from": str(old), "to": str(new), "projects": data,
                             "instruction": "Check which folder exists, then restore its paths in projects.json."})
        moved = False
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    mover(old, new)
                    moved = True
                    break
                except OSError as exc:
                    if across:
                        # shutil.move may already have copied part/all of a directory
                        # before failing to remove the source. Retrying could move it
                        # inside the now-existing target. Keep the journal for recovery.
                        if new.exists():
                            raise OSError(f"the move from {old} to {new} did not finish; keep both folders. "
                                          f"Recovery details: {journal}. {exc}") from exc
                        raise
                    if not isinstance(exc, PermissionError):
                        raise
                    if time.monotonic() > deadline:
                        raise PermissionError(f"{old.name} is in use by another program; close it and try again")
                    time.sleep(0.25)
            try:
                write_json(file, data)
            except OSError as exc:
                try:
                    mover(new, old)
                    moved = False
                except OSError:
                    raise MoveIncomplete(new, journal, exc) from exc
                raise OSError(f"the project list could not be saved; the folder was restored to {old}: {exc}") from exc
        finally:
            # Leave evidence if rollback failed (or a cross-device copy was partial).
            rolled_back = not moved and old.is_dir() and (same or not new.exists())
            saved = moved and read_json(file) == data
            if rolled_back or saved:
                try:
                    journal.unlink()
                except OSError:
                    pass


def safe_touch(project: Path) -> None:
    """touch(), for callers that must not fail because of the project list."""
    try:
        touch(project)
    except (OSError, TimeoutError, ValueError):
        pass
