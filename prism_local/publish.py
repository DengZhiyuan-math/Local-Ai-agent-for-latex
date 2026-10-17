"""Putting a project on GitHub: its own repository (git init, with build output ignored and
line ends kept alike for every co-author), a private GitHub repository for it, the first
push, and the sync target bound. The Home page and the editor's GitHub menu both publish
here; GitHub itself is asked through github.GITHUB.

    publish_info(root)                      # what publishing would do, or why it cannot
    publish_project(root, name, owner, …)   # {"url", "created"} or {"error"}
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import github
import gitenv
import gitsync
import instances
import settings
import sources
from fsutil import write_bytes
from projectconfig import Config
from repository import RepoBusy, Repository, locate
from synctarget import RepoSettings

REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")     # a GitHub repository name
# Text files are stored with \n line ends whatever each co-author's git does with them
# (core.autocrlf differs between Windows, macOS and Linux setups): without this, one
# person's CRLF commit would make every line of a shared file clash with everyone else's.
GITATTRIBUTES = "* text=auto\n*.pdf binary\n*.png binary\n*.jpg binary\n*.jpeg binary\n"
# Folders of a project that are better kept off GitHub unless you choose otherwise.
PRIVATE_DIRS = {"conversations": "saved AI chat transcripts"}


def repo_name(folder: str) -> str:
    """A GitHub repository name for a folder name: letters, digits, '.', '-', '_'."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", folder).strip("-.")
    return name[:100] or "latex-project"


