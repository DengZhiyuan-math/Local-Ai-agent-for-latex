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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import keepboth
import registry
import gitenv
import sources
from fsutil import write_bytes
from repository import FilesLock, RepoBusy, Repository

IDLE = 120               # seconds without an edit before an autosave commit
MAX_WAIT = 600           # ... but at the latest this long after the first change
FETCH_EVERY = 60         # seconds between fetches from the remote (co-authors' changes arrive soon)
QUIET = 20               # automatic pulls wait this long after your last save (no clash with typing)
PUSH_TRIES = 5           # pushes in a row while co-authors keep pushing in between
MENU_WAIT = 2            # seconds the GitHub menu waits for a push or pull before answering
BIND_WAIT = 10           # ... and confirming a sync target, before "try again"
TICK = 5                 # the worker's period
RETRY = 60               # seconds before a failed push is tried again
SHARED_MARKER = "prism-repo.json"


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
    repo = Repository.find(new)
    if repo is None or repo.top == Path(new).resolve() or not shared_marker(repo.top):
        return None
    specs = [f":(top,literal){Path(p).resolve().relative_to(repo.top).as_posix()}" for p in (old, new)]
    message = f"Rename {Path(old).name} to {Path(new).name}"
    with repo.changing():                    # as an editor's commit
        repo.ok("add", "-A", "--", *specs)
        repo.ok("commit", "-q", "-m", message, "--", *specs)
    return message


