#!/usr/bin/env python3
"""prism-local Home: one page to manage all your LaTeX projects.

    python3 prism_local/hub.py [--port 8790] [--no-browser] [--exit-when-idle]
                               [--port-tries N] [--ready-file F]

Lists the projects in the shared project list (registry.py), with title, PDF
thumbnail, git state and whether an editor is running, grouped in folders and tagged
(folders and tags exist only in the list). From here you can add an
existing folder, create a new project from a template, and open a project: each
project still runs in its own prism-local server (server.py), started through
the launcher, so it keeps its stable port and per-project browser state.

Like server.py it listens on 127.0.0.1 only, requires X-Prism-Local: 1 on every
state-changing request, and uses the Python standard library only.
"""
from __future__ import annotations

import gitenv

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitsync  # noqa: E402
import github  # noqa: E402
import settings  # noqa: E402
import sources  # noqa: E402
import synctarget  # noqa: E402
from projectconfig import Config  # noqa: E402
from publish import (create_github_repo, git_init, github_of, project_sync, publish_info,  # noqa: E402
                     publish_project, repo_kind)
from repository import RepoBusy, locate, refs_stamp  # noqa: E402
import sharedrepo  # noqa: E402
from sharedrepo import BAD_NAME, inside, is_running  # noqa: E402
import httpbase  # noqa: E402
import desktop  # noqa: E402
from errors import NotFound, say  # noqa: E402
from proc import NO_WINDOW  # noqa: E402
import instances  # noqa: E402
import registry  # noqa: E402
import selfupdate  # noqa: E402
from backend_claude import claude_account, claude_bin  # noqa: E402
from fsutil import EDITABLE_SUFFIXES, SKIP_DIRS  # noqa: E402
from presence import Presence  # noqa: E402
from texutil import tex_title  # noqa: E402

PRESENCE = Presence()
# Like the editor, the Home page keeps running the code it started with; once prism-local
# is updated, the page offers a restart (/api/restart) on the same command line.
STARTED_CODE = httpbase.code_stamp()
RESTART: dict = {"server": None, "again": False}
LIFETIME: instances.Serving | None = None
MAX_SCAN = 3000


# ---------------------------------------------------------------- project facts

SCAN_TTL = 60.0
_SCANS: dict[tuple, tuple] = {}      # (project folder, output folder) -> what the last walk saw


def scan_sources(root: Path, outdir: str) -> tuple[int, float | None, bool]:
    """How many editable source files the project has, the newest mtime, and whether it has
    more than MAX_SCAN (the count then stops at MAX_SCAN).

    The folders are walked again only when one of those walked last time changed (a file
    added, removed or renamed in it, which a save by the editor is: it writes a new file
    and renames it over the old one), the newest file changed, or SCAN_TTL went by. A file
    edited in place, which changes no folder, is seen at the latest then: one stat per
    folder instead of a walk of them all every ten seconds."""
    key = (str(root), outdir)
    hit = _SCANS.get(key)
    if hit and time.monotonic() - hit[0] < SCAN_TTL \
            and all(_stat(Path(p)) == st for p, st in hit[1].items()):
        return hit[2]
    when = time.monotonic()
    # Each folder's stat is taken before it is listed, so a change while it is walked
    # makes it differ next time.
    stats = {str(root): _stat(root)}
    count, newest, newest_path = 0, None, None
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root).as_posix()
        rel = "" if rel == "." else rel + "/"
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
                       and rel + d != outdir]
        stats.update((os.path.join(dirpath, d), _stat(Path(dirpath, d))) for d in dirnames)
        for f in filenames:
            if Path(f).suffix in EDITABLE_SUFFIXES and not f.startswith("._"):
                count += 1
                try:
                    t = os.stat(os.path.join(dirpath, f)).st_mtime
                    if newest is None or t > newest:
                        newest, newest_path = t, os.path.join(dirpath, f)
                except OSError:
                    pass
        if count > MAX_SCAN:
            break
    if newest_path:
        stats[newest_path] = _stat(Path(newest_path))
    summary = (min(count, MAX_SCAN), newest, count > MAX_SCAN)
    _SCANS[key] = (when, stats, summary)
    return summary


def _stat(p: Path):
    try:
        st = p.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


# The Home page asks for every project's facts every ten seconds, and most are the same as
# the last time: each is worked out again only when what it is made of changed.
_TITLES: dict[str, tuple] = {}       # main file -> (its stat, its title)
_PLACES: dict[str, object] = {}      # project folder -> its Place, while its git dir is there
_SYNC_FACTS: dict[str, tuple] = {}   # git dir -> (what they depend on, the sync facts)


def title_of(path: Path) -> str | None:
    """tex_title, read again only when the file changed (it is read whole for it)."""
    st = _stat(path)
    hit = _TITLES.get(str(path))
    if hit and st is not None and hit[0] == st:
        return hit[1]
    title = tex_title(path)
    _TITLES[str(path)] = (st, title)
    return title


