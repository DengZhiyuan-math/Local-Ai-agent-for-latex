"""Record and sync the project's changes with its GitHub repository.

While the editor runs, a worker thread keeps the project's own git repository up to date:

- autosave commits: once you stop editing for IDLE seconds (or at the latest MAX_WAIT
  seconds after the first unsaved change), everything that changed is committed;
- agent turns: the edits you made before a turn are committed first, then the files the
  turn changed are committed alone, with your request as the message (an Undo of the
  turn is committed the same way);
- push after every commit, and fetch every FETCH_EVERY seconds: a remote that is ahead
  (another computer, GitHub's web editor) is fast-forwarded, so the editor reloads the
  files; histories that have diverged are reported, never merged automatically.

Build output (the build folder, .aux, .log, ...) is never committed. Sync can be switched
off per clone (.git/prism-local.json, never committed).

A project is synced when it is the top of its own repository, or when it sits in a
repository shared by several projects: one whose top has a SHARED_MARKER file (the Home
page writes it when a folder of projects gets one repository). There each editor commits
only its own project's files, and pushes and pulls the shared branch. Nothing happens in
a folder inside any other repository (prism-local's examples, say).
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import keepboth
import registry
import gitenv
from fsutil import write_bytes

IDLE = 120               # seconds without an edit before an autosave commit
MAX_WAIT = 600           # ... but at the latest this long after the first change
FETCH_EVERY = 60         # seconds between fetches from the remote (co-authors' changes arrive soon)
QUIET = 20               # automatic pulls wait this long after your last save (no clash with typing)
STALE_LOCK = 600         # an index.lock this old was left by a git that crashed
PUSH_TRIES = 5           # pushes in a row while co-authors keep pushing in between
TICK = 5                 # the worker's period
RETRY = 60               # seconds before a failed push is tried again
MAX_FILE = 50 * 1024 * 1024    # GitHub refuses files over 100 MB; keep well below
LOCK_WAIT = 10           # seconds to wait for another editor's git command in a shared repository
SHARED_MARKER = "prism-repo.json"
# Files LaTeX writes next to the sources when they are not in the build folder.
AUX = (".aux", ".log", ".out", ".toc", ".lof", ".lot", ".bbl", ".blg", ".bcf", ".run.xml",
       ".fls", ".fdb_latexmk", ".synctex.gz", ".synctex", ".nav", ".snm", ".vrb", ".idx",
       ".ilg", ".ind", ".xdv", ".dvi", ".thm", ".loe")
# The git commands prism-local runs that take the index (and its index.lock).
INDEX_WRITERS = frozenset(("merge", "commit", "add", "checkout"))
# New files never recorded: those, and a save half-way (fsutil.replace_file's temporary file).
NOT_SOURCES = AUX + (".prism-tmp",)


def redact_url(url: str) -> str:
    if "://" in url:
        try:
            p = urlsplit(url)
            host = p.hostname or ""
            if p.port:
                host += f":{p.port}"
            return urlunsplit((p.scheme, host, p.path, "", ""))
        except ValueError:
            return "(invalid URL)"
    return re.sub(r"^[^/@:]+@", "", url)


def safe_git_message(message: str) -> str:
    return re.sub(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"]+", lambda m: redact_url(m.group()), message)


def repository_identity(url: str, top: Path) -> str:
    clean = redact_url(url)
    m = re.fullmatch(r"(?:https?://github\.com/|ssh://github\.com(?::22)?/|github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?", clean)
    if m:
        return "github.com/" + m.group(1).lower()
    if "://" not in url and ":" not in url:
        return registry.norm((top / url).resolve())
    return clean


def public_target(target: dict | None) -> dict | None:
    if not isinstance(target, dict):
        return None
    return {k: redact_url(target[k]) if k.endswith("_url") else target.get(k)
            for k in ("remote", "branch_ref", "fetch_url", "push_url", "repository_identity",
                      "configuration_fingerprint") if k in target}


def github_url(target: dict | None) -> str | None:
    identity = (target or {}).get("repository_identity", "")
    return "https://" + identity if identity.startswith("github.com/") else None


GUARD_MARK = "prism-local: refuse force pushes"
# A pre-push hook in every repository prism-local syncs: a push that would drop commits
# from the remote (a force push) or delete a branch there is refused, whoever starts it:
# this editor, a terminal, an AI agent's shell. Co-authors' work on GitHub cannot be
# wiped from here. `git push --no-verify` skips it on purpose; deleting the file removes it.
GUARD = """#!/bin/sh
# prism-local: refuse force pushes
# A push that would drop commits from the remote, or delete a branch there, is refused,
# so that co-authors' work on GitHub cannot be wiped from this copy. Delete this file to
# allow such pushes again; `git push --no-verify` skips this check once.
while read local_ref local_sha remote_ref remote_sha; do
  case "$remote_sha" in *[!0]*) ;; *) continue ;; esac        # a new branch there: fine
  case "$local_sha" in
    *[!0]*) ;;
    *) echo "prism-local: refusing to delete $remote_ref on the remote." >&2; exit 1 ;;
  esac
  if ! git merge-base --is-ancestor "$remote_sha" "$local_sha" 2>/dev/null; then
    echo "prism-local: refusing a push that would drop commits from $remote_ref on the remote" >&2
    echo "(a force push). Bring the remote's changes in first (pull)." >&2
    exit 1
  fi
done
exit 0
"""


# What decides a repository's sync target (_candidate) and its hooks: its config (also a
# worktree's), HEAD, and your own git config. Their contents are the key of the cache
# below, so any change is seen at once; a system-wide config or an [include]d file is not
# read, so an entry is also renewed after CACHE_TTL seconds at the latest.
CACHE_TTL = 60
_CACHE: dict[tuple, tuple] = {}
_CACHE_LOCK = threading.Lock()


def _config_key(git_dir: Path) -> tuple:
    common = git_dir
    try:                                  # a worktree keeps its config in the main repository
        common = (git_dir / (git_dir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except OSError:
        pass
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git" / "config"

    def read(f: Path) -> bytes | None:
        try:
            return f.read_bytes()
        except OSError:
            return None
    return tuple(read(f) for f in (common / "config", git_dir / "config.worktree", git_dir / "HEAD",
                                   Path.home() / ".gitconfig", xdg))


def _cached(kind: str, git_dir: Path, compute: Callable[[], object]):
    """compute(), or its result from less than CACHE_TTL seconds ago while git_dir's
    configuration (_config_key) is unchanged. A ValueError it raises is kept like a result."""
    key, stamp = (kind, registry.norm(git_dir)), _config_key(git_dir)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and hit[0] == stamp and time.monotonic() - hit[1] < CACHE_TTL:
        value = hit[2]
    else:
        try:
            value = compute()
        except ValueError as e:
            value = e
        with _CACHE_LOCK:
            _CACHE[key] = (stamp, time.monotonic(), value)
    if isinstance(value, ValueError):
        raise ValueError(str(value))
    return value


def install_guard(git_dir: Path) -> bool:
    """Put GUARD in the repository's hooks, unless it has a pre-push hook of its own or
    uses a hooks folder of its own (core.hooksPath). True when the guard is in place."""
    hook = _cached("hooks", git_dir, lambda: _hook_path(git_dir))
    if hook is None:
        return False
    try:
        if hook.exists():
            return GUARD_MARK in hook.read_text(encoding="utf-8", errors="replace")
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_bytes(GUARD.encode("utf-8"))      # \n line ends: sh reads it on Windows too
        hook.chmod(0o755)
        return True
    except OSError:
        return False


def _hook_path(git_dir: Path) -> Path | None:
    """Where the repository's pre-push hook goes; None when it uses a hooks folder of its own."""
    try:
        if gitenv.git(git_dir, "config", "--get", "core.hooksPath", timeout=20).stdout.strip():
            return None
        r = gitenv.git(git_dir, "rev-parse", "--git-path", "hooks", timeout=20)
        if r.returncode:
            return None
        return (git_dir / r.stdout.strip()).resolve() / "pre-push"
    except (OSError, subprocess.SubprocessError):
        return None


