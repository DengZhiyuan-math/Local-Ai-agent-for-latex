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
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import keepboth
from proc import NO_WINDOW

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
    try:
        r = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=git_dir, capture_output=True,
                           text=True, timeout=20, **NO_WINDOW)
        if r.stdout.strip():
            return False
        hook = Path(git_dir) / "hooks" / "pre-push"
        if hook.exists():
            return GUARD_MARK in hook.read_text(encoding="utf-8", errors="replace")
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_bytes(GUARD.encode("utf-8"))      # \n line ends: sh reads it on Windows too
        hook.chmod(0o755)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def shared_marker(top: Path) -> bool:
    """Whether `top` is the top of a repository shared by several projects."""
    try:
        return bool(json.loads((top / SHARED_MARKER).read_text(encoding="utf-8")).get("shared"))
    except (OSError, ValueError, AttributeError):
        return False


def record_move(old: Path, new: Path) -> str | None:
    """A project's folder was renamed inside a repository shared by several projects:
    commit the move there (its own repository needs nothing: its paths do not change).
    Returns the commit message, or None."""
    def git(*args, cwd=new):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                              **NO_WINDOW)
    try:
        r = git("rev-parse", "--show-toplevel")
        if r.returncode != 0:
            return None
        top = Path(r.stdout.strip()).resolve()
        if top == Path(new).resolve() or not shared_marker(top):
            return None
        specs = [f":(top,literal){Path(p).resolve().relative_to(top).as_posix()}" for p in (old, new)]
        git("add", "-A", "--", *specs, cwd=top)
        message = f"Rename {Path(old).name} to {Path(new).name}"
        if git("commit", "-q", "-m", message, "--", *specs, cwd=top).returncode != 0:
            return None
        return message
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name).strip("-")[:30] or "github"


def _names(paths: list[str]) -> str:
    names = [p.split("/")[-1] for p in paths]
    return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