def place_of(root: Path):
    """locate(root), looked up again only when its repository's git dir went (the project
    joined a shared repository, or left one), it had none (one may have been made), or a
    .git appeared between it and the top of the repository it is in (git finds that one)."""
    hit = _PLACES.get(str(root))
    if hit is not None and hit.repo is not None and hit.repo.git_dir.is_dir()             and not (hit.prefix and _git_below(root, hit.repo.top)):
        return hit
    place = locate(root, timeout=8)
    _PLACES[str(root)] = place
    return place


def _git_below(folder: Path, top: Path) -> bool:
    """A .git in `folder` or a folder above it, below `top`."""
    p, top = folder.resolve(), top.resolve()
    while p != top and p != p.parent:
        if (p / ".git").exists():
            return True
        p = p.parent
    return False


def sync_facts(root: Path, git_dir: Path) -> dict:
    """What git_info shows of the project's sync (its GitHub page, why it is paused, its
    target): a GitSync's status, some seven git commands, worked out again only when the
    repository's refs or configuration (refs_stamp), its prism-local.json (the binding)
    or what git reads of its configuration besides (your ~/.gitconfig and
    $XDG_CONFIG_HOME/git/config with their URL rewrites, a worktree's config) changed."""
    stamp = refs_stamp(git_dir)
    key = (stamp, _stat(git_dir / "prism-local.json"), synctarget.config_key(git_dir))
    hit = _SYNC_FACTS.get(str(git_dir))
    if hit and stamp is not None and hit[0] == key:
        return hit[1]
    sync = project_sync(root)
    sync.validate_target()          # a target not bound yet is bound, as the editor's worker does
    status = sync.status()          # which binds nothing itself
    facts = {k: status[k] for k in ("github", "blocked_reason", "target")}
    # Asked again: validate_target may have bound the target, changing prism-local.json.
    _SYNC_FACTS[str(git_dir)] = ((refs_stamp(git_dir), _stat(git_dir / "prism-local.json"),
                                  synctarget.config_key(git_dir)), facts)
    return facts


def pdf_path(root: Path) -> tuple[Path | None, str | None, str]:
    """The project's PDF, its document and its output folder, as the editor names them."""
    cfg = Config(root)
    return (cfg.pdf if cfg.named_main else None), cfg.named_main, cfg.outdir


def project_info(entry: dict) -> dict:
    root = Path(entry["path"])
    d = {"id": registry.project_key(root), "path": str(root),
         "name": entry.get("name") or root.name, "folder": root.name,
         "custom_name": bool(entry.get("name")), "pinned": bool(entry.get("pinned")),
         "folder_id": entry.get("folder_id"), "tags": list(entry.get("tags") or []),
         "opened": entry.get("opened"), "added": entry.get("added"),
         "exists": root.is_dir(), "running": None}
    if not d["exists"]:
        return d
    pdf, main, outdir = pdf_path(root)
    files, edited, capped = scan_sources(root, outdir)
    d.update(main=main, title=title_of(root / main) if main else None,
             files=files, files_capped=capped, edited=edited,
             pdf_mtime=pdf.stat().st_mtime if pdf and pdf.is_file() else None)
    # Only a look: a server that is slow to answer right now keeps its instance file.
    state = instances.inspect(root)
    if state.state == "ready":
        d["running"] = {"url": state.info["url"], "pages": state.info.get("pages", 0)}
    return d


def list_projects() -> dict:
    data = registry.load_projects()
    entries = data["projects"]
    with ThreadPoolExecutor(max_workers=8) as ex:
        projects = list(ex.map(project_info, entries))
    return {"projects": projects, "folders": data.get("folders", []),
            "tags": data.get("tags", {}), "default_parent": default_parent(data),
            "home": str(Path.home())}


def all_git_info(ids: set[str] | None = None) -> dict:
    """git_info of the projects in the list that exist (or of those in `ids`), by id. One
    request for all, so the page does not queue a hundred requests ahead of a click on Open
    (browsers run six at a time)."""
    roots = [Path(e["path"]) for e in registry.load_projects()["projects"]]
    roots = [r for r in roots if (ids is None or registry.project_key(r) in ids) and r.is_dir()]
    with ThreadPoolExecutor(max_workers=8) as ex:
        infos = list(ex.map(git_info, roots))
    return {registry.project_key(r): info for r, info in zip(roots, infos)}


def default_parent(data: dict) -> str:
    chosen = settings.load()["default_parent"]
    if chosen and Path(chosen).is_dir():
        return chosen
    if data.get("last_parent") and Path(data["last_parent"]).is_dir():
        return data["last_parent"]
    recent = sorted(data["projects"], key=lambda p: p.get("opened") or p.get("added") or 0,
                    reverse=True)
    for p in recent:
        parent = Path(p["path"]).parent
        if parent.is_dir():
            return str(parent)
    docs = Path.home() / "Documents"
    return str(docs if docs.is_dir() else Path.home())