def shared_marker(top: Path) -> bool:
    """Whether `top` is the top of a repository shared by several projects."""
    try:
        return bool(json.loads((top / SHARED_MARKER).read_text(encoding="utf-8")).get("shared"))
    except (OSError, ValueError, AttributeError):
        return False


def record_move(old: Path, new: Path) -> str | None:
    """A project's folder was renamed inside a repository shared by several projects:
    commit the move there (its own repository needs nothing: its paths do not change).
    Returns the commit message; None when there is nothing to record. Raises RuntimeError
    (RepoBusy too) when the move could not be committed: say so, since nobody else would
    commit it (each editor of the repository commits its own project only), and the other
    copies would get the new folder next to the old one."""
    r = gitenv.git(new, "rev-parse", "--show-toplevel", "--absolute-git-dir")
    lines = r.stdout.strip().splitlines()
    if r.returncode != 0 or len(lines) != 2:
        return None
    top, git_dir = Path(lines[0]).resolve(), Path(lines[1])
    if top == Path(new).resolve() or not shared_marker(top):
        return None
    specs = [f":(top,literal){Path(p).resolve().relative_to(top).as_posix()}" for p in (old, new)]
    message = f"Rename {Path(old).name} to {Path(new).name}"
    # As an editor's commit: one prism-local process at a time, no save meanwhile.
    with repo_lock(git_dir), FilesLock(lambda: git_dir):
        deadline = time.monotonic() + LOCK_WAIT          # another program's git, for a moment
        while (git_dir / "index.lock").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("git is busy (index.lock)")
            time.sleep(0.2)
        for args in (("add", "-A", "--", *specs), ("commit", "-q", "-m", message, "--", *specs)):
            c = gitenv.git(top, *args)
            if c.returncode != 0:
                raise RuntimeError((c.stderr or c.stdout).strip()[-300:] or f"git {args[0]} failed")
    return message


class RepoBusy(RuntimeError):
    """Another prism-local process is changing the repository; try again later."""


REPO_WAIT = 30           # seconds a commit, pull or push waits while another process changes the repository
_REPO_LOCKS: dict[str, list] = {}
_REPO_LOCKS_GUARD = threading.Lock()


def _take(path: Path | None, timeout: float, own: list | None = None) -> list | None:
    """This process's hold on the OS file lock `path`: [its threads' lock, depth, the file
    lock]. The file is locked at the first hold in the process, and unlocked by the process
    ending at the latest, so a crash never leaves it behind. Re-entrant within a thread.
    path None: `own`, a lock of this process only. None after `timeout` seconds."""
    if path is None:
        entry = own
    else:
        with _REPO_LOCKS_GUARD:
            entry = _REPO_LOCKS.setdefault(registry.norm(path), [threading.RLock(), 0, None])
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
    an editor still pushing as it closes while the project opens again. It is an OS file
    lock (.git/prism-local.lock, see _take). Re-entrant within a thread. Raises RepoBusy
    after `timeout` (REPO_WAIT) seconds."""
    if git_dir is None:
        yield
        return
    entry = _take(git_dir / "prism-local.lock", REPO_WAIT if timeout is None else timeout)
    if entry is None:
        raise RepoBusy("git is busy in another prism-local window; will try again")
    try:
        yield
    finally:
        _give_back(entry)


FILES_WAIT = 60          # seconds git waits for a save or an Undo to finish writing (they take milliseconds)


class FilesLock:
    """Held while the project's files are written, or git reads and changes them: the
    editor's saves and an Undo, a sync's commit and merge (GitSync.writes). A merge that
    gives up puts the files back as they were when it started, undoing a save made
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

    def __enter__(self):
        if not self.acquire():
            raise RepoBusy("another prism-local window is writing the files; will try again")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name).strip("-")[:30] or "github"


def _names(paths: list[str]) -> str:
    names = [p.split("/")[-1] for p in paths]
    return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