@dataclass(frozen=True)
class Outcome:
    """What one sync step did, for whoever asked it: not what another thread did meanwhile
    (the shared `error` may be someone else's)."""
    state: str                    # "synced": GitHub has it; "local": kept here (message: why); "failed"
    message: str | None = None


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
        self.repo: Repository | None = None   # that repository (check_repo)
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
        # What the worker (or a button) changed that the pages should know of, kept with a
        # number: each page shows each notice once, also when several pages ask.
        self._state = threading.Lock()        # notice, kept: the worker writes, requests read
        self._notice: tuple[int, float, str | None] = (0, 0.0, None)   # number, when, text
        self._repo_guard = threading.RLock()  # check_repo, from requests and the worker at once
        self.clash: list[str] = []            # files you and GitHub both changed (project paths)
        self.kept: list[dict] = []            # where both versions were kept: {"path", "line", "who", "mtime"}
        self.stop = threading.Event()

    @property
    def notice(self) -> str | None:
        """The last notice, e.g. "Pulled 2 commits from GitHub" (see status())."""
        return self._notice[2]

    @notice.setter
    def notice(self, text: str | None) -> None:
        if text:
            with self._state:
                self._notice = (self._notice[0] + 1, time.time(), text)

    # ------------------------------------------------------------ git
    def git(self, *args: str, timeout: float = 60, interactive: bool = False) -> subprocess.CompletedProcess:
        """git at the repository's top: paths are relative to it (see rel()). A command that
        changes the index runs only inside _changing() (Repository.git). Only a fetch or push
        you started (manual) may ask you to log in (gitenv.NO_PROMPTS)."""
        if self.repo is None:
            r = gitenv.git(self.root_fn(), *args, timeout=timeout, interactive=interactive)
        else:
            r = self.repo.git(*args, timeout=timeout, interactive=interactive)
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
        with self._repo_guard:      # requests and the worker ask at once; one looks
            return self._check_repo()

    def _check_repo(self) -> bool:
        root = self.root_fn().resolve()
        if getattr(self, "_checked", None) != root:
            self._checked, self.own, self.repo, self.git_dir, self.top, self.prefix = root, None, None, None, None, ""
        if self.own is None:
            try:
                # Its changes hold the editor's save lock (self.writes): saves wait for them.
                repo = Repository.find(root, files=lambda: self.writes)
                top = repo.top if repo else None
                if top is not None and registry.norm(top) == registry.norm(root):
                    self.own = True
                elif top is not None and shared_marker(top):
                    self.own, self.prefix = True, root.relative_to(top).as_posix() + "/"
                else:
                    self.own = False
                if self.own:
                    self.repo, self.git_dir, self.top = repo, repo.git_dir, repo.top
                    self.guarded = install_guard(self.git_dir)
            except (OSError, subprocess.SubprocessError, ValueError):
                self.own = False
        return bool(self.own)

    def _changing(self, files: bool = True, wait: float | None = None):
        """self.repo.changing(): this process alone changes the repository, and with
        `files`, no save writes the files meanwhile (see repository.py)."""
        if not self.check_repo():
            raise RuntimeError("This project has no repository to sync")
        return self.repo.changing(files=files, wait=wait)

    def recheck(self) -> bool:
        """Look for a repository again: one may have been created while the editor runs (the
        Home page's "+ GitHub" says so; git init in a terminal is seen when the GitHub menu
        opens). Not on a timer: a project without a repository costs nothing. While a push
        or pull runs (minutes on a slow network), the menu gets what is known instead."""
        if not self.lock.acquire(timeout=MENU_WAIT):
            return bool(self.own)
        try:
            if not self.own:
                self._checked = None
            if self.check_repo():
                self._safe(self._remote_state)
        finally:
            self.lock.release()
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
        top, outdir = self.top or self.root_fn(), self.prefix + self.outdir_fn().strip("/")
        return sorted({path for xy, path in sources.parse_status(out)
                       if sources.recordable(path, xy == "??", top, outdir)})

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
        with self.lock, self._changing():
            paths = pick()
            if not paths:
                return False
            msg = message(paths)
            if self.shared:      # the repository has the project's name in every message
                msg = f"{self.root_fn().name}: {msg}"
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
        if not self.lock.acquire(timeout=BIND_WAIT):
            raise ValueError("A push or pull is running; try again in a moment")
        try:
            candidate = self._candidate(fresh=True)
            if expected is not None and public_target(candidate) != expected:
                raise ValueError("Sync target changed since confirmation; open the menu again")
            self._update_settings(sync_target=candidate)
            self.target, self.blocked_reason = candidate, None
        finally:
            self.lock.release()

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

    def push(self, retry: bool = True, tries: int = PUSH_TRIES, manual: bool = False) -> Outcome:
        """Push. If GitHub has commits this copy lacks (a co-author pushed), bring them in
        (pull: a merge, never a rewrite) and push again, as long as GitHub keeps moving on
        (co-authors pushing one after another), up to `tries` times. Never forced. Returns
        what this push did (`error` also says so, for the pages)."""
        attempts = tries if retry else 1
        with self.lock, self._changing(files=False):
            for attempt in range(attempts):
                self._remote_state()
                if not self.has_remote:
                    return Outcome("local", "The repository has no remote, so nothing was pushed.")
                if self.blocked_reason:
                    return Outcome("local", "Sync paused: " + self.blocked_reason)
                if not manual and not self.enabled:
                    return Outcome("local", "Automatic sync is off.")
                if not self.ahead:
                    return Outcome("synced")
                self.state = "pushing"
                try:
                    if not self.validate_target(fresh=True):
                        return Outcome("local", "Sync paused: " + str(self.blocked_reason))
                    args = ["push", "-q", "--no-follow-tags", "--recurse-submodules=no", "--",
                            self.target["remote"], f"HEAD:{self.target['branch_ref']}"]
                    r = self.git(*args, timeout=180, interactive=manual)
                    if r.returncode == 0:
                        self.last_push, self.error = time.time(), None
                        self._remote_state()
                        return Outcome("synced")
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
                        return Outcome("failed", self.error)        # pull said what is wrong
                    if self.behind and self.busy_fn():
                        return Outcome("local", "The agent is working: pushed once its turn ends.")
                    time.sleep(min(0.3 * (attempt + 1), 1.5))
                    continue
                self.push_failed_at = time.time()
                self.error = ("GitHub has changes this copy does not have yet; pull first."
                              if behind else "Push failed: " + msg[-300:])
                return Outcome("failed", self.error)
        return Outcome("failed", self.error)

    def record_file(self, path: str, message: str) -> Outcome:
        """Commit one file the editor wrote (an upload, `path` in the project) alone, other
        changes staying as they are, and push it. Returns what happened to it."""
        if not self.check_repo():
            return Outcome("local", "Saved in the project. It has no git repository to sync with "
                                    "(none, or it is inside another one), so nothing was committed.")
        if not self.enabled:
            return Outcome("local", "Saved in the project. Sync with GitHub is off for this project.")
        full = self.rel(path)
        if self.git("check-ignore", "-q", "--", full, timeout=20).returncode == 0:
            folder = path.rsplit("/", 1)[0] + "/" if "/" in path else path
            return Outcome("local", f"Saved in the project. {folder} is in .gitignore, so it is not committed.")
        self.commit([full], message)
        pushed = self.push()
        if pushed.state == "synced":
            return Outcome("synced", f"Committed and pushed to {self.upstream or 'origin'}.")
        if pushed.state == "local":
            return Outcome("local", "Committed. " + str(pushed.message))
        return Outcome("failed", "Committed, but not pushed: " + str(pushed.message))

    def pull(self, manual: bool = False) -> None:
        """Fetch, and bring GitHub's commits in. Your changes are committed first. When only
        GitHub moved on, a fast-forward; when both did (co-authors writing at the same time),
        a merge commit that keeps both sides. Nothing is ever rewritten or thrown away: when
        the two sides changed the same lines, the merge is called off, both versions stay
        where they are (GitHub's there, yours committed here), and `error` says which files."""
        with self.lock, self._changing(files=False):
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
                with self._changing():
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
            with self._changing():
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
            with self._state:
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
        with self._state:
            looked_at = list(self.kept)
        still = []
        for k in looked_at:
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
        # Drop only those looked at and gone: a merge may have added others meanwhile.
        with self._state:
            self.kept = [k for k in self.kept if k in still or k not in looked_at]
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
        with self.lock, self._changing(files=False):
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
            with self._changing():
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
            # Unless a prism-local process is merging right now (it holds the repository): another
            # project's editor in the same repository, or this project's last editor still
            # closing. A crashed one holds nothing.
            try:
                with self._changing(wait=0):
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
              "pending": self.dirty_since is not None,
              "next_autosave": (None if self.dirty_since is None else
                                max(0, min((self.last_edit or 0) + IDLE, self.dirty_since + MAX_WAIT) - time.time()))}
        # Kept, not taken by the first who asks: each page shows each notice (by its number)
        # once, also when several ask (the tree's poll, a button's answer, another tab).
        number, at, text = self._notice
        st.update(notice=text, notice_id=number, notice_age=time.time() - at if number else None)
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
