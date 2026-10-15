"""Where a repository syncs to, and the target its author confirmed.

    target = synctarget.resolve(repo)              # ValueError: why it cannot sync
    settings = synctarget.RepoSettings(repo)       # .git/prism-local.json
    target, blocked = settings.validate(fresh=True)
    settings.bind(target)                          # after the author confirmed it

A repository's sync target is what its configuration names: the branch checked out, its
upstream (else origin) and that remote's one fetch and one push URL, which must be the
same repository. resolve refuses anything ambiguous (two upstreams, a mirror, a push
remote elsewhere, a detached HEAD) rather than guess.

The binding is the target the author confirmed, kept in .git/prism-local.json with the
sync switch: syncing stops when the configuration later names another repository or
branch, until the author confirms the new one. Plain functions of git's configuration:
the editor's sync (gitsync.py), publishing and the Home page all use them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import registry
from fsutil import write_bytes
from repository import Repository


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


# What decides a repository's sync target (resolve) and its hooks: its config (also a
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


def cached(kind: str, git_dir: Path, compute: Callable[[], object]):
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


def resolve(repo: Repository, fresh: bool = False) -> dict:
    """The sync target `repo`'s configuration names, looked up again only when that
    configuration changed (cached), unless `fresh`: everything that pushes, fetches or
    binds asks fresh. ValueError: why the repository cannot sync as it is."""
    if fresh:
        return _resolve(repo)
    return dict(cached("target", repo.git_dir, lambda: _resolve(repo)))


def _resolve(repo: Repository) -> dict:
    def git(*args: str) -> subprocess.CompletedProcess:
        return repo.git(*args)
    actual = git("rev-parse", "--show-toplevel", "--absolute-git-dir").stdout.strip().splitlines()
    if len(actual) != 2 or registry.norm(Path(actual[0]).resolve()) != registry.norm(repo.top) \
            or registry.norm(Path(actual[1]).resolve()) != registry.norm(repo.git_dir.resolve()):
        raise ValueError("Project repository changed. Reopen the editor before syncing")
    branch = git("symbolic-ref", "--quiet", "HEAD").stdout.strip()
    if not branch.startswith("refs/heads/"):
        raise ValueError("Detached HEAD: select a local branch before syncing")
    name = branch.removeprefix("refs/heads/")

    def values(key):
        return git("config", "--get-all", key).stdout.strip().splitlines()
    remotes = git("remote").stdout.splitlines()
    upstream = values(f"branch.{name}.remote")
    merges = values(f"branch.{name}.merge")
    if len(upstream) > 1 or len(merges) > 1:
        raise ValueError("Ambiguous upstream configuration")
    remote = upstream[0] if upstream else "origin"
    if remote == "." or remote not in remotes:
        raise ValueError("A unique remote upstream or origin is required")
    branch_ref = merges[0] if merges else branch
    if not branch_ref.startswith("refs/heads/") or git("check-ref-format", branch_ref).returncode:
        raise ValueError("Sync requires one valid remote branch")
    for key in (f"branch.{name}.pushRemote", "remote.pushDefault"):
        if any(v != remote for v in values(key)):
            raise ValueError(f"{key} differs from the sync remote {remote}")
    if git("config", "--bool", "--get", f"remote.{remote}.mirror").stdout.strip() == "true":
        raise ValueError("Mirror remotes cannot be used for editor sync")
    fetch = git("remote", "get-url", "--all", remote).stdout.strip().splitlines()
    push = git("remote", "get-url", "--push", "--all", remote).stdout.strip().splitlines()
    if len(fetch) != 1 or len(push) != 1:
        raise ValueError("Sync requires exactly one fetch URL and one push URL")
    identity = repository_identity(fetch[0], repo.top)
    if identity != repository_identity(push[0], repo.top) or (
            "://" in fetch[0] and not identity.startswith("github.com/") and fetch[0] != push[0]):
        raise ValueError("Fetch and push repositories differ: " + redact_url(push[0]))
    config = git("config", "--null", "--get-regexp",
                 r"^(branch\..*\.(remote|merge|pushremote)|remote\..*\.(url|pushurl|fetch|push|mirror)|remote\.pushdefault|url\..*\.(insteadof|pushinsteadof))$").stdout
    # Only relevant selected-remote/branch configuration affects the binding. Other
    # remotes may be added by the author without changing this target.
    entries = [e for e in config.split("\0") if e and (
        e.lower().startswith((f"branch.{name}.".lower(), f"remote.{remote}.".lower(),
                              "remote.pushdefault", "url.")))]
    fingerprint = hashlib.sha256(json.dumps([branch, sorted(entries)]).encode()).hexdigest()
    return {"remote": remote, "branch_ref": branch_ref, "local_branch": branch,
            "fetch_url": redact_url(fetch[0]), "push_url": redact_url(push[0]), "repository_identity": identity,
            "git_dir": registry.norm(repo.git_dir.resolve()),
            "configuration_fingerprint": fingerprint}


class RepoSettings:
    """prism-local's own settings of a repository (.git/prism-local.json): the sync switch,
    the confirmed sync target, and what a rewritten history was answered with. The editor
    and the Home page both write it: under a file lock, so neither drops what the other wrote."""

    def __init__(self, repo: Repository):
        self.repo = repo
        self.file = repo.git_dir / "prism-local.json"

    def read(self) -> dict:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def update(self, **changes) -> None:
        """Change keys, keeping the others."""
        with registry.FileLock(self.file.with_name(self.file.name + ".lock"), timeout=10):
            write_bytes(self.file, json.dumps({**self.read(), **changes}).encode())

    @property
    def enabled(self) -> bool:
        return bool(self.read().get("sync", True))

    def bind(self, target: dict) -> None:
        """Called only after the author confirmed `target`, or after creating a known repo."""
        self.update(sync_target=target)

    def validate(self, fresh: bool = False) -> tuple[dict | None, str | None]:
        """(the target, None) when syncing may go on; else (the target or None, why not).
        No binding yet (a project from before bindings, or a fresh clone): the target passed
        every check of resolve, so it is bound now. The same repository and branch reached
        another way (ssh instead of https, an upstream set by `git push -u`) is rebound
        quietly; another repository or branch waits for the author to confirm it."""
        bound = self.read().get("sync_target")
        target = None
        try:
            target = resolve(self.repo, fresh)
            # git_dir is the current local location, not the bound remote identity.
            same = isinstance(bound, dict) and all(target[k] == bound.get(k) for k in
                                                   ("repository_identity", "branch_ref", "local_branch"))
            if isinstance(bound, dict) and not same:
                raise ValueError("Sync target changed. Local edits are saved; confirm the new target in the Git menu")
            if public_target(target) != public_target(bound):
                self.bind(target)
            return target, None
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            return target, str(exc)
