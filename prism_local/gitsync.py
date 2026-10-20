"""Record and sync the project's changes with its GitHub repository.

While the editor runs, a worker thread keeps the project's own git repository up to date:

- autosave commits: once you stop editing for IDLE seconds (or at the latest MAX_WAIT
  seconds after the first unsaved change), everything that changed is committed;
- agent turns: the edits you made before a turn are committed first, then the files the
  turn changed are committed alone, with your request as the message (an Undo of the
  turn is committed the same way);
- push after every commit, and fetch every FETCH_EVERY seconds: a remote that is ahead
  (another computer, GitHub's web editor, a co-author) is brought in: a fast-forward, or a
  merge that keeps both versions where both changed the same lines (integrate.py), so
  the editor reloads the files. Nothing is ever forced or thrown away.

Build output (the build folder, .aux, .log, ...) is never committed. Sync can be switched
off per clone (.git/prism-local.json, never committed).

GitSync is what the editor and the Home page call; what it decides with lives apart:
- synctarget.py: where the repository syncs to, and the target its author confirmed;
- repository.py: where a project's folder is in git (locate), and its locks;
- repoview.py: what changed in it, as the editor, the Home page and the commits count it;
- integrate.py: every merge, losing nothing;
- syncschedule.py: when to commit, push and pull, on a clock it is given.

A project is synced when it is the top of its own repository, or when it sits in a
repository shared by several projects: one whose top has a SHARED_MARKER file (the Home
page writes it when a folder of projects gets one repository). There each editor commits
only its own project's files, and pushes and pulls the shared branch. Nothing happens in
a folder inside any other repository (prism-local's examples, say).
"""
from __future__ import annotations

import functools
import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import integrate
import keepboth
import registry
import gitenv
import repoview
import diskwatch
from errors import say
from repository import SHARED_MARKER, FilesLock, Place, RepoBusy, Repository, locate, refs_stamp, shared_marker  # noqa: F401
from synctarget import (RepoSettings, cached, github_url, public_target, redact_url,  # noqa: F401
                        repository_identity, safe_git_message)
import synctarget
from syncschedule import FETCH_EVERY, IDLE, MAX_WAIT, QUIET, RETRY, Schedule  # noqa: F401

PUSH_TRIES = 5           # pushes in a row while co-authors keep pushing in between
MENU_WAIT = 2            # seconds the GitHub menu waits for a push or pull before answering
BIND_WAIT = 10           # ... and confirming a sync target, before "try again"
TICK = 5                 # the worker's period


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


def install_guard(git_dir: Path) -> bool:
    """Put GUARD in the repository's hooks, unless it has a pre-push hook of its own or
    uses a hooks folder of its own (core.hooksPath). True when the guard is in place."""
    hook = cached("hooks", git_dir, lambda: _hook_path(git_dir))
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


def record_move(old: Path, new: Path) -> str | None:
    """A project's folder was renamed inside a repository shared by several projects:
    commit the move there (its own repository needs nothing: its paths do not change).
    Returns the commit message; None when there is nothing to record. Raises RuntimeError
    (RepoBusy too) when the move could not be committed: say so, since nobody else would
    commit it (each editor of the repository commits its own project only), and the other
    copies would get the new folder next to the old one."""
    place = locate(new)
    if place.kind != "shared":
        return None
    repo = place.repo
    specs = [f":(top,literal){Path(p).resolve().relative_to(repo.top).as_posix()}" for p in (old, new)]
    message = f"Rename {Path(old).name} to {Path(new).name}"
    with repo.changing():                    # as an editor's commit
        repo.ok("add", "-A", "--", *specs)
        repo.ok("commit", "-q", "-m", message, "--", *specs)
    return message


@dataclass(frozen=True)
class Outcome:
    """What one sync step (a push, a pull, a resolve, a button) did, for whoever asked it:
    not what another thread did meanwhile (status()'s `error` is the last error any step
    met, maybe another's)."""
    state: str                    # "synced": GitHub has it; "local": kept here (message: why); "failed"
    message: str | None = None

    @property
    def error(self) -> str | None:
        """What went wrong; None unless the step failed."""
        return self.message if self.state == "failed" else None


