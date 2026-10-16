"""A git repository as prism-local works on it: found from a folder, read freely, changed
by one prism-local process at a time.

    repo = Repository.find(folder)          # None: the folder is in no repository
    repo.git("log", "-1")                   # a read: runs at once, takes no lock
    with repo.changing():                   # this process alone changes it, no save meanwhile
        repo.git("add", "-A", "--", *paths)
        repo.git("commit", "-q", "-m", message, "--", *paths)
    with repo.changing(files=False):        # a fetch or a push: saves go on meanwhile
        repo.git("push", ...)

What changing() takes care of, so that no caller has to know it:
- the locks, always in one order: the repository's (one prism-local process at a time,
  .git/prism-local.lock), then its files' (FilesLock: the editors' saves, uploads and Undo
  take it too, also those of the other projects of a shared repository);
- before a command that takes the index (INDEX_WRITERS), another program's index.lock (an
  IDE's git status, a terminal) is waited for a moment, and one left by a git that crashed
  long ago is removed;
- such a command killed by its timeout leaves its own index.lock, which would stop the
  merge --abort after it: it is removed at once.
A command that takes the index outside changing() is a bug, and raises at once.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import gitenv
import registry

REPO_WAIT = 30           # seconds a commit, pull or push waits while another process changes the repository
NET_WAIT = 10            # seconds a fetch or push waits while another one talks to the remote
FILES_WAIT = 60          # seconds git waits for a save or an Undo to finish writing (they take milliseconds)
LOCK_WAIT = 10           # seconds to wait for another program's git (its index.lock)
STALE_LOCK = 600         # an index.lock this old was left by a git that crashed
# The git commands that take the index (and its index.lock), and change the files.
INDEX_WRITERS = frozenset(("merge", "commit", "add", "checkout", "rm", "mv", "reset", "restore",
                           "read-tree", "update-index", "stash", "cherry-pick", "revert", "rebase",
                           "apply", "pull"))


class RepoBusy(RuntimeError):
    """Another prism-local process is changing the repository; try again later."""


# ---------------------------------------------------------------- locks between processes

_LOCKS: dict[str, list] = {}
_LOCKS_GUARD = threading.Lock()


def _take(path: Path | None, timeout: float, own: list | None = None) -> list | None:
    """This process's hold on the OS file lock `path`: [its threads' lock, depth, the file
    lock]. The file is locked at the first hold in the process, and unlocked by the process
    ending at the latest, so a crash never leaves it behind. Re-entrant within a thread.
    path None: `own`, a lock of this process only. None after `timeout` seconds."""
    if path is None:
        entry = own
    else:
        with _LOCKS_GUARD:
            entry = _LOCKS.setdefault(registry.norm(path), [threading.RLock(), 0, None])
    deadline = time.monotonic() + timeout
    if not entry[0].acquire(timeout=max(timeout, 0)):
        return None
    if entry[1] == 0 and path is not None:
        held = registry.FileLock(path, timeout=max(deadline - time.monotonic(), 0))
        try:
            held.__enter__()
        except (TimeoutError, OSError):
            entry[0].release()
            return None
        entry[2] = held
    entry[1] += 1
    return entry


def _give_back(entry: list) -> None:
    """Undo one _take. The threads' lock is let go even when unlocking the file fails (a
    network drive gone): the file lock goes with its handle, closed regardless."""
    try:
        entry[1] -= 1
        if entry[1] == 0 and entry[2] is not None:
            held, entry[2] = entry[2], None
            held.__exit__(None, None, None)
    finally:
        entry[0].release()


@contextmanager
def repo_lock(git_dir: Path | None, timeout: float | None = None):
    """Held while prism-local changes a repository (commit, merge, push): one process at a
    time, also across processes: two editors in a repository shared by several projects, or
    an editor still pushing as it closes while the project opens again. An OS file lock
    (.git/prism-local.lock, see _take). Re-entrant within a thread. Raises RepoBusy after
    `timeout` (REPO_WAIT) seconds. Repository.changing() takes it; use that."""
    if git_dir is None:
        yield
        return
    entry = _take(Path(git_dir) / "prism-local.lock", REPO_WAIT if timeout is None else timeout)
    if entry is None:
        raise RepoBusy("git is busy in another prism-local window; will try again")
    try:
        yield
    finally:
        _give_back(entry)


@contextmanager
def network_lock(git_dir: Path, timeout: float | None = None):
    """Held while prism-local fetches or pushes a repository: one transfer at a time, also
    across processes (two fetches at once fail on the refs: the Home page's and an
    editor's). Apart from repo_lock, and never taken while holding it: a transfer takes
    minutes on a slow network, and commits, merges and saves go on meanwhile. An OS file
    lock (.git/prism-local-net.lock). Raises RepoBusy after `timeout` (NET_WAIT) seconds."""
    entry = _take(Path(git_dir) / "prism-local-net.lock", NET_WAIT if timeout is None else timeout)
    if entry is None:
        raise RepoBusy("another prism-local window is syncing with GitHub; will try again")
    try:
        yield
    finally:
        _give_back(entry)


class FilesLock:
    """Held while the project's files are written, or git reads and changes them: the
    editor's saves and an Undo, a sync's commit and merge (Repository.changing). A merge
    that gives up puts the files back as they were when it started, undoing a save made
    meanwhile; on Windows git cannot open a file a save is replacing. One per repository,
    across processes too (an OS file lock, .git/prism-local-files.lock): projects sharing a
    repository have their own editors, and one's merge changes the other's files. Never
    held for a fetch or a push, so a save waits for git's local work only. Re-entrant
    within a thread."""

    def __init__(self, git_dir_fn: Callable[[], Path | None]):
        self.git_dir_fn = git_dir_fn
        self._own = [threading.RLock(), 0, None]     # no repository (yet): this process only
        self._held = threading.local()

    def acquire(self, timeout: float = FILES_WAIT) -> bool:
        stack = getattr(self._held, "stack", [])
        if stack:                                    # again: the one this thread holds
            path = stack[-1][0]
        else:
            git_dir = self.git_dir_fn()
            path = Path(git_dir) / "prism-local-files.lock" if git_dir is not None else None
        entry = _take(path, timeout, self._own)
        if entry is None:
            return False
        self._held.stack = stack + [(path, entry)]
        return True

    def release(self) -> None:
        _give_back(self._held.stack.pop()[1])

    def held(self) -> bool:
        """Whether this thread holds it."""
        return bool(getattr(self._held, "stack", None))

    def __enter__(self):
        if not self.acquire():
            raise RepoBusy("another prism-local window is writing the files; will try again")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


# ---------------------------------------------------------------- the repository

SHARED_MARKER = "prism-repo.json"


def shared_marker(top: Path) -> bool:
    """Whether `top` is the top of a repository shared by several projects."""
    try:
        return bool(json.loads((top / SHARED_MARKER).read_text(encoding="utf-8")).get("shared"))
    except (OSError, ValueError, AttributeError):
        return False


def refs_stamp(git_dir: Path | None) -> tuple | None:
    """What changes on the disk whenever git's answer about the branches could: a
    commit, a checkout, a fetch, a change of remotes or of the configuration rewrite one
    of HEAD, the branch HEAD names, packed-refs, the remotes' refs, FETCH_HEAD, the index
    or config. The same stamp twice: the answers are the same, and need no git (which
    costs a process each time, some 20 ms on Windows and the antivirus's look).
    None when the git dir cannot be looked at: then git is asked."""
    if git_dir is None:
        return None

    def st(p: Path):
        try:
            s = p.stat()
            return s.st_mtime_ns, s.st_size
        except OSError:
            return None
    try:
        git_dir = Path(git_dir)
        common = git_dir
        if (git_dir / "commondir").is_file():               # a worktree: refs live in the common dir
            common = (git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()).resolve()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        branch = common / head[5:].strip() if head.startswith("ref:") else None
        remotes = []
        for folder, _, names in os.walk(common / "refs" / "remotes"):
            remotes += [(folder, n, st(Path(folder) / n)) for n in sorted(names)]
        return (head, st(git_dir / "HEAD"), st(branch) if branch else None, st(common / "packed-refs"),
                st(git_dir / "index"), st(common / "config"), st(git_dir / "FETCH_HEAD"), tuple(sorted(remotes)))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Place:
    """Where a project's folder is in git. kind: "own" (the top of its repository),
    "shared" (in a repository several projects share: its top has SHARED_MARKER),
    "nested" (inside some other repository, such as prism-local's: none to sync with), or
    "none". prefix: the project's path in its repository, "" or "ex2/"."""
    kind: str
    repo: Repository | None = None
    prefix: str = ""

    @property
    def syncs(self) -> bool:
        return self.kind in ("own", "shared")


def locate(folder: Path, files: Callable[[], object] | None = None, timeout: float = 20) -> Place:
    """The one place that tells where a project's folder is in git (see Place)."""
    folder = Path(folder)
    # Git finds a repository only through a .git in the folder or one above it (git_env
    # drops GIT_DIR and the like): without one, there is none, and no git to run.
    if not folder.is_dir() or not any((p / ".git").exists() for p in (folder, *folder.resolve().parents)):
        return Place("none")
    try:
        r = gitenv.git(folder, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--show-prefix",
                       timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return Place("none")
    lines = r.stdout.split("\n")
    if r.returncode != 0 or len(lines) < 3:
        return Place("none")
    repo = Repository(Path(lines[0]).resolve(), Path(lines[1]), files)
    prefix = lines[2].strip()
    if not prefix:
        return Place("own", repo)
    return Place("shared" if shared_marker(repo.top) else "nested", repo, prefix)


class Repository:
    """One git repository: its top (where paths start) and its git dir. See the module's
    docstring. `files` gives the files lock its changes hold (the editor's save lock, so
    that both are one); by default the repository's own FilesLock."""

    def __init__(self, top: Path, git_dir: Path, files: Callable[[], object] | None = None):
        self.top, self.git_dir = Path(top), Path(git_dir)
        own = FilesLock(lambda: self.git_dir)
        self._files = files or (lambda: own)
        self._here = threading.local()               # this thread's changing() holds

    @classmethod
    def find(cls, folder: Path, files: Callable[[], object] | None = None) -> Repository | None:
        """The repository `folder` is in, or None."""
        try:
            r = gitenv.git(folder, "rev-parse", "--show-toplevel", "--absolute-git-dir", timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        lines = r.stdout.strip().splitlines()
        if r.returncode != 0 or len(lines) != 2:
            return None
        return cls(Path(lines[0]).resolve(), Path(lines[1]), files)

    def _held(self) -> list[bool]:
        """This thread's changing() blocks, innermost last: whether each holds the files."""
        if not hasattr(self._here, "stack"):
            self._here.stack = []
        return self._here.stack

    @contextmanager
    def changing(self, files: bool = True, wait: float | None = None):
        """This process alone changes the repository (RepoBusy after `wait`, REPO_WAIT, seconds),
        and with `files`, no save or Undo writes the files meanwhile. Re-entrant within a thread."""
        with repo_lock(self.git_dir, wait), (self._files() if files else nullcontext()):
            self._held().append(files)
            try:
                yield self
            finally:
                self._held().pop()

    def transferring(self, wait: float | None = None):
        """A fetch or push of this repository: one at a time (network_lock). Commits and
        merges do not wait for it, and it holds none of their locks."""
        return network_lock(self.git_dir, wait)

    def git(self, *args: str, timeout: float = 60, interactive: bool = False) -> subprocess.CompletedProcess:
        """git at the repository's top. A command that takes the index runs only inside
        changing() (with the files), after another program's index.lock is gone."""
        writes = bool(args) and args[0] in INDEX_WRITERS
        if writes:
            if not any(self._held()):
                raise RuntimeError(f"git {args[0]} changes the repository: run it in changing()")
            self._wait_index()
        started = time.time()
        try:
            return gitenv.git(self.top, *args, timeout=timeout, interactive=interactive)
        except subprocess.TimeoutExpired:
            if writes:
                # Killed, it leaves its index.lock, which would stop the merge --abort that
                # follows. One made since it started is its own: none other could take it
                # while it held it. (Any other command takes none: one there is another's.)
                lock = self.git_dir / "index.lock"
                try:
                    if lock.stat().st_mtime >= started - 1:
                        lock.unlink()
                except OSError:
                    pass
            raise

    def ok(self, *args: str, timeout: float = 60) -> str:
        """git(), its output; RuntimeError with git's message when it fails."""
        r = self.git(*args, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-400:] or f"git {args[0]} failed")
        return r.stdout

    def _wait_index(self) -> None:
        """Another program's git (an IDE's status, a terminal) holds the index for a moment;
        one left by a git that crashed long ago (a power cut, a killed process) goes."""
        lock = self.git_dir / "index.lock"
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK:
                    lock.unlink()
                    return
            except OSError:
                return                               # none
            if time.monotonic() >= deadline:
                raise RepoBusy("git is busy (index.lock); will try again")
            time.sleep(0.2)
