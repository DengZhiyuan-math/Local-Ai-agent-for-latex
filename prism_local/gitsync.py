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

Build output (the build folder, .aux, .log, ...) is never committed. Nothing happens in a
folder that sits inside another repository (it has no repository of its own), and sync
can be switched off per clone (.git/prism-local.json, never committed).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from proc import NO_WINDOW

IDLE = 120               # seconds without an edit before an autosave commit
MAX_WAIT = 600           # ... but at the latest this long after the first change
FETCH_EVERY = 300        # seconds between fetches from the remote
TICK = 5                 # the worker's period
RETRY = 60               # seconds before a failed push is tried again
MAX_FILE = 50 * 1024 * 1024    # GitHub refuses files over 100 MB; keep well below
# Files LaTeX writes next to the sources when they are not in the build folder.
AUX = (".aux", ".log", ".out", ".toc", ".lof", ".lot", ".bbl", ".blg", ".bcf", ".run.xml",
       ".fls", ".fdb_latexmk", ".synctex.gz", ".synctex", ".nav", ".snm", ".vrb", ".idx",
       ".ilg", ".ind", ".xdv", ".dvi", ".thm", ".loe")


def _names(paths: list[str]) -> str:
    names = [p.split("/")[-1] for p in paths]
    return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


class GitSync:
    def __init__(self, root_fn: Callable[[], Path], outdir_fn: Callable[[], str],
                 busy: Callable[[], bool] = lambda: False):
        self.root_fn, self.outdir_fn, self.busy_fn = root_fn, outdir_fn, busy
        self.lock = threading.RLock()         # one git command sequence at a time
        self.own: bool | None = None          # the project is the top of its repository
        self.git_dir: Path | None = None
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
        self.stop = threading.Event()

    # ------------------------------------------------------------ git
    def git(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.root_fn(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **NO_WINDOW)

    def _ok(self, *args: str, timeout: float = 60) -> str:
        r = self.git(*args, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-400:] or f"git {args[0]} failed")
        return r.stdout

    def check_repo(self) -> bool:
        """Whether the project is the top of its own repository (looked up once per folder)."""
        root = self.root_fn().resolve()
        if getattr(self, "_checked", None) != root:
            self._checked, self.own, self.git_dir = root, None, None
        if self.own is None:
            try:
                r = self.git("rev-parse", "--show-toplevel", "--absolute-git-dir", timeout=20)
                lines = r.stdout.strip().splitlines()
                self.own = r.returncode == 0 and len(lines) == 2 \
                    and Path(lines[0]).resolve() == self.root_fn().resolve()
                self.git_dir = Path(lines[1]) if self.own else None
            except (OSError, subprocess.SubprocessError):
                self.own = False
        return bool(self.own)

    # ------------------------------------------------------------ the switch
    def _settings_file(self) -> Path | None:
        return self.git_dir / "prism-local.json" if self.check_repo() and self.git_dir else None

    @property
    def enabled(self) -> bool:
        f = self._settings_file()
        if not f:
            return False
        try:
            return bool(json.loads(f.read_text(encoding="utf-8")).get("sync", True))
        except (OSError, ValueError):
            return True

    def set_enabled(self, on: bool) -> None:
        f = self._settings_file()
        if f:
            f.write_text(json.dumps({"sync": bool(on)}), encoding="utf-8")

    # ------------------------------------------------------------ what changed
    def changes(self) -> list[str]:
        """Paths (from the top) with changes worth recording: no build output, no huge files."""
        out = self._ok("status", "--porcelain", "-z", "-uall", timeout=30)
        outdir = self.outdir_fn().strip("/") + "/"
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
            p = self.root_fn() / path
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
        with self.lock:
            if self.git_dir and (self.git_dir / "index.lock").exists():
                raise RuntimeError("git is busy (index.lock); will try again")
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

    def push(self) -> None:
        with self.lock:
            self._remote_state()
            if not self.has_remote or not self.ahead:
                return
            self.state = "pushing"
            try:
                args = ["push", "-q"] if self.upstream else ["push", "-q", "-u", "origin", "HEAD"]
                r = self.git(*args, timeout=180)
                if r.returncode != 0:
                    self.push_failed_at = time.time()
                    msg = (r.stderr or r.stdout).strip()
                    self.error = ("GitHub has changes this copy does not have yet; pull first."
                                  if "rejected" in msg or "fetch first" in msg else "Push failed: " + msg[-300:])
                    return
                self.last_push, self.error = time.time(), None
                self._remote_state()
            finally:
                self.state = "idle"

    def pull(self) -> None:
        """Fetch, and fast-forward when only the remote moved on (after committing your
        changes, so that nothing of yours is in the way)."""
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
                if self.behind and not self.ahead:
                    if self.changes() and not self.busy_fn():
                        self.commit_all("Autosave before pulling")
                        self._remote_state()
                    m = self.git("merge", "-q", "--ff-only", "@{u}", timeout=60)
                    if m.returncode == 0:
                        self.notice = f"Pulled {self.behind} commit{'s' if self.behind != 1 else ''} from GitHub"
                        self.error = None
                    else:
                        self.error = "Could not update from GitHub: " + (m.stderr or m.stdout).strip()[-200:]
                    self._remote_state()
                elif self.behind and self.ahead:
                    self.error = (f"This copy and GitHub have both changed ({self.ahead} and {self.behind} "
                                  "commits). Merge them in a terminal: git pull")
            finally:
                self.state = "idle"

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
        self._safe(lambda: self.commit(paths, f"Agent: {first}"))

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
        if now - self.last_fetch >= FETCH_EVERY and not self.busy_fn():
            self._safe(self.pull)

    def run(self) -> None:
        if self.check_repo():
            self._safe(self._remote_state)
            # Changes left from before the editor started are recorded like new ones.
            try:
                if self.enabled and self.changes():
                    self.last_edit = self.dirty_since = time.time()
            except (OSError, subprocess.SubprocessError, RuntimeError):
                pass
        while not self.stop.wait(TICK):
            self.tick()

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True).start()

    def flush(self) -> None:
        """When the editor closes: commit what is left and push it."""
        self.stop.set()
        if self.active():
            self._safe(lambda: self.commit_all("Autosave"))
            self._safe(self.push)

    def now(self, action: str) -> None:
        """A button: commit everything now and push, or pull."""
        if action == "pull":
            self._safe(self.pull)
        else:
            self._safe(lambda: self.commit_all("Saved"))
            self._safe(self.push)

    # ------------------------------------------------------------ for the editor
    def status(self) -> dict:
        own = self.check_repo()
        st = {"own": own, "enabled": own and self.enabled, "state": self.state, "error": self.error,
              "remote": self.has_remote, "upstream": self.upstream, "ahead": self.ahead,
              "behind": self.behind, "last_commit": self.last_commit, "last_push": self.last_push or None,
              "pending": self.dirty_since is not None, "notice": self.notice,
              "next_autosave": (None if self.dirty_since is None else
                                max(0, min((self.last_edit or 0) + IDLE, self.dirty_since + MAX_WAIT) - time.time()))}
        self.notice = None
        return st

    def log(self, path: str, n: int = 60) -> list[dict]:
        """The commits that changed `path` (newest first), following renames."""
        out = self._ok("log", "--follow", f"-n{n}", "--format=%H%x1f%ct%x1f%an%x1f%s", "--", path, timeout=30)
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
        name = self._ok("log", "--follow", "-n1", "--format=", "--name-only", rev, "--", path).strip() or path
        content = self.git("show", f"{rev}:{name}", timeout=30)
        diff = self.git("show", "--format=", "--no-color", rev, "--", name, timeout=30)
        return {"content": content.stdout if content.returncode == 0 else None, "diff": diff.stdout, "path": name}