class GitSync:
    def __init__(self, root_fn: Callable[[], Path], outdir_fn: Callable[[], str],
                 busy: Callable[[], bool] = lambda: False, writes=None):
        self.root_fn, self.outdir_fn, self.busy_fn = root_fn, outdir_fn, busy
        self.lock = threading.RLock()         # one git command sequence at a time
        # Held while git reads or changes the files: a commit, with the look at what changed;
        # a merge, from the commit before it to its end (FilesLock). The editor's saves and
        # an Undo take it too (server.SAVE_LOCK).
        self.writes = writes or FilesLock(lambda: self.git_dir if self.check_repo() else None)
        self.own: bool | None = None          # the project has a repository to sync with
        self.git_dir: Path | None = None
        self.top: Path | None = None          # that repository's top: the project, or above it
        self.prefix = ""                      # the project's path in it: "" or "ex2/"
        self.last_edit = self.dirty_since = None
        self.saves = 0                        # touched() so far
        self._touch = threading.Lock()        # touched() against commit_all's "all recorded"
        self.last_commit: dict | None = None  # {"at", "message", "hash"}
        self.last_push = self.last_fetch = 0.0
        self.push_failed_at = 0.0
        self.ahead = self.behind = 0
        self.has_remote = False
        self.upstream: str | None = None
        self.target: dict | None = None
        self.blocked_reason: str | None = None
        self.state = "idle"                   # idle | committing | pushing | pulling
        self.error: str | None = None
        self.notice: str | None = None        # e.g. "pulled 2 commits from GitHub"
        self.clash: list[str] = []            # files you and GitHub both changed (project paths)
        self.kept: list[dict] = []            # where both versions were kept: {"path", "line", "who", "mtime"}
        self.stop = threading.Event()

    # ------------------------------------------------------------ git
    def git(self, *args: str, timeout: float = 60, interactive: bool = False) -> subprocess.CompletedProcess:
        """git at the repository's top: paths are relative to it (see rel()). Only a fetch
        or push you started (manual) may ask you to log in (gitenv.NO_PROMPTS)."""
        started = time.time()
        try:
            r = gitenv.git(self.top or self.root_fn(), *args, timeout=timeout, interactive=interactive)
        except subprocess.TimeoutExpired:
            # Killed: a command that takes the index (run under repo_lock and the files lock,
            # as all of them here are) leaves its index.lock, which would stop the merge
            # --abort that follows. One made since it started is its own: none other could
            # take it while it held it. Any other command (a fetch, a read) took none, and
            # one there now is someone else's.
            lock = self.git_dir / "index.lock" if self.git_dir and args[0] in INDEX_WRITERS else None
            try:
                if lock and lock.stat().st_mtime >= started - 1:
                    lock.unlink()
            except OSError:
                pass
            raise
        if args[0] in ("push", "fetch"):
            print(json.dumps({"event": "git_sync", "git_top": str(self.top),
                              "target": public_target(self.target), "stage": args[0],
                              "result": "passed" if r.returncode == 0 else "failed",
                              "error": safe_git_message(r.stderr.strip())[-300:] if r.returncode else None}), flush=True)
        return r

    def _ok(self, *args: str, timeout: float = 60) -> str:
        r = self.git(*args, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-400:] or f"git {args[0]} failed")
        return r.stdout

    def check_repo(self) -> bool:
        """Whether the project is the top of its own repository, or in a shared one
        (looked up once per folder; recheck() looks again)."""
        root = self.root_fn().resolve()
        if getattr(self, "_checked", None) != root:
            self._checked, self.own, self.git_dir, self.top, self.prefix = root, None, None, None, ""
        if self.own is None:
            try:
                r = self.git("rev-parse", "--show-toplevel", "--absolute-git-dir", timeout=20)
                lines = r.stdout.strip().splitlines()
                top = Path(lines[0]).resolve() if r.returncode == 0 and len(lines) == 2 else None
                if top is not None and registry.norm(top) == registry.norm(root):
                    self.own = True
                elif top is not None and shared_marker(top):
                    self.own, self.prefix = True, root.relative_to(top).as_posix() + "/"
                else:
                    self.own = False
                if self.own:
                    self.git_dir, self.top = Path(lines[1]), top
                    self.guarded = install_guard(self.git_dir)
            except (OSError, subprocess.SubprocessError, ValueError):
                self.own = False
        return bool(self.own)

    def recheck(self) -> bool:
        """Look for a repository again: one may have been created while the editor runs (the
        Home page's "+ GitHub" says so; git init in a terminal is seen when the GitHub menu
        opens). Not on a timer: a project without a repository costs nothing."""
        with self.lock:
            if not self.own:
                self._checked = None
            if self.check_repo():
                self._safe(self._remote_state)
        return bool(self.own)

    @property
    def shared(self) -> bool:
        return bool(self.check_repo() and self.prefix)

    def rel(self, path: str) -> str:
        """A project-relative path as git sees it (from the repository's top)."""
        return self.prefix + path

    def _pathspec(self) -> str:
        """This project's files only. literal: a folder may be called "a*b"."""
        return f":(top,literal){self.prefix.rstrip('/')}" if self.prefix else "."

    # ------------------------------------------------------------ the switch
    def _settings_file(self) -> Path | None:
        return self.git_dir / "prism-local.json" if self.check_repo() and self.git_dir else None

    def _settings(self) -> dict:
        f = self._settings_file()
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if f else {}
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @property
    def enabled(self) -> bool:
        if not self._settings_file():
            return False
        return bool(self._settings().get("sync", True))

    def set_enabled(self, on: bool) -> None:
        self._update_settings(sync=bool(on))

    def _update_settings(self, **changes) -> None:
        """Change keys of .git/prism-local.json, keeping the others. The editor and the Home
        page both write it: under a file lock, so neither drops what the other wrote."""
        f = self._settings_file()
        if not f:
            return
        with registry.FileLock(f.with_name(f.name + ".lock"), timeout=10):
            write_bytes(f, json.dumps({**self._settings(), **changes}).encode())

    # ------------------------------------------------------------ what changed
    def changes(self) -> list[str]:
        """Paths (from the top) with changes worth recording: no build output, no huge files."""
        out = self._ok("status", "--porcelain", "-z", "-uall", "--", self._pathspec(), timeout=30)
        outdir = self.prefix + self.outdir_fn().strip("/") + "/"
        entries, paths, i = out.split("\0"), [], 0
        while i < len(entries):
            e = entries[i]
            i += 1
            if len(e) < 4:
                continue
            xy, path = e[:2], e[3:]
            if "R" in xy or "C" in xy:            # a rename: the old path follows
                old = entries[i] if i < len(entries) else ""
                i += 1
                if old:
                    paths.append(old)
            if path.startswith(outdir) or path.startswith(".git/"):
                continue
            if xy == "??" and path.lower().endswith(NOT_SOURCES):
                continue
            p = (self.top or self.root_fn()) / path
            try:
                if p.is_file() and p.stat().st_size > MAX_FILE:
                    continue
            except OSError:
                continue
            paths.append(path)
        return sorted(set(paths))

    def commit(self, paths: list[str], message: str) -> bool:
        """Commit exactly `paths` (other changes stay as they are). False: nothing to commit."""
        return bool(paths) and self._record(lambda: paths, lambda _: message)

    def commit_all(self, why: str) -> bool:
        saves = self.saves
        ok = self._record(self.changes, lambda paths: f"{why}: {_names(paths)}")
        # Everything saved is recorded, unless a save came in meanwhile: its autosave stays
        # due (at worst one that finds nothing). A save still writing calls touched() after.
        with self._touch:
            if self.saves == saves:
                self.dirty_since = None
        return ok

    def _record(self, pick: Callable[[], list[str]], message: Callable[[list[str]], str]) -> bool:
        """Commit the paths pick() names, chosen and committed at one hold of the files lock:
        no save lands in between, and git never reads a file a save is replacing. False:
        nothing to commit."""
        with self.lock, repo_lock(self.git_dir), self.writes:
            paths = pick()
            if not paths:
                return False
            msg = message(paths)
            if self.shared:      # the repository has the project's name in every message
                msg = f"{self.root_fn().name}: {msg}"
            self._wait_index()
            self.state = "committing"
            try:
                self._ok("add", "-A", "--", *paths)
                if self.git("diff", "--cached", "--quiet", "--", *paths).returncode == 0:
                    return False
                self._ok("commit", "-q", "-m", msg, "--", *paths)
                h = self._ok("rev-parse", "--short", "HEAD").strip()
                self.last_commit = {"at": time.time(), "message": msg, "hash": h}
                self.error = None
                return True
            finally:
                self.state = "idle"

    def _wait_index(self) -> None:
        """In a shared repository another editor may be committing right now."""
        deadline = time.monotonic() + (LOCK_WAIT if self.shared else 0)
        self._clear_stale_lock()
        while self.git_dir and (self.git_dir / "index.lock").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("git is busy (index.lock); will try again")
            time.sleep(0.2)

    # ------------------------------------------------------------ remote
    def _candidate(self, fresh: bool = False) -> dict:
        """The sync target the repository's configuration names (see _target), looked up
        again only when that configuration changed (_cached), unless `fresh`: everything
        that pushes, fetches or binds asks fresh."""
        if not self.check_repo():
            raise ValueError("This project has no repository to sync")
        if fresh:
            return self._target()
        return dict(_cached("target", self.git_dir, self._target))

    def _target(self) -> dict:
        actual = self.git("rev-parse", "--show-toplevel", "--absolute-git-dir").stdout.strip().splitlines()
        if len(actual) != 2 or registry.norm(Path(actual[0]).resolve()) != registry.norm(self.top) \
                or registry.norm(Path(actual[1]).resolve()) != registry.norm(self.git_dir.resolve()):
            raise ValueError("Project repository changed. Reopen the editor before syncing")
        branch = self.git("symbolic-ref", "--quiet", "HEAD").stdout.strip()
        if not branch.startswith("refs/heads/"):
            raise ValueError("Detached HEAD: select a local branch before syncing")
        name = branch.removeprefix("refs/heads/")
        def values(key):
            return self.git("config", "--get-all", key).stdout.strip().splitlines()
        remotes = self.git("remote").stdout.splitlines()
        upstream = values(f"branch.{name}.remote")
        merges = values(f"branch.{name}.merge")
        if len(upstream) > 1 or len(merges) > 1:
            raise ValueError("Ambiguous upstream configuration")
        remote = upstream[0] if upstream else "origin"
        if remote == "." or remote not in remotes:
            raise ValueError("A unique remote upstream or origin is required")
        branch_ref = merges[0] if merges else branch
        if not branch_ref.startswith("refs/heads/") or self.git("check-ref-format", branch_ref).returncode:
            raise ValueError("Sync requires one valid remote branch")
        for key in (f"branch.{name}.pushRemote", "remote.pushDefault"):
            if any(v != remote for v in values(key)):
                raise ValueError(f"{key} differs from the sync remote {remote}")
        if self.git("config", "--bool", "--get", f"remote.{remote}.mirror").stdout.strip() == "true":
            raise ValueError("Mirror remotes cannot be used for editor sync")
        fetch = self.git("remote", "get-url", "--all", remote).stdout.strip().splitlines()
        push = self.git("remote", "get-url", "--push", "--all", remote).stdout.strip().splitlines()
        if len(fetch) != 1 or len(push) != 1:
            raise ValueError("Sync requires exactly one fetch URL and one push URL")
        identity = repository_identity(fetch[0], self.top)
        if identity != repository_identity(push[0], self.top) or (
                "://" in fetch[0] and not identity.startswith("github.com/") and fetch[0] != push[0]):
            raise ValueError("Fetch and push repositories differ: " + redact_url(push[0]))
        config = self.git("config", "--null", "--get-regexp",
                          r"^(branch\..*\.(remote|merge|pushremote)|remote\..*\.(url|pushurl|fetch|push|mirror)|remote\.pushdefault|url\..*\.(insteadof|pushinsteadof))$").stdout
        # Only relevant selected-remote/branch configuration affects the binding. Other
        # remotes may be added by the author without changing this target.
        entries = [e for e in config.split("\0") if e and (
            e.lower().startswith((f"branch.{name}.".lower(), f"remote.{remote}.".lower(),
                                  "remote.pushdefault", "url.")))]
        fingerprint = hashlib.sha256(json.dumps([branch, sorted(entries)]).encode()).hexdigest()
        return {"remote": remote, "branch_ref": branch_ref, "local_branch": branch,
                "fetch_url": redact_url(fetch[0]), "push_url": redact_url(push[0]), "repository_identity": identity,
                "git_dir": registry.norm(self.git_dir.resolve()),
                "configuration_fingerprint": fingerprint}

    def bind_target(self, expected: dict | None = None) -> None:
        """Called only after explicit menu confirmation, or after creating a known repo."""
        with self.lock:
            candidate = self._candidate(fresh=True)
            if expected is not None and public_target(candidate) != expected:
                raise ValueError("Sync target changed since confirmation; open the menu again")
            self._update_settings(sync_target=candidate)
            self.target, self.blocked_reason = candidate, None

    def validate_target(self, fresh: bool = False) -> bool:
        with self.lock:
            bound = self._settings().get("sync_target")
            self.target = None
            try:
                candidate = self._candidate(fresh)
                self.target = candidate
                # git_dir is the current local location, not the bound remote identity.
                same = isinstance(bound, dict) and all(candidate[k] == bound.get(k) for k in
                                                       ("repository_identity", "branch_ref", "local_branch"))
                if isinstance(bound, dict) and not same:
                    raise ValueError("Sync target changed. Local edits are saved; confirm the new target in the Git menu")
                # No binding yet (a project from before bindings, or a fresh clone): the
                # candidate passed every check above, so it is bound now. The same repository
                # and branch reached another way (ssh instead of https, an upstream set by
                # `git push -u`) is rebound quietly.
                if public_target(candidate) != public_target(bound):
                    self._update_settings(sync_target=candidate)
                self.blocked_reason = None
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                self.blocked_reason = str(exc)
            signature = (self.blocked_reason, json.dumps(public_target(self.target), sort_keys=True))
            if getattr(self, "_target_log", None) != signature:
                self._target_log = signature
                print(json.dumps({"event": "git_target", "project_key": registry.project_key(self.root_fn().resolve()),
                                  "cwd": str(self.root_fn().resolve()), "git_top": str(self.top),
                                  "target": public_target(self.target), "stage": "validation",
                                  "blocked_reason": self.blocked_reason}), flush=True)
            return self.blocked_reason is None

    @property
    def tracking_ref(self) -> str:
        return "refs/remotes/" + self.upstream if self.upstream else "refs/remotes/prism-local/missing"

    def fetch(self, manual: bool = False) -> subprocess.CompletedProcess | None:
        if not self.validate_target(fresh=True) or (not manual and not self.enabled):
            return None
        t = self.target
        self.upstream = f"{t['remote']}/{t['branch_ref'].removeprefix('refs/heads/')}"
        return self.git("fetch", "-q", "--no-tags", "--no-recurse-submodules", "--",
                        t["remote"], f"+{t['branch_ref']}:{self.tracking_ref}", timeout=120,
                        interactive=manual)

    def _remote_state(self) -> None:
        self.validate_target()
        # A target names one of the repository's remotes (_target checked it, and looks
        # again when the configuration changes): only without one is git asked.
        self.has_remote = bool(self.target) or bool(self.git("remote", timeout=20).stdout.strip())
        self.upstream = (f"{self.target['remote']}/{self.target['branch_ref'].removeprefix('refs/heads/')}"
                         if self.target else None)
        if self.upstream and self.git("rev-parse", "--verify", self.tracking_ref).returncode == 0:
            r = self.git("rev-list", "--left-right", "--count", f"HEAD...{self.tracking_ref}", timeout=20)
            try:
                self.ahead, self.behind = (int(x) for x in r.stdout.split())
            except ValueError:
                self.ahead = self.behind = 0
        else:
            n = self.git("rev-list", "--count", "HEAD", timeout=20).stdout.strip()
            self.ahead, self.behind = (int(n) if n.isdigit() else 0), 0

    def push(self, retry: bool = True, tries: int = PUSH_TRIES, manual: bool = False) -> None:
        """Push. If GitHub has commits this copy lacks (a co-author pushed), bring them in
        (pull: a merge, never a rewrite) and push again, as long as GitHub keeps moving on
        (co-authors pushing one after another), up to `tries` times. Never forced."""
        attempts = tries if retry else 1
        with self.lock, repo_lock(self.git_dir):
            for attempt in range(attempts):
                self._remote_state()
                if self.blocked_reason or (not manual and not self.enabled) or not self.has_remote or not self.ahead:
                    return
                self.state = "pushing"
                try:
                    if not self.validate_target(fresh=True):
                        return
                    args = ["push", "-q", "--no-follow-tags", "--recurse-submodules=no", "--",
                            self.target["remote"], f"HEAD:{self.target['branch_ref']}"]
                    r = self.git(*args, timeout=180, interactive=manual)
                    if r.returncode == 0:
                        self.last_push, self.error = time.time(), None
                        self._remote_state()
                        return
                    msg = safe_git_message((r.stderr or r.stdout).strip())
                    # GitHub has commits this copy lacks: GitHub said so, or the guard did
                    # (it stops such a push before GitHub sees it).
                    behind = any(s in msg for s in ("rejected", "fetch first", "non-fast-forward",
                                                    "refusing a push that would drop commits"))
                finally:
                    self.state = "idle"
                if behind and attempt < attempts - 1:
                    self.pull(manual=manual)
                    if self.error:
                        return                   # pull said what is wrong
                    if self.behind and self.busy_fn():
                        return                   # an agent turn: merged and pushed after it
                    time.sleep(min(0.3 * (attempt + 1), 1.5))
                    continue
                self.push_failed_at = time.time()
                self.error = ("GitHub has changes this copy does not have yet; pull first."
                              if behind else "Push failed: " + msg[-300:])
                return

    def pull(self, manual: bool = False) -> None:
        """Fetch, and bring GitHub's commits in. Your changes are committed first. When only
        GitHub moved on, a fast-forward; when both did (co-authors writing at the same time),
        a merge commit that keeps both sides. Nothing is ever rewritten or thrown away: when
        the two sides changed the same lines, the merge is called off, both versions stay
        where they are (GitHub's there, yours committed here), and `error` says which files."""
        with self.lock, repo_lock(self.git_dir):
            self.last_fetch = time.time()
            if not self.has_remote and not self.git("remote", timeout=20).stdout.strip():
                return
            self.state = "pulling"
            try:
                r = self.fetch(manual=manual)
                if r is None:
                    return
                if r.returncode != 0:
                    self.error = "Could not reach GitHub: " + safe_git_message((r.stderr or r.stdout).strip())[-200:]
                    return
                self._remote_state()
                if self.blocked_reason:
                    return
                if self.busy_fn():
                    # An agent turn is running: it reads and writes the files now, and a merge
                    # changing them under it could have its writes drop a co-author's text.
                    # Fetched; merged once the turn ends.
                    return
                healed = self._heal_rewrite()
                if healed is None:               # a rewrite to keep: this copy waits
                    return
                if not self.behind:
                    if self.clash:               # resolved elsewhere (a terminal, a co-author)
                        self.error, self.clash = None, []
                    if healed and self.ahead:
                        self.push(retry=False, manual=manual)   # put the dropped commits back on GitHub now
                    return
                with self.writes:
                    try:
                        if self.busy_fn():       # an agent turn is writing: after it
                            return
                        if self.commit_all("Autosave before pulling"):
                            self._remote_state()
                            if self.blocked_reason:
                                return
                        n = self.behind
                        if not self.ahead:
                            m = self.git("merge", "-q", "--ff-only", self.tracking_ref, timeout=60)
                            if m.returncode == 0:
                                self.notice = f"Pulled {n} commit{'s' if n != 1 else ''} from GitHub"
                                self.error, self.clash = None, []
                            else:
                                self.error = "Could not update from GitHub: " + (m.stderr or m.stdout).strip()[-200:]
                        else:
                            self._merge(n)
                    finally:
                        self._finish_crashed_merge()    # a merge its timeout cut off
                self._remote_state()
                if healed:
                    self.notice = self._heal_note        # says more than "combined"
                if healed and self.ahead and not self.error:
                    self.push(retry=False, manual=manual)       # put the dropped commits back on GitHub now
            finally:
                self.state = "idle"

    def _heal_rewrite(self) -> bool | None:
        """Someone rewrote the branch on GitHub (a force push from a terminal, `--no-verify`
        past the guard): commits it had before are gone from it. Every prism-local copy
        keeps them, so bring them back into this branch (the usual merge with GitHub's new
        history then follows, and the push puts them back on GitHub). Git's log of where
        GitHub's branch has been (the reflog of refs/remotes/...) says what it had, also when
        the Home page fetched in between. `"restore_rewritten": false` in
        .git/prism-local.json leaves a deliberate rewrite alone (removing a file from history)."""
        if not self.upstream:
            return False
        ref = f"refs/remotes/{self.upstream}"
        now = self.git("rev-parse", "-q", "--verify", ref, timeout=20).stdout.strip()
        if not now:
            return False
        was = self.git("reflog", "show", "--format=%H", "-n", "20", ref, timeout=20).stdout.split()
        # Positions it had that are no longer in its history (newest first). Those the last
        # look found in the history of its tip then are still in it while that tip is (a
        # fetch that only added commits): then only the newer positions are looked at.
        seen_ref, seen_tip, kept = getattr(self, "_in_history", (None, None, frozenset()))
        if seen_ref != ref or (seen_tip != now and self.git(
                "merge-base", "--is-ancestor", seen_tip, now, timeout=20).returncode != 0):
            kept = frozenset()
        elif seen_tip != now:
            kept |= {seen_tip}
        lost, ok = [], set()
        for c in dict.fromkeys(was):
            if c == now:
                continue
            if c in kept:
                ok.add(c)
            elif self.git("cat-file", "-e", f"{c}^{{commit}}", timeout=20).returncode == 0:
                if self.git("merge-base", "--is-ancestor", c, now, timeout=20).returncode == 0:
                    ok.add(c)
                else:
                    lost.append(c)
        if not lost:
            self._in_history = (ref, now, frozenset(ok))
            return False
        tip = lost[0]                            # the newest position it had: it holds the rest
        dropped = self.git("rev-list", "--count", f"{now}..{tip}", timeout=20).stdout.strip()
        who = self.git("log", "-1", "--format=%an", now, timeout=20).stdout.strip() or "someone"
        if self._settings().get("restore_rewritten") is False:
            # A rewrite on purpose (a file removed from history): no merge, no push, so as not
            # to bring it back; this copy keeps everything, and waits until it matches GitHub.
            self.notice = (f"{who} rewrote the history on GitHub ({dropped} commits dropped); left as "
                           "it is (restore_rewritten is off)")
            self.error = ("GitHub's history was rewritten on purpose (restore_rewritten is off): this "
                          "copy does not sync until it follows the new history. It keeps everything; "
                          "once you are sure, in a terminal: git reset --keep @{u}")
            return None
        if self.git("merge-base", "--is-ancestor", tip, "HEAD", timeout=20).returncode != 0:
            with self.writes:
                try:
                    # Your edits recorded first, as for any merge: one that gives up over them
                    # would put back the files as they were when it started.
                    self.commit_all("Autosave before pulling")
                    m = self.git("merge", "--no-edit", "-m", "Put back commits dropped from GitHub", tip, timeout=120)
                    if m.returncode != 0 and self.git_dir and (self.git_dir / "MERGE_HEAD").exists():
                        self.git("merge", "--abort", timeout=60)
                finally:
                    self._finish_crashed_merge()    # a merge its timeout cut off
            if m.returncode != 0:
                self.error = ("The history on GitHub was rewritten (a force push) and its old commits "
                              "could not be brought back by themselves: " + (m.stderr or m.stdout).strip()[-200:])
                return False
        self._remote_state()
        self._heal_note = (f"{who} rewrote the history on GitHub (a force push) and dropped {dropped} "
                           f"commit{'s' if dropped != '1' else ''}; they are put back, nothing is lost")
        self.notice = self._heal_note
        return True

    def _clear_stale_lock(self) -> None:
        """A git that crashed (a power cut, a killed process) leaves index.lock behind, and
        every commit fails from then on. One older than STALE_LOCK cannot belong to a git
        still running (they take seconds), so it goes."""
        lock = self.git_dir / "index.lock" if self.git_dir else None
        try:
            if lock and lock.exists() and time.time() - lock.stat().st_mtime > STALE_LOCK:
                lock.unlink()
        except OSError:
            pass

    def _merge(self, n: int, again: bool = True) -> None:
        """Merge GitHub's branch into this one. Where both sides changed the same lines, both
        versions are kept (keepboth.py), so syncing never stops for it. If anything goes
        wrong, the merge is called off and everything is exactly as before."""
        m = self.git("merge", "--no-edit", "-m", "Merge changes from GitHub", self.tracking_ref, timeout=120)
        if m.returncode == 0:
            self.notice = (f"Combined your changes with {n} commit{'s' if n != 1 else ''} from GitHub "
                           "(a co-author's work)")
            self.error, self.clash = None, []
            return
        merging = bool(self.git_dir and (self.git_dir / "MERGE_HEAD").exists())
        if merging:
            try:
                kept = self._keep_both()
                c = self.git("commit", "-q", "-m",
                             "Merge changes from GitHub (both versions kept where both changed the same lines)",
                             timeout=60)
                if c.returncode != 0:
                    raise RuntimeError((c.stderr or c.stdout).strip()[-300:])
            except (OSError, subprocess.SubprocessError, RuntimeError, keepboth.MergeError) as e:
                if self.git_dir and (self.git_dir / "MERGE_HEAD").exists():
                    self.git("merge", "--abort", timeout=60)
                self._report_clash(f" ({e})")
                return
            self.kept = [k for k in self.kept if k["path"] not in {x["path"] for x in kept}] + kept
            where = ", ".join(sorted({k["path"] for k in kept}))
            self.notice = (f"Combined your changes with {n} commit{'s' if n != 1 else ''} from GitHub. "
                           f"You both changed the same lines in {where}: both versions are kept there "
                           "(see the GitHub menu)")
            self.error, self.clash = None, []
            return
        out = m.stderr or m.stdout
        if "would be overwritten" in out and again:
            # Changes not recorded yet (another project of this shared repository, or edits
            # made outside the editor) in files GitHub changed: git touched nothing. Record
            # them, then merge again: they are kept and combined like any other.
            files = [ln.strip() for ln in out.splitlines() if ln.startswith(("\t", "    ")) and ln.strip()]
            if files:
                self._ok("add", "-A", "--", *files)
                self._ok("commit", "-q", "-m", "Autosave before combining with GitHub "
                         "(changed outside this editor)", "--", *files)
                self._merge(n, again=False)
                return
        self.error = "Could not combine your changes with GitHub's: " + out.strip()[-300:]

    def _unmerged(self) -> dict[str, set[int]]:
        """The files a merge stopped at: path (from the top) -> the stages present
        (1 the common version, 2 yours, 3 GitHub's)."""
        out = self.git("ls-files", "-u", "-z", timeout=30).stdout
        files: dict[str, set[int]] = {}
        for entry in out.split("\0"):
            if "\t" not in entry:
                continue
            meta, path = entry.split("\t", 1)
            files.setdefault(path, set()).add(int(meta.split()[2]))
        return files

    def _blob(self, spec: str) -> bytes:
        r = gitenv.git(self.top or self.root_fn(), "show", spec, binary=True)
        if r.returncode != 0:
            raise RuntimeError(f"git show {spec}: " + r.stderr.decode("utf-8", "replace").strip())
        return r.stdout

    def _keep_both(self) -> list[dict]:
        """Settle every file the merge stopped at, losing nothing: text gets both versions;
        a binary file (a figure) keeps yours, with GitHub's saved next to it; a file one side
        deleted and the other changed keeps the changed version."""
        top = self.top or self.root_fn()
        mine = self.git("config", "user.name", timeout=20).stdout.strip() or "you"
        them = self.git("log", "-1", "--format=%an", self.tracking_ref, timeout=20).stdout.strip() or "a co-author"
        if them == mine:
            them = f"{them} (another copy)"
        kept = []
        for path, stages in self._unmerged().items():
            full = top / path
            rel = path[len(self.prefix):] if self.prefix and path.startswith(self.prefix) else path
            if 2 in stages and 3 in stages:
                ours, theirs = self._blob(f":2:{path}"), self._blob(f":3:{path}")
                base = self._blob(f":1:{path}") if 1 in stages else b""
                try:
                    texts = [b.decode("utf-8") for b in (ours, base, theirs)]
                    if any("\0" in t for t in texts):
                        raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary")
                except UnicodeDecodeError:
                    self._ok("checkout", "--ours", "--", path)
                    side = full.with_name(f"{full.stem}.from-{_slug(them)}{full.suffix}")
                    side.write_bytes(theirs)
                    self._ok("add", "--", path, str(side.relative_to(top).as_posix()))
                    kept.append({"path": rel, "line": None, "who": them, "side": side.name,
                                 "mtime": full.stat().st_mtime})
                    continue
                if keepboth.too_big(texts[0], texts[1], texts[2]):
                    # Most of the file clashes (rewritten, reformatted): both inside would double
                    # the paper. Yours stays; GitHub's whole version is saved next to it.
                    self._ok("checkout", "--ours", "--", path)
                    side = full.with_name(f"{full.stem}.from-{_slug(them)}{full.suffix}")
                    side.write_bytes(theirs)
                    self._ok("add", "--", path, str(side.relative_to(top).as_posix()))
                    kept.append({"path": rel, "line": None, "who": them, "side": side.name,
                                 "mtime": full.stat().st_mtime})
                    continue
                text, places = keepboth.keep_both(texts[0], texts[1], texts[2], mine, them, keepboth.is_tex(path))
                crlf = b"\r\n" in ours or (full.exists() and b"\r\n" in full.read_bytes()[:65536])
                full.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
                self._ok("add", "--", path)
                for line in places:
                    kept.append({"path": rel, "line": line, "who": them, "mtime": full.stat().st_mtime})
            elif 2 in stages:                    # GitHub deleted it, you changed it: yours stays
                self._ok("add", "--", path)
            elif 3 in stages:                    # you deleted it, GitHub changed it: theirs stays
                self._ok("checkout", "--theirs", "--", path)
                self._ok("add", "--", path)
            else:
                raise RuntimeError(f"unexpected merge state for {path}")
        if self._unmerged():
            raise RuntimeError("files left unmerged")
        return kept

    def kept_both(self) -> list[dict]:
        """The places where both versions were kept and are still there."""
        top = self.top or self.root_fn()
        still = []
        for k in self.kept:
            f = top / (self.prefix + k["path"])
            try:
                if k["line"] is not None and keepboth.is_tex(k["path"]):
                    if keepboth.marked_lines(f.read_text(encoding="utf-8", errors="replace")):
                        still.append(k)
                elif f.stat().st_mtime == k["mtime"]:        # untouched since: still to look at
                    still.append(k)
            except OSError:
                pass
        # The line numbers move as you edit: report where the markers are now.
        out, seen = [], set()
        for k in still:
            if k["path"] in seen:
                continue
            seen.add(k["path"])
            try:
                lines = keepboth.marked_lines((top / (self.prefix + k["path"])).read_text(
                    encoding="utf-8", errors="replace")) if keepboth.is_tex(k["path"]) else []
            except OSError:
                lines = []
            group = [x for x in still if x["path"] == k["path"]]
            if lines:
                out += [{**group[0], "line": ln} for ln in lines]
            else:
                out += group
        self.kept = [k for k in self.kept if k in still]
        return out

    def _report_clash(self, why: str = "") -> None:
        """The automatic merge could not be done: say so; both versions stay where they are."""
        clash = [x for x in self.git("diff", "--name-only", "HEAD", self.tracking_ref, timeout=30).stdout.splitlines() if x]
        names = [x[len(self.prefix):] if x.startswith(self.prefix) else x for x in clash]
        self.clash = names
        who = self.git("log", "-1", "--format=%an, %ar", self.tracking_ref, timeout=20).stdout.strip()
        self.error = (f"Your changes and GitHub's could not be combined by themselves{why}"
                      f"{f' (last there: {who})' if who else ''}. Nothing was lost: GitHub keeps "
                      "its version, this copy keeps yours (committed). In the GitHub menu: compare "
                      "with GitHub's version, edit the file so it has what both of you want, then "
                      "choose \"I've combined them\".")

    def theirs(self, path: str) -> dict:
        """GitHub's version of a project file, and how it differs from yours."""
        full = self.rel(path)
        content = self.git("show", f"{self.tracking_ref}:{full}", timeout=30)
        diff = self.git("diff", "--no-color", "HEAD", self.tracking_ref, "--", full, timeout=30)
        return {"path": path, "content": content.stdout if content.returncode == 0 else None,
                "diff": diff.stdout}

    def resolve_mine(self) -> None:
        """After a clash, once you have edited the files to hold what both of you want: merge
        GitHub's branch, taking your version of the lines both changed (git merge -X ours:
        everything else of theirs comes in as usual), and push. Their version stays in
        their commits; nothing leaves the history."""
        with self.lock, repo_lock(self.git_dir):
            self.commit_all("Combined with GitHub's version")     # recorded even when offline
            r = self.fetch(manual=True)
            if r is None:
                return
            if r.returncode != 0:
                self.error = "Could not reach GitHub: " + safe_git_message((r.stderr or r.stdout).strip())[-200:]
                return
            self._remote_state()
            if self.blocked_reason:
                return
            if not self.behind:
                self.clash = []
                self.push(manual=True)
                return
            with self.writes:
                try:
                    self.commit_all("Combined with GitHub's version")     # saved during the fetch
                    m = self.git("merge", "--no-edit", "-X", "ours", "-m",
                                 "Merge changes from GitHub (clashing lines: kept the combined version)",
                                 self.tracking_ref, timeout=120)
                    if m.returncode != 0:
                        if self.git_dir and (self.git_dir / "MERGE_HEAD").exists():
                            self.git("merge", "--abort", timeout=60)
                        self.error = "Could not combine: " + (m.stderr or m.stdout).strip()[-300:]
                        return
                finally:
                    self._finish_crashed_merge()        # a merge its timeout cut off
            self.error, self.clash = None, []
            self.notice = "Combined with GitHub's version"
            self.push(manual=True)

    # ------------------------------------------------------------ hooks
    def touched(self) -> None:
        """You saved a file."""
        with self._touch:
            now = time.time()
            self.last_edit = now
            self.saves += 1
            if self.dirty_since is None:
                self.dirty_since = now

    def active(self) -> bool:
        return self.check_repo() and self.enabled

    def before_turn(self) -> None:
        """Before an agent turn: your edits so far get their own commit."""
        if self.active():
            self._safe(lambda: self.commit_all("Your edits before an agent turn"))

    def after_turn(self, paths: list[str], prompt: str) -> None:
        """After an agent turn (or its Undo): commit exactly the files it changed."""
        if not self.active() or not paths:
            return
        # Undo snapshots include ignored files and build output; commits use the
        # same source-file policy as autosave, restricted to this turn's paths.
        changed = {self.rel(p) for p in paths}
        first = next((ln.strip() for ln in prompt.splitlines()
                      if ln.strip() and not ln.startswith("[")), "") or "(no message)"
        first = first if len(first) <= 72 else first[:71] + "…"
        self._safe(lambda: self._record(lambda: [p for p in self.changes() if p in changed],
                                        lambda _: f"Agent: {first}"))

    def _safe(self, fn) -> None:
        try:
            fn()
        except RepoBusy:
            pass                            # another window is at it; the next tick tries again
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, keepboth.MergeError) as e:
            self.error = str(e)[-300:]

    # ------------------------------------------------------------ the worker
    def tick(self) -> None:
        if not self.active():
            return
        now = time.time()
        if self.dirty_since and not self.busy_fn() and (
                now - (self.last_edit or now) >= IDLE or now - self.dirty_since >= MAX_WAIT):
            self._safe(lambda: self.commit_all("Autosave"))
        if now - self.push_failed_at >= RETRY:
            self._safe(self.push)
        if now - self.last_fetch >= FETCH_EVERY and not self.busy_fn() \
                and now - (self.last_edit or 0) >= QUIET:
            self._safe(self.pull)

    def run(self) -> None:
        self._guarded("start", self._start)
        while not self.stop.wait(TICK):
            self._guarded("tick", self.tick)

    def _guarded(self, stage: str, fn) -> None:
        """A failure, or a bug, in one step must not end autosave for the session."""
        try:
            fn()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"[-300:]
            print(json.dumps({"event": "git_sync", "git_top": str(self.top), "stage": stage,
                              "result": "failed", "error": self.error}), flush=True)

    def _start(self) -> None:
        if not self.check_repo():
            return
        self._safe(self._finish_crashed_merge)
        self._safe(self._remote_state)
        # Opening a project brings in what changed on GitHub (another computer) first,
        # before you start editing an old version.
        if self.enabled and self.has_remote:
            self._safe(self.pull)
        # Changes left from before the editor started are recorded like new ones.
        if self.enabled and self.changes():
            self.last_edit = self.dirty_since = time.time()

    def _finish_crashed_merge(self) -> None:
        """A merge prism-local started and never finished (the editor was killed half-way, or
        git by its timeout): call it off, so this copy is as it was before it; the next sync
        merges again. A merge someone started in a terminal is theirs to finish, and is left
        alone."""
        head = self.git_dir / "MERGE_HEAD" if self.git_dir else None
        if not (head and head.exists()):
            return
        try:
            msg = (self.git_dir / "MERGE_MSG").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        if msg.startswith(("Merge changes from GitHub", "Put back commits dropped from GitHub")):
            # Unless a prism-local process is merging right now (it holds repo_lock): another
            # project's editor in the same repository, or this project's last editor still
            # closing. A crashed one holds nothing.
            try:
                with repo_lock(self.git_dir, timeout=0), self.writes:
                    self._clear_stale_lock()
                    self.git("merge", "--abort", timeout=60)
            except RepoBusy:
                pass

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()

    def flush(self) -> None:
        """When the editor closes: commit what is left and push it."""
        self.stop.set()
        if self.active():
            self._safe(lambda: self.commit_all("Autosave"))
            self._safe(self.push)

    def now(self, action: str) -> None:
        """A button: commit everything now and push, or pull. During an agent turn it waits:
        committing would record the agent's half-done writes, merging would change the files
        under it. The turn ends with its own commit, and syncing follows by itself."""
        if self.busy_fn():
            self.notice = "The agent is working: this copy syncs as soon as its turn ends"
            return
        if action == "pull":
            self._safe(lambda: self.pull(manual=True))
        elif action == "resolve":
            self._safe(self.resolve_mine)
        else:
            self._safe(lambda: self.commit_all("Saved"))
            self._safe(lambda: self.push(manual=True))

    # ------------------------------------------------------------ for the editor
    def status(self) -> dict:
        own = self.check_repo()
        # The editor asks every two seconds: while a commit, fetch or push holds the lock
        # (minutes on a slow network), it gets what is known instead of waiting in line.
        if own and self.lock.acquire(blocking=False):
            try:
                self.validate_target()
            finally:
                self.lock.release()
        st = {"own": own, "enabled": own and self.enabled,
              "target": public_target(self.target),
              "bound_target": public_target(self._settings().get("sync_target")) if own else None,
              "blocked_reason": self.blocked_reason,
              "github": github_url(self.target) if own and not self.blocked_reason else None,
              "shared": self.top.name if own and self.prefix else None, "clash": self.clash,
              "kept": self.kept_both() if own and self.kept else [],
              "guarded": bool(getattr(self, "guarded", False)), "state": self.state, "error": self.error,
              "remote": self.has_remote, "upstream": self.upstream, "ahead": self.ahead,
              "behind": self.behind, "last_commit": self.last_commit, "last_push": self.last_push or None,
              "pending": self.dirty_since is not None, "notice": self.notice,
              "next_autosave": (None if self.dirty_since is None else
                                max(0, min((self.last_edit or 0) + IDLE, self.dirty_since + MAX_WAIT) - time.time()))}
        self.notice = None
        return st

    def log(self, path: str, n: int = 60) -> list[dict]:
        """The commits that changed `path` (newest first), following renames."""
        out = self._ok("log", "--follow", f"-n{n}", "--format=%H%x1f%ct%x1f%an%x1f%s", "--",
                       self.rel(path), timeout=30)
        rows = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                rows.append({"hash": parts[0], "at": int(parts[1]), "author": parts[2], "message": parts[3]})
        return rows

    def show(self, rev: str, path: str) -> dict:
        """`path` as it was at `rev`, and what that commit changed in it."""
        if not all(c in "0123456789abcdef" for c in rev.lower()) or not 4 <= len(rev) <= 40:
            raise ValueError("bad revision")
        full = self.rel(path)
        name = self._ok("log", "--follow", "-n1", "--format=", "--name-only", rev, "--", full).strip() or full
        content = self.git("show", f"{rev}:{name}", timeout=30)
        diff = self.git("show", "--format=", "--no-color", rev, "--", name, timeout=30)
        return {"content": content.stdout if content.returncode == 0 else None, "diff": diff.stdout,
                "path": name[len(self.prefix):] if name.startswith(self.prefix) else name}