class _Looking(RepoSettings):
    """The repository's settings as status() reads them: its look at the target binds
    and repairs nothing (validate_target does, in the worker or at a button)."""

    def bind(self, target: dict) -> None:
        pass

    def _repair(self, bound: dict) -> list[str]:
        return []


UNREACHABLE = "Could not reach GitHub. Check the internet connection; Prism tries again by itself.\n"


def said(error: str | None) -> str | None:
    """An error's plain words: its first line (see GitSync._failed)."""
    return error.split("\n", 1)[0] if error else error


def details(error: str | None) -> str | None:
    """What an error says for those who want to know (git's message): after its first line."""
    return (error.split("\n", 1)[1].strip() or None) if error and "\n" in error else None


def _names(paths: list[str]) -> str:
    names = [p.split("/")[-1] for p in paths]
    return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def one_at_a_time(method):
    """A push or pull: one at a time in this process (GitSync.syncing)."""
    @functools.wraps(method)
    def run(self, *args, **kw):
        with self.syncing:
            return method(self, *args, **kw)
    return run


class GitSync:
    def __init__(self, root_fn: Callable[[], Path], outdir_fn: Callable[[], str],
                 busy: Callable[[], bool] = lambda: False, writes=None, clock: Callable[[], float] = time.time):
        self.root_fn, self.outdir_fn, self.busy_fn = root_fn, outdir_fn, busy
        self.lock = threading.RLock()         # one git command sequence at a time
        # One push or pull at a time in this process, its transfer included (the timer's
        # and a button's would otherwise slip between each other's tries while a co-author
        # pushes). Commits do not take it: your edits before an agent's turn never wait
        # for a transfer. Across processes, Repository.transferring.
        self.syncing = threading.RLock()
        # Held while git reads or changes the files: a commit, with the look at what changed;
        # a merge, from the commit before it to its end (FilesLock). The editor's saves and
        # an Undo take it too (server.SAVE_LOCK).
        self.writes = writes or FilesLock(lambda: self.git_dir if self.check_repo() else None)
        self.own: bool | None = None          # the project has a repository to sync with
        self.repo: Repository | None = None   # that repository (check_repo)
        self.git_dir: Path | None = None
        self.top: Path | None = None          # that repository's top: the project, or above it
        self.prefix = ""                      # the project's path in it: "" or "ex2/"
        self.schedule = Schedule(clock)       # when to commit, push and pull (syncschedule.py)
        self.last_commit: dict | None = None  # {"at", "message", "hash"}
        self.last_push = 0.0
        self.ahead = self.behind = 0
        self.has_remote = False
        self._remote_stamp = None             # _remote_state's answers are for this (refs_stamp)
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
        self.source_events = None
        self.source_generation = 0

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
                place = locate(root, files=lambda: self.writes)
                self.own = place.syncs
                if self.own:
                    self.repo, self.git_dir, self.top, self.prefix = place.repo, place.repo.git_dir, place.repo.top, place.prefix
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

    # ------------------------------------------------------------ the switch
    @property
    def settings(self) -> RepoSettings | None:
        """.git/prism-local.json (synctarget.RepoSettings); None without a repository."""
        return RepoSettings(self.repo) if self.check_repo() and self.repo else None

    @property
    def enabled(self) -> bool:
        return bool(self.settings and self.settings.enabled)

    def set_enabled(self, on: bool) -> None:
        if self.settings:
            self.settings.update(sync=bool(on))

    # ------------------------------------------------------------ what changed
    def changes(self) -> list[str]:
        """Paths (from the top) with changes worth recording: no build output, no huge files
        (repoview.Changes.recordable). Asked of git now: a commit follows."""
        if not self.check_repo():
            raise RuntimeError("This project has no repository to sync")
        place = Place("shared" if self.prefix else "own", self.repo, self.prefix)
        return repoview.status(place).recordable(self.outdir_fn())

    def commit(self, paths: list[str], message: str) -> bool:
        """Commit exactly `paths` (other changes stay as they are). False: nothing to commit."""
        return bool(paths) and self._record(lambda: paths, lambda _: message)

    def commit_all(self, why: str) -> bool:
        mark = self.schedule.mark()
        ok = self._record(self.changes, lambda paths: f"{why}: {_names(paths)}")
        self.schedule.recorded(mark)        # unless a save came in meanwhile
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
    def candidate(self, fresh: bool = False) -> dict:
        """The sync target the repository's configuration names (synctarget.resolve)."""
        if not self.check_repo():
            raise ValueError("This project has no repository to sync")
        return synctarget.resolve(self.repo, fresh)

    def bind_target(self, expected: dict | None = None) -> None:
        """Called only after explicit menu confirmation, or after creating a known repo."""
        if not self.lock.acquire(timeout=BIND_WAIT):
            raise ValueError("A push or pull is running; try again in a moment")
        try:
            candidate = self.candidate(fresh=True)
            if expected is not None and public_target(candidate) != expected:
                raise ValueError("Sync target changed since confirmation; open the menu again")
            self.settings.bind(candidate)
            self.target, self.blocked_reason = candidate, None
        finally:
            self.lock.release()

    def validate_target(self, fresh: bool = False) -> bool:
        with self.lock:
            if self.check_repo():
                settings = self.settings
                self.target, self.blocked_reason = settings.validate(fresh)
                if settings.repaired:
                    # A repository Prism set up, which another program or a terminal changed.
                    self.notice = ("Prism put back this project's connection to GitHub, which had been "
                                   "changed outside Prism" + ("" if self.blocked_reason is None else
                                                              ", but it still cannot sync"))
                    print(json.dumps({"event": "git_target", "git_top": str(self.top), "stage": "repair",
                                      "repaired": settings.repaired}), flush=True)
            else:
                self.target, self.blocked_reason = None, "This project has no repository to sync"
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

    def fetch(self, manual: bool = False, wait: float | None = None) -> subprocess.CompletedProcess | None:
        """Fetch GitHub's branch into the tracking ref; None when there is nothing to fetch
        from. The transfer holds only the repository's network lock (Repository.transferring:
        RepoBusy after `wait` seconds): commits, merges and saves go on meanwhile."""
        if not self.validate_target() or (not manual and not self.enabled):
            return None
        with self.repo.transferring(wait):
            t = self._checked_target()
            if t is None:
                return None
            self.upstream = f"{t['remote']}/{t['branch_ref'].removeprefix('refs/heads/')}"
            return self.git("fetch", "-q", "--no-tags", "--no-recurse-submodules", "--",
                            t["remote"], f"+{t['branch_ref']}:{self.tracking_ref}", timeout=120,
                            interactive=manual)

    def _checked_target(self) -> dict | None:
        """The sync target as the configuration names it now, checked (validate_target,
        read afresh), or None when it may not be synced with (blocked_reason says why).
        Asked holding the network lock, right before a fetch or push: the wait for that
        lock lasts as long as another window's transfer (minutes on a slow network), and
        git reads the configuration when it starts, so a change made meanwhile (another
        program, a terminal) is seen here, not sent to."""
        return dict(self.target) if self.validate_target(fresh=True) else None

    def _remote_state(self) -> None:
        self.validate_target()
        # git is asked again only when the branches, the remotes or the configuration
        # changed on the disk, or the target did: an idle push check every few seconds
        # then runs no git at all.
        stamp = refs_stamp(self.git_dir)
        key = None if stamp is None else (stamp, json.dumps(self.target, sort_keys=True, default=str))
        if key is not None and key == self._remote_stamp:
            return
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
        self._remote_stamp = key

    @one_at_a_time
    def push(self, retry: bool = True, tries: int = PUSH_TRIES, manual: bool = False) -> Outcome:
        """Push. If GitHub has commits this copy lacks (a co-author pushed), bring them in
        (pull: a merge, never a rewrite) and push again, as long as GitHub keeps moving on
        (co-authors pushing one after another), up to `tries` times. Never forced. Returns
        what this push did (a failure is kept for the pages too). The transfer holds only
        the network lock: a commit (your edits before an agent's turn) does not wait for it."""
        if not self.check_repo():
            raise RuntimeError("This project has no repository to sync")
        attempts = max(1, tries) if retry else 1
        for attempt in range(attempts):
            with self.lock:
                self._remote_state()
                if not self.has_remote:
                    return Outcome("local", "This project is not on GitHub yet, so nothing was uploaded.")
                if self.blocked_reason:
                    return Outcome("local", str(synctarget.plain(self.blocked_reason)))
                if not manual and not self.enabled:
                    return Outcome("local", "Automatic sync is off.")
                if not self.ahead:
                    return Outcome("synced")
            self.state = "pushing"
            try:
                with self.repo.transferring():
                    t = self._checked_target()
                    if t is None:
                        return Outcome("local", str(synctarget.plain(self.blocked_reason)))
                    r = self.git("push", "-q", "--no-follow-tags", "--recurse-submodules=no", "--",
                                 t["remote"], f"HEAD:{t['branch_ref']}", timeout=180, interactive=manual)
            finally:
                self.state = "idle"
            if r.returncode == 0:
                with self.lock:
                    self.last_push, self.error = time.time(), None
                    self._remote_state()
                return Outcome("synced")
            msg = safe_git_message((r.stderr or r.stdout).strip())
            # GitHub has commits this copy lacks: GitHub said so, or the guard did (it stops
            # such a push before GitHub sees it).
            behind = any(s in msg for s in ("rejected", "fetch first", "non-fast-forward",
                                            "refusing a push that would drop commits"))
            if behind and attempt < attempts - 1:
                # Only what this pull says counts, not an earlier error ("Could not reach
                # GitHub", an hour ago) still shown on the pages.
                pulled = self.pull(manual=manual)
                if pulled.error:
                    return pulled                               # pull said what is wrong
                if self.behind and self.busy_fn():
                    return Outcome("local", "The agent is working: pushed once its turn ends.")
                time.sleep(min(0.3 * (attempt + 1), 1.5))
                continue
            with self.lock:
                self.schedule.push_failed()
                return self._failed("GitHub has newer changes, from a co-author, that kept arriving "
                                    "while Prism brought them in; it tries again by itself.\n" + msg[-300:]
                                    if behind else "Could not upload to GitHub.\n" + msg[-300:])

    def _failed(self, message: str) -> Outcome:
        """A step's failure: returned to whoever asked, and kept for the pages (status()'s
        `error`, until a step succeeds). Its first line is said in plain words; what follows
        (git's own message) is shown under Details (said, details)."""
        self.error = message
        return Outcome("failed", message)

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
            return Outcome("local", "Saved in the project's history. " + str(pushed.message))
        return Outcome("failed", "Saved in the project's history, but not uploaded: " + str(pushed.message))

    @one_at_a_time
    def pull(self, manual: bool = False) -> Outcome:
        """Fetch, and bring GitHub's commits in. Your changes are committed first. When only
        GitHub moved on, a fast-forward; when both did (co-authors writing at the same time),
        a merge commit that keeps both sides. Nothing is ever rewritten or thrown away: when
        the two sides changed the same lines, the merge is called off, both versions stay
        where they are (GitHub's there, yours committed here), and the error says which files.
        Returns what this pull did (a failure is kept for the pages too). The fetch holds
        only the network lock; the merge holds the repository and the files."""
        if not self.check_repo():
            raise RuntimeError("This project has no repository to sync")
        self.schedule.fetched()
        if not self.has_remote and not self.git("remote", timeout=20).stdout.strip():
            return Outcome("local", "This project is not on GitHub yet, so there was nothing to get.")
        self.state = "pulling"
        try:
            r = self.fetch(manual=manual)
            if r is None:
                return Outcome("local", str(synctarget.plain(self.blocked_reason)) if self.blocked_reason
                               else "Automatic sync is off.")
            if r.returncode != 0:
                return self._failed(UNREACHABLE + safe_git_message((r.stderr or r.stdout).strip())[-200:])
            took, push_after = self._take_in()
        finally:
            self.state = "idle"
        if push_after:
            return self.push(retry=False, manual=manual)    # put the dropped commits back on GitHub now
        return took

    def _take_in(self) -> tuple[Outcome, bool]:
        """After a fetch: merge what it brought (see pull). Returns what it did, and whether
        this copy has commits a rewrite dropped from GitHub, to push back now (after the
        locks are let go)."""
        with self.lock, self._changing(files=False):
            self._remote_state()
            if self.blocked_reason:
                return Outcome("local", str(synctarget.plain(self.blocked_reason))), False
            if self.busy_fn():
                # An agent turn is running: it reads and writes the files now, and a merge
                # changing them under it could have its writes drop a co-author's text.
                # Fetched; merged once the turn ends.
                return Outcome("local", "The agent is working: GitHub's changes come in once its turn ends."), False
            healed, error = self._heal_rewrite()
            if healed is None:               # a rewrite to keep: this copy waits
                return self._failed(error), False
            if not self.behind:
                if self.clash:               # resolved elsewhere (a terminal, a co-author)
                    self.error, self.clash = None, []
            else:
                with self._changing():
                    try:
                        if self.busy_fn():       # an agent turn is writing: after it
                            return Outcome("local", "The agent is working: GitHub's changes come in once its turn ends."), False
                        if self.commit_all("Autosave before pulling"):
                            self._remote_state()
                            if self.blocked_reason:
                                return Outcome("local", str(synctarget.plain(self.blocked_reason))), False
                        n = self.behind
                        if not self.ahead:
                            m = self.git("merge", "-q", "--ff-only", self.tracking_ref, timeout=60)
                            if m.returncode == 0:
                                self.notice = f"Brought in {n} change{'s' if n != 1 else ''} from GitHub"
                                self.error, self.clash = None, []
                            else:
                                error = "Could not bring in the changes from GitHub.\n" + (m.stderr or m.stdout).strip()[-200:]
                        else:
                            error = self._merge(n) or error
                    finally:
                        self._finish_crashed_merge()    # a merge its timeout cut off
                self._remote_state()
                if healed:
                    self.notice = self._heal_note    # says more than "combined"
            if error:
                return self._failed(error), False
            return Outcome("synced"), bool(healed and self.ahead)

    def _heal_rewrite(self) -> tuple[bool | None, str | None]:
        """Someone rewrote the branch on GitHub (a force push from a terminal, `--no-verify`
        past the guard): commits it had before are gone from it. Every prism-local copy
        keeps them, so bring them back into this branch (the usual merge with GitHub's new
        history then follows, and the push puts them back on GitHub). Git's log of where
        GitHub's branch has been (the reflog of refs/remotes/...) says what it had, also when
        the Home page fetched in between. `"restore_rewritten": false` in
        .git/prism-local.json leaves a deliberate rewrite alone (removing a file from history).
        Returns (healed, error): True when they are put back; False when nothing was dropped,
        or what was could not be put back (the error says why); None when this copy waits."""
        if not self.upstream:
            return False, None
        ref = f"refs/remotes/{self.upstream}"
        now = self.git("rev-parse", "-q", "--verify", ref, timeout=20).stdout.strip()
        if not now:
            return False, None
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
            return False, None
        tip = lost[0]                            # the newest position it had: it holds the rest
        dropped = self.git("rev-list", "--count", f"{now}..{tip}", timeout=20).stdout.strip()
        who = self.git("log", "-1", "--format=%an", now, timeout=20).stdout.strip() or "someone"
        if self.settings.read().get("restore_rewritten") is False:
            # A rewrite on purpose (a file removed from history): no merge, no push, so as not
            # to bring it back; this copy keeps everything, and waits until it matches GitHub.
            self.notice = (f"{who} replaced the history on GitHub, dropping {dropped} saved "
                           f"change{'s' if dropped != '1' else ''}; this project is set to leave that alone")
            return None, ("Someone replaced the history on GitHub, and this project is set to leave that "
                          "alone: it does not sync until it follows the new history. Everything is kept "
                          "here.\nrestore_rewritten is off in .git/prism-local.json. Once you are sure, "
                          "in a terminal: git reset --keep @{u}")
        if self.git("merge-base", "--is-ancestor", tip, "HEAD", timeout=20).returncode != 0:
            with self._changing():
                try:
                    # Your edits recorded first, as for any merge: one that gives up over them
                    # would put back the files as they were when it started.
                    self.commit_all("Autosave before pulling")
                    failed = integrate.put_back(self.repo, tip)
                finally:
                    self._finish_crashed_merge()    # a merge its timeout cut off
            if failed:
                return False, ("Someone replaced the history on GitHub, and Prism could not put back "
                               "what it dropped by itself. Everything is kept here.\n" + failed)
        self._remote_state()
        self._heal_note = (f"{who} replaced the history on GitHub, dropping {dropped} saved "
                           f"change{'s' if dropped != '1' else ''}; Prism put them back, nothing is lost")
        self.notice = self._heal_note
        return True, None

    def _merge(self, n: int) -> str | None:
        """Merge GitHub's branch into this one, losing nothing (integrate.merge). Returns
        why it could not."""
        return self._apply(integrate.merge(self.repo, self.tracking_ref, n, self.prefix))

    def _apply(self, r: integrate.Result) -> str | None:
        """What a merge did, for the pages; returns its error."""
        if r.kept:
            with self._state:
                paths = {k["path"] for k in r.kept}
                self.kept = [k for k in self.kept if k["path"] not in paths] + r.kept
        if r.notice:
            self.notice = r.notice
        self.error = r.error
        if r.clash is not None:
            self.clash = r.clash
        return r.error

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

    def theirs(self, path: str) -> dict:
        """GitHub's version of a project file, and how it differs from yours."""
        full = self.rel(path)
        content = self.git("show", f"{self.tracking_ref}:{full}", timeout=30)
        diff = self.git("diff", "--no-color", "HEAD", self.tracking_ref, "--", full, timeout=30)
        return {"path": path, "content": content.stdout if content.returncode == 0 else None,
                "diff": diff.stdout}

    @one_at_a_time
    def resolve_mine(self) -> Outcome:
        """After a clash, once you have edited the files to hold what both of you want: merge
        GitHub's branch, taking your version of the lines both changed (integrate.combine_mine),
        and push. Their version stays in their commits; nothing leaves the history. Returns
        what it did (a failure is kept for the pages too)."""
        self.commit_all("Combined with GitHub's version")     # recorded even when offline
        r = self.fetch(manual=True)
        if r is None:
            return Outcome("local", str(synctarget.plain(self.blocked_reason)))
        if r.returncode != 0:
            return self._failed(UNREACHABLE + safe_git_message((r.stderr or r.stdout).strip())[-200:])
        with self.lock, self._changing(files=False):
            self._remote_state()
            if self.blocked_reason:
                return Outcome("local", str(synctarget.plain(self.blocked_reason)))
            if not self.behind:
                self.clash = []
            else:
                with self._changing():
                    try:
                        self.commit_all("Combined with GitHub's version")     # saved during the fetch
                        failed = integrate.combine_mine(self.repo, self.tracking_ref)
                        if failed:
                            return self._failed(failed)
                    finally:
                        self._finish_crashed_merge()        # a merge its timeout cut off
                self.error, self.clash = None, []
                self.notice = "Combined with GitHub's version"
        return self.push(manual=True)

    # ------------------------------------------------------------ hooks
    def touched(self) -> None:
        """You saved a file."""
        self.schedule.edited()

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
        def record_turn():
            with self.lock, self._changing():
                mark = self.schedule.mark()
                self._record(lambda: [p for p in self.changes() if p in changed],
                             lambda _: f"Agent: {first}")
                if not self.changes():
                    self.schedule.recorded(mark)
        self._safe(record_turn)

    def _safe(self, fn):
        """fn(), what it returns; a failure is kept for the pages and returned as an Outcome."""
        try:
            return fn()
        except RepoBusy:
            # another window is at it; the next tick tries again
            return Outcome("local", "Another editor of this repository is syncing it.")
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, keepboth.MergeError) as e:
            return self._failed(say(e)[-300:])

    # ------------------------------------------------------------ the worker
    def tick(self) -> None:
        if not self.active():
            return
        if self.source_events is not None:
            generation = self.source_events.stamp()
            if generation != self.source_generation:
                # Notifications may arrive after an agent committed the write, or
                # describe generated/ignored files. Only actual source changes
                # need autosave; a clean tree must not restart its countdown.
                with self.lock, self.writes:
                    mark = self.schedule.mark()
                    if self.changes():
                        self.touched()
                    else:
                        self.schedule.recorded(mark)
                    self.source_generation = generation
        steps = {"commit": lambda: self.commit_all("Autosave"), "push": self.push, "pull": self.pull}
        for step in self.schedule.due(self.busy_fn()):
            self._safe(steps[step])

    def run(self) -> None:
        # Browser saves call touched(), but files written by another editor or an
        # external Codex session do not pass through Project.save. Native events
        # feed those edits into the same schedule, without scanning on every tick.
        root = self.root_fn()
        self.source_events = diskwatch.Changes(excluded=(root / self.outdir_fn(),))
        try:
            self.source_events.configure([root])
            self._guarded("start", self._start)
            while not self.stop.wait(TICK):
                self._guarded("tick", self.tick)
        finally:
            self.source_events.close()
            self.source_events = None

    def _guarded(self, stage: str, fn) -> None:
        """A failure, or a bug, in one step must not end autosave for the session."""
        try:
            fn()
        except Exception as e:
            self.error = ("Something went wrong while syncing; Prism tries again by itself.\n"
                          + f"{type(e).__name__}: {e}"[-300:])
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
            self.schedule.leftover()

    def _finish_crashed_merge(self) -> None:
        """A merge prism-local left half-done is called off (integrate.finish_crashed)."""
        if self.check_repo():
            integrate.finish_crashed(self.repo)

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()

    def flush(self) -> None:
        """When the editor closes: commit what is left and push it."""
        self.stop.set()
        if self.active():
            self._safe(lambda: self.commit_all("Autosave"))
            self._safe(self.push)

    def now(self, action: str) -> Outcome:
        """A button: commit everything now and push, or pull. During an agent turn it waits:
        committing would record the agent's half-done writes, merging would change the files
        under it. The turn ends with its own commit, and syncing follows by itself. Returns
        what this click did: by the time the page asks status(), its `error` may be the
        worker's."""
        if self.busy_fn():
            self.notice = "The agent is working: this copy syncs as soon as its turn ends"
            return Outcome("local", self.notice)
        if action == "pull":
            return self._safe(lambda: self.pull(manual=True))
        if action == "resolve":
            return self._safe(self.resolve_mine)
        self._safe(lambda: self.commit_all("Saved"))
        return self._safe(lambda: self.push(manual=True))

    # ------------------------------------------------------------ for the editor
    def status(self) -> dict:
        """What the pages show: what the worker and the buttons last recorded (`error`: the
        last error a step met), and a look at the sync target. It changes nothing: a target
        not bound yet is bound by the worker's next step (validate_target), not by a page
        asking every two seconds."""
        own = self.check_repo()
        target, blocked = self.target, self.blocked_reason
        # The editor asks every two seconds: while a commit, fetch or push holds the lock
        # (minutes on a slow network), it gets what is known instead of waiting in line.
        if own and self.lock.acquire(blocking=False):
            try:
                target, blocked = _Looking(self.repo).validate()
            finally:
                self.lock.release()
        st = {"own": own, "enabled": own and self.enabled,
              "target": public_target(target),
              "bound_target": public_target(self.settings.read().get("sync_target")) if own else None,
              "blocked_reason": blocked, "blocked_plain": synctarget.plain(blocked),
              "github": github_url(target) if own and not blocked else None,
              "shared": self.top.name if own and self.prefix else None, "clash": self.clash,
              "kept": self.kept_both() if own and self.kept else [],
              "guarded": bool(getattr(self, "guarded", False)), "state": self.state,
              "error": said(self.error), "error_details": details(self.error),
              "remote": self.has_remote, "upstream": self.upstream, "ahead": self.ahead,
              "behind": self.behind, "last_commit": self.last_commit, "last_push": self.last_push or None,
              "external_watch": bool(self.source_events and self.source_events.watches),
              "pending": self.schedule.pending, "next_autosave": self.schedule.next_autosave()}
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