def git_info(root: Path) -> dict | None:
    """Branch and changes of the project's repository: its own, or one shared by a folder of
    projects (then the changes are the project's own). A project folder inside some other
    repository (such as prism-local's) has none to sync with: report which one."""
    def git(*args):
        return gitenv.git(root, *args, timeout=8)
    place = place_of(root)
    if place.kind == "none":
        return None
    toplevel = place.repo.top
    if place.kind == "nested":
        return {"nested": True, "toplevel": toplevel.name, "toplevel_path": str(toplevel)}
    shared = {"shared": toplevel.name, "toplevel_path": str(toplevel)} if place.kind == "shared" else None
    try:
        r = git("status", "--porcelain", "-z", "-b", "-uall", "--", ".")
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    first = r.stdout.split("\0", 1)[0]
    head = first[3:] if first.startswith("## ") else ""
    # What autosave would record, as the editor counts it: not build output or LaTeX's
    # auxiliary files, which a folder added without ignore rules has many of.
    changed = sources.recordable_changes(r.stdout, toplevel, place.prefix + pdf_path(root)[2].strip("/"))
    head = re.sub(r"^(No commits yet on|Initial commit on) ", "", head)
    branch = re.split(r"\.\.\.| ", head)[0] if head else ""
    ahead = re.search(r"ahead (\d+)", head)
    behind = re.search(r"behind (\d+)", head)
    sync = sync_facts(root, place.repo.git_dir)
    web = sync["github"]
    top_s = str(toplevel.resolve())
    fetch_error = next((v for k, v in FETCH["errors"].items() if registry.norm(k) == registry.norm(top_s)), None)
    return {"branch": branch, "changes": len(changed),
            "ahead": int(ahead.group(1)) if ahead else 0,
            "behind": int(behind.group(1)) if behind else 0, "github": web,
            "fetch_error": fetch_error or sync["blocked_reason"], "sync_target": sync["target"], **(shared or {})}


# ---------------------------------------------------------------- checking GitHub
#
# When the Home page starts (and on "Check GitHub"), every project's repository is fetched
# in the background: once per repository (a folder's shared one holds many projects), and
# not where an editor is open, since that editor fetches and pulls by itself. A fetch only
# downloads: the cards then show "N new on GitHub", and opening the project pulls them.

FETCH: dict = {"running": False, "done": 0, "total": 0, "at": None, "errors": {}}
_fetch_lock = threading.Lock()


def fetch_targets() -> list[Path]:
    """The repositories to fetch: with an origin, and no editor open on any of their projects."""
    tops: dict[str, Path] = {}
    busy: set[str] = set()
    for e in registry.load_projects()["projects"]:
        root = Path(e["path"])
        if not root.is_dir():
            continue
        kind, top = repo_kind(root)
        if kind not in ("own", "shared"):
            continue
        key = registry.norm(top)
        tops[key] = top
        if is_running(root):
            busy.add(key)
    return [t for k, t in tops.items() if k not in busy and github_of(t)]


def fetch_one(top: Path) -> str | None:
    """git fetch; an error message, or None."""
    try:
        sync = project_sync(top)
        if not sync.check_repo():
            return None
        # Not while another prism-local window talks to GitHub (an editor that opened since
        # the list was made): it fetches by itself, and two fetches at once fail on the refs.
        r = sync.fetch(wait=0)
        if r is None:
            return sync.blocked_reason
    except RepoBusy:
        return None
    except (OSError, subprocess.SubprocessError) as e:
        return say(e)
    return None if r.returncode == 0 else ((r.stderr or r.stdout).strip()[-300:] or "git fetch failed")


def fetch_all() -> None:
    """Fetch every project's repository (see FETCH); one run at a time."""
    if not _fetch_lock.acquire(blocking=False):
        return
    try:
        # Running from now: working out the targets (each project's repository and GitHub
        # link) takes a while with many projects, and a page that saw "not running" then
        # would stop watching and never show what the fetch found.
        FETCH.update(running=True, done=0, total=0, errors={})
        targets = fetch_targets()
        FETCH["total"] = len(targets)
        with ThreadPoolExecutor(max_workers=4) as ex:
            for top, err in zip(targets, ex.map(fetch_one, targets)):
                FETCH["done"] += 1
                if err:
                    # A new dict, never one changed in place: the cards' requests read it
                    # meanwhile, from other threads.
                    FETCH["errors"] = {**FETCH["errors"], str(top): err}
        FETCH["at"] = time.time()
    finally:
        FETCH["running"] = False
        _fetch_lock.release()


def start_fetch_all() -> dict:
    if not _fetch_lock.locked():
        FETCH["running"] = True         # before the thread: the page's next look sees it
    threading.Thread(target=fetch_all, daemon=True).start()
    return {"ok": True}


def entry_for(pid: str) -> dict:
    for e in registry.load_projects()["projects"]:
        if registry.project_key(Path(e["path"])) == pid:
            return e
    raise NotFound("unknown project")


# ---------------------------------------------------------------- actions

def add_project(path: str, folder_id: str = "") -> dict:
    p = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"')))).resolve()
    if not p.is_dir():
        raise ValueError(f"not a folder: {p}")
    has_tex = any(p.glob("*.tex"))
    registry.update_projects(lambda data: _add(data, p, folder_id))
    return {"id": registry.project_key(p), "path": str(p), "has_tex": has_tex}


def _add(data: dict, p: Path, folder_id: str = "") -> None:
    entry = registry.find(data, p)
    if entry is None:
        entry = {"path": str(p), "added": time.time()}
        data["projects"].append(entry)
    if folder_id:
        entry["folder_id"] = _folder(data, folder_id)["id"]


