"""prism-local updates itself: before a server starts, the app's own repository (the
folder this file is in, when it is a git clone) takes what is new on GitHub.

Only a fast-forward, and only when that cannot touch work of yours: the checkout is on a
branch that tracks one on GitHub, has no uncommitted changes and no merge or rebase in
progress, and has no commits GitHub lacks. Otherwise it is left alone and the server log
says why. At most once every few minutes (the launcher, the Home page and each editor
all ask; the first one fetches). PRISM_AUTO_UPDATE=0 switches it off.

A server that is already running keeps its code; the editor then offers "Update:
restart" (server.py, code_stamp). Only the Python standard library is used.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import registry
from gitenv import git_env

APP_DIR = Path(__file__).resolve().parent.parent
EVERY = 300                      # seconds between two checks
FETCH_TIMEOUT = 20
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


def _git(repo: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, env=git_env(), **NO_WINDOW)


def _out(r: subprocess.CompletedProcess) -> str:
    return (r.stderr or r.stdout).strip()[-200:]


def update(repo: Path = APP_DIR) -> str | None:
    """Fast-forward the app's checkout to GitHub's branch if that is safe. Returns what
    happened (None: nothing to say, e.g. switched off, checked a moment ago, up to date)."""
    if os.environ.get("PRISM_AUTO_UPDATE", "").strip() == "0" or not (repo / ".git").exists():
        return None
    stamp = registry.state_dir() / "self-update.json"
    try:
        with registry.FileLock(stamp.with_suffix(".lock"), timeout=FETCH_TIMEOUT + 10):
            last = registry.read_json(stamp) or {}
            if time.time() - float(last.get("at") or 0) < EVERY:
                return None
            msg = _update(repo)
            registry.write_json(stamp, {"at": time.time(), "result": msg})
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as e:
        msg = f"not updated: {type(e).__name__}: {e}"
    if msg:
        print(f"prism-local update: {msg}", flush=True)
    return msg


def _update(repo: Path) -> str | None:
    top = _git(repo, "rev-parse", "--absolute-git-dir")
    if top.returncode != 0:
        return "not updated: not a git checkout"
    git_dir = Path(top.stdout.strip())
    if any((git_dir / name).exists() for name in IN_PROGRESS):
        return "not updated: a merge or rebase is in progress in " + str(repo)
    if _git(repo, "symbolic-ref", "-q", "HEAD").returncode != 0:
        return "not updated: no branch checked out"
    up = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if up.returncode != 0:
        return "not updated: the branch tracks no branch on GitHub"
    upstream = up.stdout.strip()
    remote = upstream.split("/", 1)[0]
    f = _git(repo, "fetch", "-q", remote, timeout=FETCH_TIMEOUT)
    if f.returncode != 0:
        return "not updated: could not reach GitHub: " + _out(f)
    counts = _git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}").stdout.split()
    ahead, behind = (int(x) for x in counts) if len(counts) == 2 else (0, 0)
    if not behind:
        return None
    if ahead:
        return (f"not updated: {behind} new on {upstream}, but this copy has {ahead} commit(s) "
                "GitHub lacks; pull by hand")
    if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return f"not updated: {behind} new on {upstream}, but there are uncommitted changes in {repo}"
    m = _git(repo, "merge", "-q", "--ff-only", "@{u}", timeout=60)
    if m.returncode != 0:
        return "not updated: " + _out(m)
    head = _git(repo, "log", "-1", "--format=%h %s").stdout.strip()
    return f"updated: {behind} commit{'s' if behind != 1 else ''} from {upstream}, now at {head}"