class GitSync:
    def __init__(self, root_fn: Callable[[], Path], outdir_fn: Callable[[], str],
                 busy: Callable[[], bool] = lambda: False):
        self.root_fn, self.outdir_fn, self.busy_fn = root_fn, outdir_fn, busy
        self.lock = threading.RLock()         # one git command sequence at a time
        self.own: bool | None = None          # the project has a repository to sync with
        self.git_dir: Path | None = None
        self.top: Path | None = None          # that repository's top: the project, or above it
        self.prefix = ""                      # the project's path in it: "" or "ex2/"
        self.last_edit = self.dirty_since = None
        self.last_commit: dict | None = None  # {"at", "message", "hash"}
        self.last_push = self.last_fetch = 0.0
        self.push_failed_at = 0.0
        self.ahead = self.behind = 0
        self.has_remote = False
        self.upstream: str | None = None
        self.state = "idle"                   # idle | committing | pushing | pulling
        self.error: str | None = None
        self.notice: str | None = None        # e.g. "pulled 2 commits from GitHub"
        self.clash: list[str] = []            # files you and GitHub both changed (project paths)
        self.kept: list[dict] = []            # where both versions were kept: {"path", "line", "who", "mtime"}
        self.stop = threading.Event()

    # ------------------------------------------------------------ git
    def git(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        """git at the repository's top: paths are relative to it (see rel())."""
        return subprocess.run(["git", *args], cwd=self.top or self.root_fn(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **NO_WINDOW)

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
                if top == root:
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
        f = self._settings_file()
        if f:
            f.write_text(json.dumps({**self._settings(), "sync": bool(on)}), encoding="utf-8")

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
            if xy == "??" and path.lower().endswith(AUX):
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
        if not paths:
            return False
        if self.shared:      # the repository has the project's name in every message
            message = f"{self.root_fn().name}: {message}"
        with self.lock:
            # In a shared repository another editor may be committing right now.
            deadline = time.monotonic() + (LOCK_WAIT if self.shared else 0)
            self._clear_stale_lock()
            while self.git_dir and (self.git_dir / "index.lock").exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("git is busy (index.lock); will try again")
                time.sleep(0.2)
            self.state = "committing"
            try:
                self._ok("add", "-A", "--", *paths)
                if self.git("diff", "--cached", "--quiet", "--", *paths).returncode == 0:
                    return False
                self._ok("commit", "-q", "-m", message, "--", *paths)
                h = self._ok("rev-parse", "--short", "HEAD").strip()
                self.last_commit = {"at": time.time(), "message": message, "hash": h}
                self.error = None
                return True
            finally:
                self.state = "idle"

    def commit_all(self, why: str) -> bool:
        paths = self.changes()
        ok = self.commit(paths, f"{why}: {_names(paths)}") if paths else False
        self.dirty_since = None if not self.changes() else self.dirty_since
        return ok

    # ------------------------------------------------------------ remote
    def _remote_state(self) -> None:
        self.has_remote = bool(self.git("remote", timeout=20).stdout.strip())
        up = self.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=20)
        self.upstream = up.stdout.strip() if up.returncode == 0 else None
        if self.upstream:
            r = self.git("rev-list", "--left-right", "--count", "HEAD...@{u}", timeout=20)
            try:
                self.ahead, self.behind = (int(x) for x in r.stdout.split())
            except ValueError:
                self.ahead = self.behind = 0
        else:
            n = self.git("rev-list", "--count", "HEAD", timeout=20).stdout.strip()
            self.ahead, self.behind = (int(n) if n.isdigit() else 0), 0

    def push(self, retry: bool = True, tries: int = PUSH_TRIES) -> None:
        """Push. If GitHub has commits this copy lacks (a co-author pushed), bring them in
        (pull: a merge, never a rewrite) and push again, as long as GitHub keeps moving on
        (co-authors pushing one after another), up to `tries` times. Never forced."""
        attempts = tries if retry else 1
        with self.lock:
            for attempt in range(attempts):
                self._remote_state()
                if not self.has_remote or not self.ahead:
                    return
                self.state = "pushing"
                try:
                    args = ["push", "-q"] if self.upstream else ["push", "-q", "-u", "origin", "HEAD"]
                    r = self.git(*args, timeout=180)
                    if r.returncode == 0:
                        self.last_push, self.error = time.time(), None
                        self._remote_state()
                        return
                    msg = (r.stderr or r.stdout).strip()
                    # GitHub has commits this copy lacks: GitHub said so, or the guard did
                    # (it stops such a push before GitHub sees it).
                    behind = any(s in msg for s in ("rejected", "fetch first", "non-fast-forward",
                                                    "refusing a push that would drop commits"))
                finally:
                    self.state = "idle"
                if behind and attempt < attempts - 1:
                    self.pull()
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

    def pull(self) -> None:
        """Fetch, and bring GitHub's commits in. Your changes are committed first. When only
        GitHub moved on, a fast-forward; when both did (co-authors writing at the same time),
        a merge commit that keeps both sides. Nothing is ever rewritten or thrown away: when
        the two sides changed the same lines, the merge is called off, both versions stay
        where they are (GitHub's there, yours committed here), and `error` says which files."""
        with self.lock:
            self.last_fetch = time.time()
            if not self.has_remote and not self.git("remote", timeout=20).stdout.strip():
                return
            self.state = "pulling"
            try:
                r = self.git("fetch", "-q", "--prune", timeout=120)
                if r.returncode != 0:
                    self.error = "Could not reach GitHub: " + (r.stderr or r.stdout).strip()[-200:]
                    return
                self._remote_state()
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
                        self.push(retry=False)   # put the dropped commits back on GitHub now
                    return
                if self.changes():
                    if self.busy_fn():           # an agent turn is writing: after it
                        return
                    self.commit_all("Autosave before pulling")
                    self._remote_state()
                n = self.behind
                if not self.ahead:
                    m = self.git("merge", "-q", "--ff-only", "@{u}", timeout=60)
                    if m.returncode == 0:
                        self.notice = f"Pulled {n} commit{'s' if n != 1 else ''} from GitHub"
                        self.error, self.clash = None, []
                    else:
                        self.error = "Could not update from GitHub: " + (m.stderr or m.stdout).strip()[-200:]
                else:
                    self._merge(n)
                self._remote_state()
                if healed:
                    self.notice = self._heal_note        # says more than "combined"
                if healed and self.ahead and not self.error:
                    self.push(retry=False)       # put the dropped commits back on GitHub now
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
        # Positions it had that are no longer in its history (newest first).
        lost = [c for c in dict.fromkeys(was) if c != now
                and self.git("cat-file", "-e", f"{c}^{{commit}}", timeout=20).returncode == 0
                and self.git("merge-base", "--is-ancestor", c, now, timeout=20).returncode != 0]
        if not lost:
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
            m = self.git("merge", "--no-edit", "-m", "Put back commits dropped from GitHub", tip, timeout=120)
            if m.returncode != 0:
                if self.git_dir and (self.git_dir / "MERGE_HEAD").exists():
                    self.git("merge", "--abort", timeout=60)
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
        m = self.git("merge", "--no-edit", "-m", "Merge changes from GitHub", "@{u}", timeout=120)
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
        r = subprocess.run(["git", "show", spec], cwd=self.top or self.root_fn(), capture_output=True,
                           timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError(f"git show {spec}: " + r.stderr.decode("utf-8", "replace").strip())
        return r.stdout

    def _keep_both(self) -> list[dict]:
        """Settle every file the merge stopped at, losing nothing: text gets both versions;
        a binary file (a figure) keeps yours, with GitHub's saved next to it; a file one side
        deleted and the other changed keeps the changed version."""
        top = self.top or self.root_fn()
        mine = self.git("config", "user.name", timeout=20).stdout.strip() or "you"
        them = self.git("log", "-1", "--format=%an", "@{u}", timeout=20).stdout.strip() or "a co-author"
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
        clash = [x for x in self.git("diff", "--name-only", "HEAD", "@{u}", timeout=30).stdout.splitlines() if x]
        names = [x[len(self.prefix):] if x.startswith(self.prefix) else x for x in clash]
        self.clash = names
        who = self.git("log", "-1", "--format=%an, %ar", "@{u}", timeout=20).stdout.strip()
        self.error = (f"Your changes and GitHub's could not be combined by themselves{why}"
                      f"{f' (last there: {who})' if who else ''}. Nothing was lost: GitHub keeps "
                      "its version, this copy keeps yours (committed). In the GitHub menu: compare "
                      "with GitHub's version, edit the file so it has what both of you want, then "
                      "choose \"I've combined them\".")

    def theirs(self, path: str) -> dict:
        """GitHub's version of a project file, and how it differs from yours."""
        full = self.rel(path)
        content = self.git("show", f"@{{u}}:{full}", timeout=30)
        diff = self.git("diff", "--no-color", "HEAD", "@{u}", "--", full, timeout=30)
        return {"path": path, "content": content.stdout if content.returncode == 0 else None,
                "diff": diff.stdout}

    def resolve_mine(self) -> None:
        """After a clash, once you have edited the files to hold what both of you want: merge
        GitHub's branch, taking your version of the lines both changed (git merge -X ours:
        everything else of theirs comes in as usual), and push. Their version stays in
        their commits; nothing leaves the history."""
        with self.lock:
            if self.changes():
                self.commit_all("Combined with GitHub's version")
            r = self.git("fetch", "-q", "--prune", timeout=120)
            if r.returncode != 0:
                self.error = "Could not reach GitHub: " + (r.stderr or r.stdout).strip()[-200:]
                return
            self._remote_state()
            if not self.behind:
                self.clash = []
                self.push()
                return
            m = self.git("merge", "--no-edit", "-X", "ours", "-m",
                         "Merge changes from GitHub (clashing lines: kept the combined version)",
                         "@{u}", timeout=120)
            if m.returncode != 0:
                if self.git_dir and (self.git_dir / "MERGE_HEAD").exists():
                    self.git("merge", "--abort", timeout=60)
                self.error = "Could not combine: " + (m.stderr or m.stdout).strip()[-300:]
                return
            self.error, self.clash = None, []
            self.notice = "Combined with GitHub's version"
            self.push()

    # ------------------------------------------------------------ hooks
    def touched(self) -> None:
        """You saved a file."""
        now = time.time()
        self.last_edit = now
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
        first = next((ln.strip() for ln in prompt.splitlines()
                      if ln.strip() and not ln.startswith("[")), "") or "(no message)"
        first = first if len(first) <= 72 else first[:71] + "…"
        self._safe(lambda: self.commit([self.rel(p) for p in paths], f"Agent: {first}"))

    def _safe(self, fn) -> None:
        try:
            fn()
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
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
        if self.check_repo():
            self._finish_crashed_merge()
            self._safe(self._remote_state)
            # Opening a project brings in what changed on GitHub (another computer) first,
            # before you start editing an old version.
            if self.enabled and self.has_remote:
                self._safe(self.pull)
            # Changes left from before the editor started are recorded like new ones.
            try:
                if self.enabled and self.changes():
                    self.last_edit = self.dirty_since = time.time()
            except (OSError, subprocess.SubprocessError, RuntimeError):
                pass
        while not self.stop.wait(TICK):
            self.tick()

    def _finish_crashed_merge(self) -> None:
        """A merge prism-local started and never finished (the editor was killed half-way):
        call it off, so this copy is as it was before it; the next sync merges again. A merge
        someone started in a terminal is theirs to finish, and is left alone."""
        head = self.git_dir / "MERGE_HEAD" if self.git_dir else None
        if not (head and head.exists()):
            return
        try:
            msg = (self.git_dir / "MERGE_MSG").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        if msg.startswith(("Merge changes from GitHub", "Put back commits dropped from GitHub")):
            self._clear_stale_lock()
            self.git("merge", "--abort", timeout=60)

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
            self._safe(self.pull)
        elif action == "resolve":
            self._safe(self.resolve_mine)
        else:
            self._safe(lambda: self.commit_all("Saved"))
            self._safe(self.push)

    # ------------------------------------------------------------ for the editor
    def status(self) -> dict:
        own = self.check_repo()
        st = {"own": own, "enabled": own and self.enabled,
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