# ---------------------------------------------------------------- folders and tags
#
# Folders only organize the list on the Home page (a research topic and its papers,
# say); nothing on disk moves. projects.json keeps
#     "folders": [{"id", "name", "parent", "note"}]     parent: a folder id or None
#     "tags":    {"<name>": {"color": "#rrggbb"}}       tags that have a color
# and each project entry may have "folder_id" and "tags": ["<name>", ...].

TAG_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _folder(data: dict, fid: str) -> dict:
    for f in data.get("folders", []):
        if f["id"] == fid:
            return f
    raise ValueError("unknown folder")


def _folder_name(body: dict) -> str:
    name = str(body.get("name") or "").strip()[:120]
    if not name:
        raise ValueError("the folder needs a name")
    return name


def _set_parent(data: dict, f: dict, parent) -> None:
    """Put folder `f` inside `parent` (a folder id, or empty for the top level)."""
    if not parent:
        f["parent"] = None
        return
    up = _folder(data, str(parent))
    while up is not None:
        if up["id"] == f["id"]:
            raise ValueError("a folder cannot go inside itself")
        up = _folder(data, up["parent"]) if up.get("parent") else None
    f["parent"] = str(parent)


def create_folder(body: dict) -> dict:
    def fn(data):
        f = {"id": secrets.token_hex(4), "name": _folder_name(body), "parent": None,
             "note": str(body.get("note") or "").strip()[:2000]}
        _set_parent(data, f, body.get("parent"))
        data.setdefault("folders", []).append(f)
        return dict(f)
    return registry.update_projects(fn)


def change_folder(body: dict) -> dict:
    def fn(data):
        f = _folder(data, str(body["id"]))
        if "name" in body:
            f["name"] = _folder_name(body)
        if "note" in body:
            f["note"] = str(body["note"] or "").strip()[:2000]
        if "parent" in body:
            _set_parent(data, f, body["parent"])
        return dict(f)
    return registry.update_projects(fn)


def remove_folder(fid: str) -> dict:
    """Delete a folder. Its projects and subfolders move up into its parent."""
    def fn(data):
        up = _folder(data, fid).get("parent")
        data["folders"] = [f for f in data["folders"] if f["id"] != fid]
        for f in data["folders"]:
            if f.get("parent") == fid:
                f["parent"] = up
        for e in data["projects"]:
            if e.get("folder_id") == fid:
                if up:
                    e["folder_id"] = up
                else:
                    del e["folder_id"]
        return {"ok": True}
    return registry.update_projects(fn)


# ---------------------------------------------------------------- a folder's repositories
#
# One repository shared by a folder's projects, or one each: what switching would do and
# doing it are in sharedrepo.py. Here the folder is looked up, and the projects locked.


def sync_owner(data: dict, fid: str) -> dict | None:
    """The folder (fid or one above it) whose choice applies to fid: the nearest one that
    chose a shared repository, else fid itself."""
    f = _folder(data, fid)
    up = f
    while up is not None:
        if (up.get("sync") or {}).get("mode") == "shared":
            return up
        up = _folder(data, up["parent"]) if up.get("parent") else None
    return f


def shared_repo_of(fid: str) -> Path | None:
    """The shared repository a project created in folder fid belongs to, if any."""
    if not fid:
        return None
    try:
        owner = sync_owner(registry.load_projects(), fid)
    except ValueError:
        return None
    sync = owner.get("sync") or {}
    return Path(sync["repo"]) if sync.get("mode") == "shared" and sync.get("repo") else None


def sync_plan(fid: str, mode: str | None = None, repo: str | None = None) -> dict:
    """What the folder's projects have now, and what choosing `mode` would do to each."""
    data = registry.load_projects()
    f = _folder(data, fid)
    owner = sync_owner(data, fid)
    if owner["id"] != fid:
        return {"inherited": {"id": owner["id"], "name": owner["name"], "repo": owner["sync"]["repo"]}}
    return sharedrepo.plan(data, f, mode, repo, default_parent(data))


def apply_sync(body: dict) -> dict:
    """Give the folder's projects one shared repository, or one each (see sync_plan)."""
    fid = str(body["id"])
    plan = sync_plan(fid, str(body.get("mode") or ""), str(body.get("repo") or ""))
    if "inherited" in plan:
        raise ValueError(f"this folder is part of the repository of “{plan['inherited']['name']}”")
    if plan["plan_mode"] == "shared":
        for r in plan["projects"]:
            if r["action"] == "blocked":
                raise ValueError(f"{r['name']}: {r['why']}; choose another folder for the repository")
    changing = [r for r in plan["projects"] if r["action"] in ("moves", "splits", "new")
                or (r["action"] == "stays" and r["kind"] != "shared")]
    open_ = [r["name"] for r in changing if r["running"]]
    if open_:
        raise ValueError("close the editors of these projects first: " + ", ".join(open_))
    if body.get("github") and not github.GITHUB.status().get("logged_in"):
        raise ValueError(github.GITHUB.status().get("error") or "GitHub CLI is not ready")
    targets = [Path(r["target"]) for r in changing if r.get("target")]
    with instances.exclusive([Path(r["path"]) for r in changing], targets=[Path(plan["repo"]), *targets]):
        # A competing Home request may have changed membership or paths before the locks.
        current = sync_plan(fid, str(body.get("mode") or ""), str(body.get("repo") or ""))
        def identity(p):
            return (p.get("plan_mode"), p.get("repo"), p.get("repo_kind"),
                    [(r["id"], r["path"], r.get("target"), r.get("action"), r.get("kind"))
                     for r in p.get("projects", [])])
        if identity(current) != identity(plan):
            raise ValueError("the folder changed; preview the operation again")
        return _apply_sync_locked(body, fid, plan)