def run_git(root: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """git in `root`; CalledProcessError when it fails."""
    return gitenv.git(root, *args, timeout=timeout, check=True)


def repo_kind(p: Path) -> tuple[str, Path | None]:
    """own | shared | nested (inside some other repository) | none, and the repository's top
    (repository.locate)."""
    place = locate(p)
    return place.kind, (place.repo.top if place.repo else None)


def project_sync(root: Path) -> gitsync.GitSync:
    """The sync of a project whose editor may not be open (Home, publishing). Its build
    output, never committed, is in the folder its prism.json names, not always build/."""
    return gitsync.GitSync(lambda: root, lambda: Config(root).outdir)


def github_of(top: Path | None) -> str | None:
    """The GitHub page of the repository at `top` (or its remote's address)."""
    if top is None:
        return None
    sync = project_sync(top)
    if not sync.validate_target():
        return None
    return gitsync.github_url(sync.target) or gitsync.redact_url(sync.target["push_url"])


def git_init(root: Path) -> None:
    """A repository of the project's own, with build output ignored, line ends kept the
    same for co-authors on every system, and force pushes refused (gitsync.install_guard)."""
    gi = root / ".gitignore"
    if not gi.exists():
        lines = sources.gitignore(Config(root).outdir)
        write_bytes(gi, ("\n".join(lines) + "\n").encode("utf-8"))
    ga = root / ".gitattributes"
    if not ga.exists():
        write_bytes(ga, GITATTRIBUTES.encode("utf-8"))
    if not (root / ".git").exists():
        try:
            run_git(root, "init", "-q", "-b", "main")
        except subprocess.CalledProcessError:          # git older than 2.28
            run_git(root, "init", "-q")
    if (root / ".git").is_dir():
        gitsync.install_guard(root / ".git")


def ensure_ignore(top: Path) -> None:
    """What a repository shared by several projects ignores, added to its .gitignore."""
    gi = top / ".gitignore"
    have = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    add = [x for x in sources.gitignore(shared=True) if x not in have]
    if add:
        text = "\n".join(have + add) + "\n"
        write_bytes(gi, text.lstrip("\n").encode("utf-8"))


def unstage_big(repo: Repository) -> list[str]:
    """Files GitHub would refuse (sources.MAX_FILE and over) stay out of the commit. In
    repo.changing()."""
    staged = repo.ok("diff", "--cached", "--name-only", "-z").split("\0")
    big = [x for x in staged if x and (repo.top / x).is_file() and (repo.top / x).stat().st_size > sources.MAX_FILE]
    if big:
        repo.ok("rm", "-q", "--cached", "--", *big)
    return big


def create_github_repo(root: Path, owner: str = "", name: str = "", files_lock=None) -> dict:
    """Commit the project and push it to a new private GitHub repository. The commit holds
    the repository and `files_lock` (the editor's save lock, when the editor publishes; else
    the repository's FilesLock): no save lands in the middle of git add.

    Returns {"url", "protection"} or {"error"}; failures after creation also include "url".
    The project itself is never removed on failure."""
    gh = github.GITHUB
    st = gh.status()
    if not st.get("logged_in"):
        return {"error": st.get("error") or "GitHub CLI is not ready"}
    if owner and not settings.OWNER_RE.fullmatch(owner):
        return {"error": "not a GitHub user or organization name"}
    name = name.strip() or repo_name(root.name)
    if not REPO_RE.fullmatch(name):
        return {"error": f"not a GitHub repository name: {name}"}
    full = f"{owner}/{name}" if owner else name
    who = owner or st["account"]
    url = f"https://github.com/{who}/{name}"
    try:
        git_init(root)
        if run_git(root, "remote").stdout.strip():
            return {"error": "This repository already has a remote. Confirm its sync target in the editor Git menu."}
        repo = Repository.find(root, files=(lambda: files_lock) if files_lock else None)
        if repo is None:
            return {"error": "git init failed"}
        with repo.changing():
            repo.ok("add", "-A")
            unstage_big(repo)
            if repo.ok("status", "--porcelain", "--untracked-files=no").strip():
                repo.ok("commit", "-q", "-m", "Initial commit")
        branch = run_git(root, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
        # What it syncs with is the repository created here, bound before it exists: from
        # the moment it has an origin, a look (the Home page, the editor's sync) finds it
        # bound, and never binds whatever the configuration names instead. Its branch
        # pushes to the branch of the same name there.
        own = RepoSettings(repo)
        own.bind({"repository_identity": f"github.com/{who}/{name}".lower(),
                  "branch_ref": f"refs/heads/{branch}", "local_branch": f"refs/heads/{branch}"})
        run_git(root, "config", f"branch.{branch}.remote", "origin")
        run_git(root, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
    except subprocess.CalledProcessError as e:
        return {"error": f"git {e.cmd[1]} failed: {(e.stderr or e.stdout or '').strip()[:400]}"}
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:   # RepoBusy, too
        return {"error": str(e)}
    failed = gh.create(root, full)
    if failed:
        own.unbind()                    # nothing was created to sync with
        return {"error": failed}
    sync = project_sync(root)
    try:
        candidate = sync.candidate()
        if candidate["repository_identity"] != f"github.com/{who}/{name}".lower() \
                or candidate["branch_ref"] != candidate["local_branch"]:
            # Something on this computer sends it elsewhere (a URL rewrite in your git
            # settings, a change made meanwhile). It stays bound to the repository created
            # here: sync waits, and goes on by itself once they agree again.
            return {"error": f"The GitHub repository {who}/{name} was created, but this computer's git "
                             "settings send uploads elsewhere, so nothing was uploaded. Your files are "
                             "kept here, and uploading starts by itself once the settings point to it.",
                    "url": url}
        sync.bind_target(gitsync.public_target(candidate))
        pushed = sync.push(manual=True)
        if pushed.state != "synced":
            return {"error": pushed.message or "not pushed", "url": url}
    except (ValueError, OSError, subprocess.SubprocessError, RepoBusy) as exc:
        return {"error": str(exc), "url": url}
    branch = candidate["branch_ref"].removeprefix("refs/heads/")
    return {"url": url, "protection": gh.protect(f"{who}/{name}", branch)}


def publish_info(root: Path) -> dict:
    """What creating a GitHub repository for the project would do, or why it cannot."""
    kind, top = repo_kind(root)
    info = {"kind": kind, "top": str(top) if top else None, "github": None,
            "name": repo_name(root.name), "private_dirs": [], "gh": github.GITHUB.status()}
    if kind == "own":
        info["github"] = github_of(root)
    if kind == "shared":
        info["github"] = github_of(top)
        info["name"] = repo_name(top.name)
    for d, what in PRIVATE_DIRS.items():
        if (root / d).is_dir():
            ignored = top is not None and gitenv.git(root, "check-ignore", "-q", d + "/", timeout=20).returncode == 0
            info["private_dirs"].append({"dir": d, "what": what, "ignored": ignored})
    return info


def publish_project(root: Path, name: str = "", owner: str = "", leave_out: list | None = None,
                    files_lock=None) -> dict:
    """Give the project a private GitHub repository: git init if needed, a .gitignore
    (build output, LaTeX's auxiliary files, and the folders in `leave_out`), a commit of
    everything, the repository created, push. A project in a folder's shared repository
    publishes that repository. `owner`: your own account, or the one in your settings, when
    empty. `files_lock`: see create_github_repo. Returns {"url", "created"} or {"error"}."""
    info = publish_info(root)
    if info["kind"] == "nested":
        return {"error": f"This folder is inside another repository ({info['top']}), which is "
                         "not a project's: move the project out of it first."}
    if info["github"] and info["github"].startswith("https://github.com/"):
        return {"url": info["github"], "created": False}
    target = Path(info["top"]) if info["kind"] == "shared" else root
    owner = owner or settings.load()["github_owner"]
    try:
        git_init(target)
        ensure_ignore(target)
        if info["kind"] != "shared" and leave_out:
            gi = target / ".gitignore"
            have = gi.read_text(encoding="utf-8").splitlines()
            add = [f"{d.strip('/')}/" for d in leave_out if d.strip("/") in PRIVATE_DIRS
                   and f"{d.strip('/')}/" not in have]
            if add:
                write_bytes(gi, ("\n".join(have + add) + "\n").encode("utf-8"))
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"git init failed: {e}"}
    r = create_github_repo(target, owner, name or info["name"], files_lock=files_lock)
    if "url" in r:
        tell_editor(root)
    return {**r, "created": "url" in r}


def tell_editor(root: Path) -> None:
    """The project's open editor, if any, looks for its new repository (gitsync.recheck)."""
    instances.call(root, "api/git/recheck")