def _apply_sync_locked(body: dict, fid: str, plan: dict) -> dict:
    report: list[str] = []
    data = registry.load_projects()
    f = _folder(data, fid)
    try:
        sharedrepo.apply(f, plan, body, report)
    except subprocess.CalledProcessError as e:
        report.append(f"git {e.cmd[1]} failed: {(e.stderr or e.stdout or '').strip()[-400:]}")
        return {"ok": False, "report": report}
    except (OSError, ValueError, RuntimeError) as e:           # RuntimeError: git, RepoBusy
        report.append(str(e))
        return {"ok": False, "report": report}
    finally:
        sync = f.get("sync")

        def fn(data):
            g = _folder(data, fid)
            if sync:
                g["sync"] = sync
        registry.update_projects(fn)
    for r in plan["projects"]:
        if r["action"] == "blocked":
            report.append(f"Skipped {r['name']}: {r['why']}")
    return {"ok": True, "report": report}


def clean_tags(tags) -> list[str]:
    """Tag names trimmed, without duplicates (ignoring case), in the order given."""
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    out, seen = [], set()
    for t in tags:
        t = re.sub(r"\s+", " ", str(t)).strip()[:40]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def change_tag(body: dict) -> dict:
    """Set a tag's color, or rename it on every project (merging into a tag of that name)."""
    name = str(body["name"])

    def fn(data):
        colors = data.setdefault("tags", {})
        if "color" in body:
            color = str(body["color"] or "")
            if color and not TAG_COLOR.fullmatch(color):
                raise ValueError(f"not a color: {color}")
            if color:
                colors[name] = {**colors.get(name, {}), "color": color}
            else:
                colors.pop(name, None)
        if "new_name" in body:
            new = (clean_tags([body["new_name"]]) or [""])[0]
            if not new:
                raise ValueError("the tag needs a name")
            for e in data["projects"]:
                if name in e.get("tags", []):
                    e["tags"] = clean_tags([new if t == name else t for t in e["tags"]])
            if name in colors and new != name:
                colors[new] = {**colors.pop(name), **colors.get(new, {})}
        return {"ok": True}
    return registry.update_projects(fn)


def remove_tag(name: str) -> dict:
    """Take a tag off every project."""
    def fn(data):
        for e in data["projects"]:
            if name in e.get("tags", []):
                e["tags"] = [t for t in e["tags"] if t != name]
                if not e["tags"]:
                    del e["tags"]
        data.get("tags", {}).pop(name, None)
        return {"ok": True}
    return registry.update_projects(fn)


# ---------------------------------------------------------------- accounts

def claude_status(refresh: bool = False) -> dict:
    """The account Claude Code is logged in to, and anything that would override it."""
    return claude_account(claude_bin(), None, max_age=0 if refresh else 30)


TEMPLATES = {
    "amsart": {
        "main.tex": r"""\documentclass[11pt]{amsart}
\usepackage{amsmath,amssymb,amsthm}
\usepackage{hyperref}
\usepackage[capitalize]{cleveref}

\newtheorem{theorem}{Theorem}[section]
\newtheorem{proposition}[theorem]{Proposition}
\newtheorem{lemma}[theorem]{Lemma}
\newtheorem{corollary}[theorem]{Corollary}
\theoremstyle{definition}
\newtheorem{definition}[theorem]{Definition}
\theoremstyle{remark}
\newtheorem{remark}[theorem]{Remark}

\title{@TITLE@}
\author{@AUTHOR@}

\begin{document}

\begin{abstract}
\end{abstract}

\maketitle

\input{sections/intro}

% Uncomment once the paper has a \cite:
% \bibliographystyle{amsplain}
% \bibliography{refs}
\end{document}
""",
        "sections/intro.tex": "\\section{Introduction}\\label{sec:intro}\n\n",
        "refs.bib": "% BibTeX entries for this paper.\n",
    },
    "article": {
        "main.tex": r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage{amsmath,amssymb}
\usepackage{hyperref}

\title{@TITLE@}
\author{@AUTHOR@}
\date{\today}

\begin{document}
\maketitle

\section{Introduction}\label{sec:intro}

\end{document}
""",
    },
    "empty": {
        "main.tex": "\\documentclass{article}\n\\begin{document}\n\n\\end{document}\n",
    },
}


def create_project(body: dict) -> dict:
    name = str(body.get("name", "")).strip()
    if not name or BAD_NAME.search(name) or name in (".", "..") or name.endswith((".", " ")):
        raise ValueError("choose a folder name without \\ / : * ? \" < > |")
    parent = Path(os.path.expanduser(str(body.get("parent", "")).strip().strip('"')))
    shared = shared_repo_of(str(body.get("folder_id") or ""))
    if shared and parent.is_absolute() and inside(parent.resolve(), shared):
        parent.mkdir(parents=True, exist_ok=True)        # a subfolder's folder in the repository
    if not parent.is_absolute() or not parent.is_dir():
        raise ValueError(f"location is not a folder: {parent}")
    tpl = TEMPLATES.get(body.get("template", "amsart"))
    if tpl is None:
        raise ValueError("unknown template")
    root = (parent / name).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"{root} already exists and is not empty")
    title = str(body.get("title") or "").strip() or name
    author = str(body.get("author") or "").strip()
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in tpl.items():           # \n line ends on every system, as git stores them
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(text.replace("@TITLE@", title).replace("@AUTHOR@", author).encode("utf-8"))
    (root / "prism.json").write_bytes((json.dumps({"main": "main.tex", "outdir": "build"},
                                                  indent=2) + "\n").encode("utf-8"))
    git_note, created = None, None
    if shared and inside(root, shared):
        # Part of the folder's repository: the editor records it there.
        body = {**body, "git": False, "github": False}
    if body.get("git") or body.get("github"):
        try:
            git_init(root)
        except (OSError, subprocess.SubprocessError) as e:
            git_note = f"git init failed: {e}"
    if body.get("github") and not git_note:
        created = create_github_repo(root, str(body.get("github_owner") or "").strip(),
                                     str(body.get("github_name") or ""))

    def fn(data):
        _add(data, root, str(body.get("folder_id") or ""))
        data["last_parent"] = str(parent.resolve())
    registry.update_projects(fn)
    return {"id": registry.project_key(root), "path": str(root), "git_note": git_note,
            "github": created}


def change_project(pid: str, body: dict) -> dict:
    def fn(data):
        for e in data["projects"]:
            if registry.project_key(Path(e["path"])) == pid:
                if "pinned" in body:
                    e["pinned"] = bool(body["pinned"])
                if "folder_id" in body:
                    if body["folder_id"]:
                        e["folder_id"] = _folder(data, str(body["folder_id"]))["id"]
                    else:
                        e.pop("folder_id", None)
                if "tags" in body:
                    e["tags"] = clean_tags(body["tags"])
                    if not e["tags"]:
                        del e["tags"]
                if "name" in body:
                    registry.set_entry_name(e, body["name"])
                return True
        return False
    if not registry.update_projects(fn):
        raise NotFound("unknown project")
    return {"ok": True}


def rename_project(pid: str, name: str, folder: bool) -> dict:
    """Rename a project in the list and, with `folder`, its folder on disk too (only while
    no editor runs in it: the editor renames a project it has open itself)."""
    root = Path(entry_for(pid)["path"])
    name = name.strip()
    new_folder = registry.folder_name(name)
    if not folder or not name or not new_folder or new_folder == root.name:
        change_project(pid, {"name": name})
        return {"id": pid, "path": str(root), "moved": False}
    if not root.is_dir():
        raise ValueError(f"folder not found: {root}")
    new = root.parent / new_folder
    with instances.exclusive([root], targets=[new]):
        if Path(entry_for(pid)["path"]) != root:
            raise ValueError("the project moved; refresh the project list")
        registry.move_folder(root, new)
        change_project(registry.project_key(new), {"name": name})
        warning = None
        try:
            gitsync.record_move(root, new)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            warning = (f"The folder was renamed, but the rename could not be committed ({e}). "
                       "Commit it from a terminal, so the other copies of the repository get it.")
    return {"id": registry.project_key(new), "path": str(new), "moved": True, "warning": warning}


def remove_project(pid: str) -> dict:
    """Forget a project. Its files are not touched."""
    def fn(data):
        before = len(data["projects"])
        data["projects"] = [e for e in data["projects"]
                            if registry.project_key(Path(e["path"])) != pid]
        return len(data["projects"]) < before
    if not registry.update_projects(fn):
        raise NotFound("unknown project")
    return {"ok": True}


def copy_project(pid: str) -> dict:
    """Put the project's folder on the system's clipboard, to paste in Explorer or Finder."""
    root = Path(entry_for(pid)["path"])
    if not root.is_dir():
        raise ValueError(f"folder not found: {root}")
    desktop.copy_files([root])
    return {"ok": True}


def paste_projects(folder_id: str = "") -> dict:
    """Add the folders copied in Explorer or Finder as projects (their files stay put)."""
    added, skipped = [], []
    for p in desktop.clipboard_files():
        if p.is_dir():
            added.append(add_project(str(p), folder_id))
        else:
            skipped.append(f"{p.name}: not a folder")
    if not added and not skipped:
        raise ValueError("the clipboard holds no folders; copy a project folder in Explorer or Finder")
    return {"added": added, "skipped": skipped}


def trash_project(pid: str) -> dict:
    """Move the project's folder to the Recycle Bin or Trash, and forget it. Not while its
    editor runs, nor a folder that holds the home folder, prism-local or other projects."""
    root = Path(entry_for(pid)["path"]).resolve()
    if not root.is_dir():
        raise ValueError(f"folder not found: {root}")
    keep = [Path.home().resolve(), Path(__file__).resolve().parents[1]]
    others = [Path(e["path"]).resolve() for e in registry.load_projects()["projects"]]
    if root == Path(root.anchor) or any(k == root or root in k.parents for k in keep) \
            or any(root in o.parents for o in others):
        raise ValueError(f"{root} holds more than this project; delete it in your file manager")
    with instances.exclusive([root]):
        desktop.trash([root])
    remove_project(pid)
    return {"ok": True}


def open_project(pid: str) -> dict:
    root = Path(entry_for(pid)["path"])
    if not root.is_dir():
        return {"error": f"folder not found: {root}"}
    r = instances.ensure(root)
    if "url" in r:
        registry.safe_touch(root)
    return r


PICK_FOLDER = r"""
import sys, tkinter, tkinter.filedialog
root = tkinter.Tk(); root.withdraw(); root.attributes("-topmost", True)
p = tkinter.filedialog.askdirectory(parent=root, initialdir=sys.argv[1] or None,
                                    title=sys.argv[2], mustexist=True)
sys.stdout.write(p or "")
"""


def native_folder_dialogs(start: str, title: str) -> list[list[str]]:
    """Folder dialogs of the desktop: zenity (GNOME and most others) or kdialog (KDE) on
    Linux, the Finder's on macOS. None on Windows, where tkinter's is the native one."""
    if sys.platform == "darwin":
        where = f" default location POSIX file {json.dumps(start)}" if start else ""
        return [["osascript", "-e", f"POSIX path of (choose folder with prompt "
                                    f"{json.dumps(title)}{where})"]]
    if os.name == "nt":
        return []
    cmds = []
    if shutil.which("zenity"):
        cmds.append(["zenity", "--file-selection", "--directory", f"--title={title}"]
                    + ([f"--filename={start.rstrip('/')}/"] if start else []))
    if shutil.which("kdialog"):
        cmds.append(["kdialog", "--getexistingdirectory", start or str(Path.home()),
                     "--title", title])
    return cmds


def pick_folder(start: str, title: str) -> dict:
    """A native folder dialog: the desktop's (native_folder_dialogs), else tkinter's, in a
    child process so that it has its own main loop."""
    title = title or "Choose a folder"
    for cmd in native_folder_dialogs(start, title):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=900)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode in (0, 1):                  # 1: cancelled
            p = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
            return {"path": str(Path(p).resolve()) if p else None}
    try:
        # No console window of its own on Windows (Home runs without one): only the dialog.
        r = subprocess.run([sys.executable, "-c", PICK_FOLDER, start or "", title or "Choose a folder"],
                           capture_output=True, timeout=900,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"folder dialog unavailable: {e}"}
    if r.returncode != 0:
        return {"error": "no folder dialog is available (on Linux, install zenity or python3-tk)"
                if os.name != "nt" else "folder dialog unavailable (tkinter missing?)"}
    p = r.stdout.decode("utf-8", "replace").strip()
    return {"path": str(Path(p).resolve()) if p else None}


# ---------------------------------------------------------------- http

class Handler(httpbase.Handler):
    """The Home page's requests. The checks every request passes are in httpbase.py."""

    server_version = "prism-home/1"
    presence = PRESENCE
    pages = {"/": "home.html", "/index.html": "home.html"}

    def get(self, path, q):
        if path == "/api/ping":
            return self._json({"app": "prism-home", "pid": os.getpid(),
                               "pages": PRESENCE.count(), "token": LIFETIME.token if LIFETIME else None})
        if path == "/api/projects":
            return self._json({**list_projects(),
                               "updated": httpbase.code_stamp() > STARTED_CODE + 1})
        if path == "/api/settings":
            return self._json({"settings": settings.load(),
                               "github": github.GITHUB.status(refresh=q.get("refresh") == "1"),
                               "claude": claude_status(q.get("refresh") == "1"),
                               "update": selfupdate.last()})
        if path == "/api/snippets":
            return self._json(settings.snippets())
        if path == "/api/folders/sync":
            return self._json(sync_plan(q["id"], q.get("mode"), q.get("repo")))
        if path == "/api/projects/publish":
            return self._json(publish_info(Path(entry_for(q["id"])["path"])))
        if path == "/api/fetch":
            return self._json({k: v for k, v in FETCH.items()})
        if path == "/api/git":
            return self._json({"git": git_info(Path(entry_for(q["id"])["path"]))})
        if path == "/api/git/all":
            ids = set(filter(None, q["ids"].split(","))) if "ids" in q else None
            return self._json({"git": all_git_info(ids)})
        if path == "/api/pdf":
            pdf, _, _ = pdf_path(Path(entry_for(q["id"])["path"]))
            if not pdf or not pdf.is_file():
                return self._err(404, "no PDF yet")
            return self._send(200, pdf.read_bytes(), "application/pdf")
        return self._err(404, "not found")

    def post(self, path, body):
        if path == "/api/state/restore":
            registry.restore_backup(str(body["file"]))
            return self._json({"ok": True})
        if path == "/api/settings":
            return self._json({"settings": settings.save(body), "github": github.GITHUB.status(),
                               "claude": claude_status()})
        if path == "/api/snippets":
            return self._json(settings.save_snippets(body.get("content")))
        if path == "/api/projects/add":
            return self._json(add_project(str(body["path"]), str(body.get("folder_id") or "")))
        if path == "/api/projects/create":
            return self._json(create_project(body))
        if path == "/api/projects/update":
            return self._json(change_project(body["id"], body))
        if path == "/api/projects/rename":
            return self._json(rename_project(body["id"], str(body.get("name") or ""),
                                             bool(body.get("folder"))))
        if path == "/api/projects/publish":
            r = publish_project(Path(entry_for(body["id"])["path"]), str(body.get("name") or ""),
                                str(body.get("owner") or ""), list(body.get("leave_out") or []))
            return self._json(r, 502 if "error" in r else 200)
        if path == "/api/projects/remove":
            return self._json(remove_project(body["id"]))
        if path == "/api/projects/trash":
            return self._json(trash_project(body["id"]))
        if path == "/api/projects/copy":
            return self._json(copy_project(body["id"]))
        if path == "/api/projects/paste":
            return self._json(paste_projects(str(body.get("folder_id") or "")))
        if path == "/api/fetch":
            return self._json(start_fetch_all())
        if path == "/api/folders/create":
            return self._json(create_folder(body))
        if path == "/api/folders/update":
            return self._json(change_folder(body))
        if path == "/api/folders/sync":
            return self._json(apply_sync(body))
        if path == "/api/folders/remove":
            return self._json(remove_folder(str(body["id"])))
        if path == "/api/tags/update":
            return self._json(change_tag(body))
        if path == "/api/tags/remove":
            return self._json(remove_tag(str(body["name"])))
        if path == "/api/restart":
            if RESTART["server"] is None:
                return self._err(409, "not ready to restart")
            if LIFETIME is None:
                return self._err(409, "not ready to restart")
            try:
                LIFETIME.transition()
            except (instances.Busy, TimeoutError, OSError) as exc:
                return self._err(409, str(exc))
            RESTART["again"] = True
            threading.Thread(target=RESTART["server"].shutdown, daemon=True).start()
            return self._json({"ok": True})
        if path == "/api/projects/open":
            r = open_project(body["id"])
            return self._json(r, 502 if "error" in r else 200)
        if path == "/api/projects/reveal":
            p = Path(entry_for(body["id"])["path"])
            if not p.is_dir():
                raise ValueError(f"folder not found: {p}")
            desktop.open_folder(p)
            return self._json({"ok": True})
        if path == "/api/pick-folder":
            return self._json(pick_folder(str(body.get("start") or ""),
                                          str(body.get("title") or "")))
        return self._err(404, "not found")


def main():
    httpbase.quiet_stdio()
    ap = argparse.ArgumentParser(description="prism-local Home: manage your LaTeX projects")
    ap.add_argument("--port", type=int, default=instances.HOME_PORT, help="port (0: any free port)")
    ap.add_argument("--port-tries", type=int, default=1,
                    help="if the port is taken, try this many ports upwards")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--exit-when-idle", action="store_true",
                    help="exit shortly after the last Home page is closed")
    ap.add_argument("--ready-file", type=Path,
                    help="once listening, write {pid, port, url} as JSON to this file")
    ap.add_argument("--idle-timings", help=argparse.SUPPRESS)
    ap.add_argument("--instance-token", help=argparse.SUPPRESS)
    a = ap.parse_args()
    global LIFETIME
    try:
        with instances.serving(None, a.ready_file, a.instance_token) as life:
            LIFETIME = life
            return serve(a, life)
    except (instances.Busy, TimeoutError) as exc:
        sys.exit(f"prism-home: {exc}")


def serve(a, life):
    if a.idle_timings:
        f, g, st = (float(x) for x in a.idle_timings.split(","))
        PRESENCE.first_wait, PRESENCE.grace, PRESENCE.stale = f, g, st
    try:
        srv = httpbase.listen(Handler, a.port, a.port_tries)
    except OSError as e:
        sys.exit(f"prism-home: {e}")
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"prism-local Home: {url}\n  projects: {registry.state_dir() / 'projects.json'}",
          flush=True)
    life.publish(url, srv.server_address[1])
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if a.exit_when_idle:
        threading.Thread(target=httpbase.idle_watchdog, args=(srv, PRESENCE), daemon=True).start()
    start_fetch_all()                   # what changed on GitHub since last time
    threading.Thread(target=selfupdate.update, daemon=True).start()   # prism-local itself
    httpbase.stop_on_signals(srv)
    RESTART["server"] = srv
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        transition = life.pending or life.transition()
        try:
            srv.server_close()
            life.unpublish()
            if RESTART["again"]:
                argv = [sys.executable, str(Path(__file__).resolve()), "--port",
                        str(srv.server_address[1]), "--port-tries", "1", "--no-browser"]
                if a.exit_when_idle:
                    argv.append("--exit-when-idle")
                if a.idle_timings:
                    argv += ["--idle-timings", a.idle_timings]
                if a.ready_file:
                    argv += ["--ready-file", str(a.ready_file)]
                result = transition.handover(argv, str(Path.home()))
                httpbase.log("restart failed: " + result["error"] if "error" in result
                             else "restart handed to the successor")
            else:
                httpbase.log("stopped")
        finally:
            transition.close()


if __name__ == "__main__":
    sys.exit(main())
