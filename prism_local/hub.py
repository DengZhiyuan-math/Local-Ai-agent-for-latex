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
import httpbase  # noqa: E402
import registry  # noqa: E402
from backend_claude import claude_account, claude_bin  # noqa: E402
from fsutil import EDITABLE_SUFFIXES, SKIP_DIRS  # noqa: E402
from presence import Presence  # noqa: E402
from proc import NO_WINDOW  # noqa: E402
from texutil import tex_title  # noqa: E402

PRESENCE = Presence()
MAX_SCAN = 3000
BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------- project facts

def read_prism_json(root: Path) -> dict:
    try:
        data = json.loads((root / "prism.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def guess_main(root: Path) -> str | None:
    if (root / "main.tex").is_file():
        return "main.tex"
    for p in sorted(root.glob("*.tex")):
        try:
            if not p.name.startswith("._") and \
                    "\\documentclass" in p.read_text(encoding="utf-8", errors="replace")[:5000]:
                return p.name
        except OSError:
            pass
    return None


def scan_sources(root: Path, outdir: str) -> tuple[int, float | None]:
    """How many editable source files the project has, and the newest mtime."""
    count, newest = 0, None
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root).as_posix()
        rel = "" if rel == "." else rel + "/"
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
                       and rel + d != outdir]
        for f in filenames:
            if Path(f).suffix in EDITABLE_SUFFIXES and not f.startswith("._"):
                count += 1
                try:
                    t = os.stat(os.path.join(dirpath, f)).st_mtime
                    newest = t if newest is None or t > newest else newest
                except OSError:
                    pass
        if count > MAX_SCAN:
            break
    return count, newest


def pdf_path(root: Path) -> tuple[Path | None, str | None, str]:
    cfg = read_prism_json(root)
    main = cfg.get("main") or guess_main(root)
    outdir = str(cfg.get("outdir") or "build").strip("/") or "build"
    if not main:
        return None, None, outdir
    return root / outdir / f"{Path(main).stem}.pdf", main, outdir


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
    files, edited = scan_sources(root, outdir)
    d.update(main=main, title=tex_title(root / main) if main else None,
             files=files, edited=edited,
             pdf_mtime=pdf.stat().st_mtime if pdf and pdf.is_file() else None)
    # Only a look: a server that is slow to answer right now keeps its instance file.
    inst = registry.running_instance(registry.instance_file(d["id"]), "prism-local", root,
                                     timeout=0.8, cleanup=False)
    if inst:
        d["running"] = {"url": inst["url"], "pages": inst.get("pages", 0)}
    return d


def list_projects() -> dict:
    data = registry.load_projects()
    entries = data["projects"]
    with ThreadPoolExecutor(max_workers=8) as ex:
        projects = list(ex.map(project_info, entries))
    return {"projects": projects, "folders": data.get("folders", []),
            "tags": data.get("tags", {}), "default_parent": default_parent(data),
            "home": str(Path.home())}


def default_parent(data: dict) -> str:
    chosen = load_settings()["default_parent"]
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
    def git(*args):      # read-only, and never holding .git/index.lock (see server.git)
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=root, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=8, **NO_WINDOW)
    try:
        top = git("rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return None
        toplevel = Path(top.stdout.strip())
        shared = None
        if registry.norm(toplevel.resolve()) != registry.norm(root.resolve()):
            if not gitsync.shared_marker(toplevel):
                return {"nested": True, "toplevel": toplevel.name, "toplevel_path": str(toplevel)}
            shared = {"shared": toplevel.name, "toplevel_path": str(toplevel)}
        r = git("status", "--porcelain", "-b", "--", ".")
        remote = git("remote", "get-url", "origin").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    lines = r.stdout.splitlines()
    head = lines[0][3:] if lines and lines[0].startswith("## ") else ""
    head = re.sub(r"^(No commits yet on|Initial commit on) ", "", head)
    branch = re.split(r"\.\.\.| ", head)[0] if head else ""
    ahead = re.search(r"ahead (\d+)", head)
    behind = re.search(r"behind (\d+)", head)
    web = None
    m = re.match(r"(?:https://github\.com/|git@github\.com:)(.+?)(?:\.git)?$", remote)
    if m:
        web = "https://github.com/" + m.group(1)
    return {"branch": branch, "changes": len(lines) - 1,
            "ahead": int(ahead.group(1)) if ahead else 0,
            "behind": int(behind.group(1)) if behind else 0, "github": web, **(shared or {})}


def entry_for(pid: str) -> dict:
    for e in registry.load_projects()["projects"]:
        if registry.project_key(Path(e["path"])) == pid:
            return e
    raise KeyError("unknown project")


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
# A folder chooses how its projects (its own and its subfolders') are kept on GitHub:
#     "separate"  every project has a repository of its own (the default);
#     "shared"    one repository for all of them: the projects sit in one folder on disk
#                 (subfolders become subdirectories), its top has gitsync.SHARED_MARKER,
#                 and each editor records its own project there (gitsync.py).
# The folder keeps {"sync": {"mode", "repo"}}. Switching never deletes anything: a project's
# own history comes along into the shared repository, and back out of it (git subtree
# split); a .git that is replaced is set aside, renamed.

SPLIT_BACKUP = ".git-prism-separate"    # a project's own .git, after it joined a shared repository
SHARED_BACKUP = ".git-prism-shared"     # a shared repository's .git, after its projects split up
SHARED_IGNORE = ["build/", ".git-prism-*/", "*.prism-tmp", ".DS_Store",
                 *(f"*{ext}" for ext in gitsync.AUX)]


def disk_name(name: str) -> str:
    """A folder name for disk from a Home folder's name ("Paper A: ergodicity")."""
    return BAD_NAME.sub("-", name).strip(" .") or "folder"


def inside(p: Path, top: Path) -> bool:
    """Whether p is top or below it."""
    return registry.norm(p) == registry.norm(top) or \
        registry.norm(p).startswith(registry.norm(top).rstrip("\\/") + os.sep)


def repo_top(p: Path) -> Path | None:
    try:
        r = subprocess.run(["git", "--no-optional-locks", "rev-parse", "--show-toplevel"], cwd=p,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=20, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None


def repo_kind(p: Path) -> tuple[str, Path | None]:
    """own | shared | nested (inside some other repository) | none, and the repository's top."""
    top = repo_top(p) if p.is_dir() else None
    if top is None:
        return "none", None
    if registry.norm(top) == registry.norm(p.resolve()):
        return "own", top
    return ("shared" if gitsync.shared_marker(top) else "nested"), top


def github_of(top: Path | None) -> str | None:
    if top is None:
        return None
    try:
        remote = run_git(top, "remote", "get-url", "origin", timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"(?:https://github\.com/|git@github\.com:)(.+?)(?:\.git)?/?$", remote)
    return "https://github.com/" + m.group(1) if m else (remote or None)


def folder_projects(data: dict, fid: str) -> list[tuple[dict, list[str]]]:
    """The projects in folder fid and its subfolders, with the subfolders' path from fid."""
    out = []

    def walk(f: str, parts: list[str]) -> None:
        out.extend((e, parts) for e in data["projects"] if e.get("folder_id") == f)
        for c in sorted((c for c in data.get("folders", []) if c.get("parent") == f),
                        key=lambda c: c["name"].lower()):
            walk(c["id"], parts + [disk_name(c["name"])])
    walk(fid, [])
    return out


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


def is_running(root: Path) -> bool:
    return registry.running_instance(registry.instance_file(registry.project_key(root)),
                                     "prism-local", root, timeout=0.8, cleanup=False) is not None


def default_repo(data: dict, f: dict, entries: list[dict]) -> Path:
    """Next to the projects when they share a parent folder, else in the default location."""
    parents = {registry.norm(Path(e["path"]).parent): Path(e["path"]).parent for e in entries}
    base = next(iter(parents.values())) if len(parents) == 1 else Path(default_parent(data))
    return base / disk_name(f["name"])


def sync_plan(fid: str, mode: str | None = None, repo: str | None = None) -> dict:
    """What the folder's projects have now, and what choosing `mode` would do to each."""
    data = registry.load_projects()
    f = _folder(data, fid)
    owner = sync_owner(data, fid)
    if owner["id"] != fid:
        return {"inherited": {"id": owner["id"], "name": owner["name"], "repo": owner["sync"]["repo"]}}
    cur = f.get("sync") or {}
    mode = mode or cur.get("mode") or "separate"
    if mode not in ("shared", "separate"):
        raise ValueError("unknown sync mode")
    items = folder_projects(data, fid)
    repo = (repo or "").strip().strip('"')
    top = Path(os.path.expanduser(repo)).resolve() if repo else \
        Path(cur["repo"]) if cur.get("repo") else default_repo(data, f, [e for e, _ in items])
    if not top.is_absolute():
        raise ValueError("choose a full path for the repository's folder")
    top_kind, top_top = repo_kind(top) if top.is_dir() else ("none", None)
    rows, taken = [], set()
    for e, parts in items:
        root = Path(e["path"])
        row = {"id": registry.project_key(root), "name": e.get("name") or root.name,
               "path": str(root), "exists": root.is_dir(), "running": False, "sub": "/".join(parts)}
        if not row["exists"]:
            row.update(kind="missing", action="skip")
            rows.append(row)
            continue
        kind, ktop = repo_kind(root)
        row.update(kind=kind, running=is_running(root), github=github_of(ktop) if kind in ("own", "shared") else None,
                   repo=str(ktop) if ktop else None)
        if mode == "shared":
            if inside(root, top):
                row.update(action="stays", target=str(root))
            else:
                dst = top.joinpath(*parts, root.name)
                n = 2
                while dst.exists() or registry.norm(dst) in taken:
                    dst = top.joinpath(*parts, f"{root.name} ({n})")
                    n += 1
                taken.add(registry.norm(dst))
                row.update(action="moves", target=str(dst))
            if inside(top, root):
                row["action"] = "blocked"
                row["why"] = "the repository's folder would be inside this project"
            row["history"] = kind == "own"
        else:
            row["action"] = {"own": "keeps", "shared": "splits", "none": "new", "nested": "blocked"}[kind]
            if kind == "nested":
                row["why"] = f"it is inside another repository ({ktop}); move it out first"
        rows.append(row)
    others = []
    if mode == "shared" and top.is_dir():
        targets = {registry.norm(Path(r.get("target", r["path"]))) for r in rows}
        others = [c.name for c in top.iterdir() if not c.name.startswith(".")
                  and c.name not in (gitsync.SHARED_MARKER, ".gitignore")
                  and registry.norm(c) not in targets and not any(t.startswith(registry.norm(c) + os.sep) for t in targets)]
    return {"folder": f["name"], "mode": cur.get("mode") or "separate", "plan_mode": mode,
            "repo": str(top), "repo_exists": top.is_dir(), "repo_kind": top_kind,
            "repo_github": github_of(top) if top_kind == "own" else None,
            "repo_inside": str(top_top) if top_kind in ("shared", "nested") else None,
            "others": others[:20], "projects": rows, "github": gh_status()}


def _repoint(pid: str, new: Path) -> None:
    """The project moved on disk: the list follows it (folder, tags and all)."""
    def fn(data):
        for e in data["projects"]:
            if registry.project_key(Path(e["path"])) == pid:
                e["path"] = str(new)
    registry.update_projects(fn)


def _ensure_ignore(top: Path) -> None:
    gi = top / ".gitignore"
    have = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    add = [x for x in SHARED_IGNORE if x not in have]
    if add:
        text = "\n".join(have + add) + "\n"
        gi.write_bytes(text.lstrip("\n").encode("utf-8"))


def _publish(root: Path, github: bool, owner: str, name: str, report: list[str]) -> str | None:
    """Push to origin, or create a private GitHub repository for `root` first."""
    url = github_of(root)
    if url:
        try:
            run_git(root, "push", "-q", "-u", "origin", "HEAD", timeout=300)
        except subprocess.CalledProcessError as e:
            report.append(f"Not pushed to {url}: {(e.stderr or e.stdout or '').strip()[-300:]}")
        return url
    if not github:
        return None
    r = create_github_repo(root, owner, name)
    if "error" in r:
        report.append(f"GitHub repository for {root.name} not created: {r['error']}")
        return None
    report.append(f"Created {r['url']}")
    return r["url"]


def _apply_shared(f: dict, plan: dict, body: dict, report: list[str]) -> None:
    top = Path(plan["repo"])
    if plan["repo_kind"] == "shared" and plan["repo_inside"] and \
            registry.norm(plan["repo_inside"]) != registry.norm(top):
        raise ValueError(f"{top} is inside the repository {plan['repo_inside']}; choose another folder")
    top.mkdir(parents=True, exist_ok=True)
    if repo_kind(top)[0] != "own":
        git_init(top)
        report.append(f"New repository in {top}")
    _ensure_ignore(top)
    marker = top / gitsync.SHARED_MARKER
    if not gitsync.shared_marker(top):
        marker.write_bytes((json.dumps({"shared": True, "folder": f["name"]}, ensure_ascii=False,
                                       indent=2) + "\n").encode("utf-8"))
    imports = []
    for r in plan["projects"]:
        if r["action"] not in ("moves", "stays"):
            continue
        root = Path(r["path"])
        if r["action"] == "moves":
            dst = Path(r["target"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root), str(dst))
            _repoint(r["id"], dst)
            report.append(f"Moved {root.name} to {dst}")
            root = dst
        dot = root / ".git"
        if dot.is_file():
            raise ValueError(f"{root.name} is a git worktree or submodule; set it up by hand")
        if dot.is_dir() and r["kind"] == "own":
            ref = f"refs/prism/imported/{r['id']}"
            got = subprocess.run(["git", "fetch", "-q", "--no-tags", str(root), f"+HEAD:{ref}"], cwd=top,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=300, **NO_WINDOW)
            if got.returncode == 0:
                imports.append(run_git(top, "rev-parse", ref).stdout.strip())
            if (root / SPLIT_BACKUP).exists():
                raise ValueError(f"{root / SPLIT_BACKUP} is in the way")
            os.replace(dot, root / SPLIT_BACKUP)
            report.append(f"{root.name}: its history is kept in the shared repository; "
                          f"its own .git is now {SPLIT_BACKUP}")
    run_git(top, "add", "-A", timeout=300)
    staged = run_git(top, "diff", "--cached", "--name-only", "-z", timeout=60).stdout.split("\0")
    big = [x for x in staged if x and (top / x).is_file() and (top / x).stat().st_size > gitsync.MAX_FILE]
    if big:
        run_git(top, "rm", "-q", "--cached", "--", *big)
        report.append("Not committed, too large for GitHub: " + ", ".join(big))
    tree = run_git(top, "write-tree").stdout.strip()
    head = subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD^{commit}"], cwd=top, capture_output=True,
                          text=True, **NO_WINDOW).stdout.strip()
    old_tree = run_git(top, "rev-parse", "HEAD^{tree}").stdout.strip() if head else None
    if imports or tree != old_tree:
        parents = [x for h in ([head] if head else []) + imports for x in ("-p", h)]
        n = len(plan["projects"])
        msg = f"{f['name']}: {n} project{'s' if n != 1 else ''} in one repository"
        sha = run_git(top, "commit-tree", tree, *parents, "-m", msg).stdout.strip()
        run_git(top, "update-ref", "HEAD", sha)
    _publish(top, bool(body.get("github")), str(body.get("github_owner") or ""),
             str(body.get("github_name") or "") or repo_name(top.name), report)
    f["sync"] = {"mode": "shared", "repo": str(top)}


def _init_own(root: Path) -> None:
    """A repository of the project's own, which leaves a set-aside .git out of it."""
    git_init(root)
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    have = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".git-prism-*/" not in have:
        lines = [*have.splitlines(), ".git-prism-*/"]
        exclude.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _apply_separate(f: dict, plan: dict, body: dict, report: list[str]) -> None:
    rows = [r for r in plan["projects"] if r["action"] in ("keeps", "splits", "new")]
    tops: dict[str, list[dict]] = {}
    for r in rows:
        if r["action"] == "splits":
            tops.setdefault(r["repo"], []).append(r)
    data = registry.load_projects()
    for top_s, members in tops.items():
        top, ids = Path(top_s), {r["id"] for r in members}
        strangers = [e.get("name") or Path(e["path"]).name for e in data["projects"]
                     if inside(Path(e["path"]), top) and registry.project_key(Path(e["path"])) not in ids]
        if strangers:
            raise ValueError(f"the repository {top} also holds {', '.join(strangers[:5])}, "
                             "which are not in this folder; move them in first")
        if (top / SHARED_BACKUP).exists():
            raise ValueError(f"{top / SHARED_BACKUP} is in the way")
        # Each project's history, cut out of the shared one.
        for r in members:
            prefix = Path(r["path"]).relative_to(top).as_posix()
            try:
                # quotepath: git subtree compares the prefix with paths git prints, and
                # would print a non-ASCII path such as 练习/ex 1 escaped otherwise.
                sha = run_git(top, "-c", "core.quotepath=false", "subtree", "split", "-q",
                              f"--prefix={prefix}", timeout=600).stdout.strip()
                run_git(top, "update-ref", f"refs/prism/split/{r['id']}", sha)
                r["split"] = f"refs/prism/split/{r['id']}"
            except subprocess.CalledProcessError as e:
                if "no new revisions" in (e.stderr or ""):        # never committed: nothing to carry
                    continue
                report.append(f"{Path(r['path']).name}: history not carried over "
                              f"({(e.stderr or '').strip()[-200:]}); it starts afresh")
        os.replace(top / ".git", top / SHARED_BACKUP)
        try:
            (top / gitsync.SHARED_MARKER).unlink()
        except OSError:
            pass
        report.append(f"The shared repository in {top} is set aside as {SHARED_BACKUP}")
        for r in members:
            root = Path(r["path"])
            _init_own(root)
            if r.get("split"):
                run_git(root, "fetch", "-q", "--no-tags", str(top / SHARED_BACKUP), r["split"], timeout=300)
                run_git(root, "reset", "-q", "FETCH_HEAD")
            report.append(f"{root.name}: a repository of its own" + (", with its history" if r.get("split") else ""))
    for r in rows:
        if r["action"] == "new":
            _init_own(Path(r["path"]))
            report.append(f"{Path(r['path']).name}: new repository")
    owner = str(body.get("github_owner") or "")
    for r in rows:
        root = Path(r["path"])
        if body.get("github") or github_of(root):
            _publish(root, bool(body.get("github")), owner, repo_name(root.name), report)
    f["sync"] = {"mode": "separate"}


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
    if body.get("github") and not gh_status().get("logged_in"):
        raise ValueError(gh_status().get("error") or "GitHub CLI is not ready")
    report: list[str] = []
    data = registry.load_projects()
    f = _folder(data, fid)
    try:
        (_apply_shared if plan["plan_mode"] == "shared" else _apply_separate)(f, plan, body, report)
    except subprocess.CalledProcessError as e:
        report.append(f"git {e.cmd[1]} failed: {(e.stderr or e.stdout or '').strip()[-400:]}")
        return {"ok": False, "report": report}
    except (OSError, ValueError) as e:
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


# ---------------------------------------------------------------- settings

SETTINGS_DEFAULTS = {"default_parent": "", "git_init": True, "github_repo": False,
                     "github_owner": "", "claude_account": "",
                     "claude_config_dir": ""}
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


def settings_path() -> Path:
    return registry.state_dir() / "settings.json"


def load_settings() -> dict:
    data = registry.read_json(settings_path()) or {}
    return {k: data.get(k, v) for k, v in SETTINGS_DEFAULTS.items()}


def save_settings(body: dict) -> dict:
    cur = load_settings()
    if "default_parent" in body:
        p = str(body["default_parent"] or "").strip().strip('"')
        if p and not Path(os.path.expanduser(p)).is_dir():
            raise ValueError(f"not a folder: {p}")
        cur["default_parent"] = str(Path(os.path.expanduser(p)).resolve()) if p else ""
    for k in ("git_init", "github_repo"):
        if k in body:
            cur[k] = bool(body[k])
    if "claude_account" in body:
        acct = str(body["claude_account"] or "").strip()
        if acct and not EMAIL_RE.fullmatch(acct):
            raise ValueError("the Claude account must be an email address")
        cur["claude_account"] = acct
    if "claude_config_dir" in body:
        d = str(body["claude_config_dir"] or "").strip().strip('"')
        if d and not Path(os.path.expanduser(d)).is_dir():
            raise ValueError(f"not a folder: {d}")
        cur["claude_config_dir"] = str(Path(os.path.expanduser(d)).resolve()) if d else ""
    if "github_owner" in body:
        owner = str(body["github_owner"] or "").strip()
        if owner and not OWNER_RE.fullmatch(owner):
            raise ValueError("not a GitHub user or organization name")
        cur["github_owner"] = owner
    registry.write_json(settings_path(), cur)
    return cur


# ---------------------------------------------------------------- GitHub

_gh_cache: dict = {}


def gh_status(refresh: bool = False) -> dict:
    """Whether the GitHub CLI (gh) is installed and logged in, and as whom."""
    if _gh_cache and not refresh and time.time() - _gh_cache["at"] < 120:
        return _gh_cache["status"]
    exe = shutil.which("gh")
    status = {"gh": exe, "logged_in": False, "account": None}
    if not exe:
        status["error"] = "GitHub CLI not found. Install it from https://cli.github.com, then run: gh auth login"
    else:
        try:
            r = subprocess.run([exe, "auth", "status", "--hostname", "github.com"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=20, env={**os.environ, "GH_PROMPT_DISABLED": "1"},
                               **NO_WINDOW)
            out = r.stdout + r.stderr
            m = re.search(r"Logged in to github\.com (?:account|as) (\S+)", out)
            if m and r.returncode == 0:
                status.update(logged_in=True, account=m.group(1).strip("()"))
            else:
                status["error"] = "GitHub CLI is not logged in. Run: gh auth login"
        except (OSError, subprocess.SubprocessError) as e:
            status["error"] = f"gh auth status failed: {e}"
    _gh_cache.update(at=time.time(), status=status)
    return status


def claude_status(refresh: bool = False) -> dict:
    """The account Claude Code is logged in to, and anything that would override it."""
    return claude_account(claude_bin(), None, max_age=0 if refresh else 30)


def repo_name(folder: str) -> str:
    """A GitHub repository name for a folder name: letters, digits, '.', '-', '_'."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", folder).strip("-.")
    return name[:100] or "latex-project"


def run_git(root: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **NO_WINDOW)


def git_init(root: Path) -> None:
    """A repository of the project's own, with build output ignored."""
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_bytes(GITIGNORE.encode("utf-8"))
    if not (root / ".git").exists():
        try:
            run_git(root, "init", "-q", "-b", "main")
        except subprocess.CalledProcessError:          # git older than 2.28
            run_git(root, "init", "-q")


def create_github_repo(root: Path, owner: str = "", name: str = "") -> dict:
    """Commit the project and push it to a new private GitHub repository.

    Returns {"url"} or {"error"}. The project itself is never removed on failure."""
    st = gh_status()
    if not st.get("logged_in"):
        return {"error": st.get("error") or "GitHub CLI is not ready"}
    if owner and not OWNER_RE.fullmatch(owner):
        return {"error": "not a GitHub user or organization name"}
    name = name.strip() or repo_name(root.name)
    if not REPO_RE.fullmatch(name):
        return {"error": f"not a GitHub repository name: {name}"}
    full = f"{owner}/{name}" if owner else name
    try:
        git_init(root)
        run_git(root, "add", "-A")
        if run_git(root, "status", "--porcelain").stdout.strip():
            run_git(root, "commit", "-q", "-m", "Initial commit")
        r = subprocess.run([st["gh"], "repo", "create", full, "--private", "--source", ".",
                            "--remote", "origin", "--push"],
                           cwd=root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180,
                           env={**os.environ, "GH_PROMPT_DISABLED": "1"}, **NO_WINDOW)
    except subprocess.CalledProcessError as e:
        return {"error": f"git {e.cmd[1]} failed: {(e.stderr or e.stdout or '').strip()[:400]}"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)}
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()[:500] or "gh repo create failed"}
    m = re.search(r"https://github\.com/\S+", r.stdout + r.stderr)
    url = m.group(0).rstrip(".") if m else f"https://github.com/{owner or st['account']}/{name}"
    return {"url": url}


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
GITIGNORE = "build/\n*.prism-tmp\n.DS_Store\n"


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
    git_note, github = None, None
    if shared and inside(root, shared):
        # Part of the folder's repository: the editor records it there.
        body = {**body, "git": False, "github": False}
    if body.get("git") or body.get("github"):
        try:
            git_init(root)
        except (OSError, subprocess.SubprocessError) as e:
            git_note = f"git init failed: {e}"
    if body.get("github") and not git_note:
        github = create_github_repo(root, str(body.get("github_owner") or "").strip(),
                                    str(body.get("github_name") or ""))

    def fn(data):
        _add(data, root, str(body.get("folder_id") or ""))
        data["last_parent"] = str(parent.resolve())
    registry.update_projects(fn)
    return {"id": registry.project_key(root), "path": str(root), "git_note": git_note,
            "github": github}


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
                    name = str(body["name"] or "").strip()[:120]
                    if name and name != Path(e["path"]).name:
                        e["name"] = name
                    else:
                        e.pop("name", None)
                return True
        return False
    if not registry.update_projects(fn):
        raise KeyError("unknown project")
    return {"ok": True}


def remove_project(pid: str) -> dict:
    """Forget a project. Its files are not touched."""
    def fn(data):
        before = len(data["projects"])
        data["projects"] = [e for e in data["projects"]
                            if registry.project_key(Path(e["path"])) != pid]
        return len(data["projects"]) < before
    if not registry.update_projects(fn):
        raise KeyError("unknown project")
    return {"ok": True}


def open_project(pid: str) -> dict:
    root = Path(entry_for(pid)["path"])
    if not root.is_dir():
        return {"error": f"folder not found: {root}"}
    r = registry.ensure_server(root)
    if "url" in r:
        registry.safe_touch(root)
    return r


def reveal(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


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
        r = subprocess.run([sys.executable, "-c", PICK_FOLDER, start or "", title or "Choose a folder"],
                           capture_output=True, timeout=900,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
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
        try:
            if path == "/api/ping":
                return self._json({"app": "prism-home", "pid": os.getpid(),
                                   "pages": PRESENCE.count()})
            if path == "/api/projects":
                return self._json(list_projects())
            if path == "/api/settings":
                return self._json({"settings": load_settings(),
                                   "github": gh_status(refresh=q.get("refresh") == "1"),
                                   "claude": claude_status(q.get("refresh") == "1")})
            if path == "/api/folders/sync":
                return self._json(sync_plan(q["id"], q.get("mode"), q.get("repo")))
            if path == "/api/git":
                return self._json({"git": git_info(Path(entry_for(q["id"])["path"]))})
            if path == "/api/pdf":
                pdf, _, _ = pdf_path(Path(entry_for(q["id"])["path"]))
                if not pdf or not pdf.is_file():
                    return self._err(404, "no PDF yet")
                return self._send(200, pdf.read_bytes(), "application/pdf")
            return self._err(404, "not found")
        except KeyError as e:
            if e.args[0] == "unknown project":
                return self._err(404, "unknown project")
            return self._err(400, f"missing {e.args[0]}")
        except (ValueError, OSError) as e:
            return self._err(400, str(e))

    def post(self, path, body):
        try:
            if path == "/api/settings":
                return self._json({"settings": save_settings(body), "github": gh_status(),
                                   "claude": claude_status()})
            if path == "/api/projects/add":
                return self._json(add_project(str(body["path"]), str(body.get("folder_id") or "")))
            if path == "/api/projects/create":
                return self._json(create_project(body))
            if path == "/api/projects/update":
                return self._json(change_project(body["id"], body))
            if path == "/api/projects/remove":
                return self._json(remove_project(body["id"]))
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
            if path == "/api/projects/open":
                r = open_project(body["id"])
                return self._json(r, 502 if "error" in r else 200)
            if path == "/api/projects/reveal":
                p = Path(entry_for(body["id"])["path"])
                if not p.is_dir():
                    raise ValueError(f"folder not found: {p}")
                reveal(p)
                return self._json({"ok": True})
            if path == "/api/pick-folder":
                return self._json(pick_folder(str(body.get("start") or ""),
                                              str(body.get("title") or "")))
            return self._err(404, "not found")
        except KeyError as e:
            if e.args[0] == "unknown project":
                return self._err(404, "unknown project")
            return self._err(400, f"missing {e.args[0]}")
        except (ValueError, OSError, TimeoutError) as e:
            return self._err(400, str(e))


def main():
    httpbase.quiet_stdio()
    ap = argparse.ArgumentParser(description="prism-local Home: manage your LaTeX projects")
    ap.add_argument("--port", type=int, default=registry.HOME_PORT, help="port (0: any free port)")
    ap.add_argument("--port-tries", type=int, default=1,
                    help="if the port is taken, try this many ports upwards")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--exit-when-idle", action="store_true",
                    help="exit shortly after the last Home page is closed")
    ap.add_argument("--ready-file", type=Path,
                    help="once listening, write {pid, port, url} as JSON to this file")
    ap.add_argument("--idle-timings", help=argparse.SUPPRESS)
    a = ap.parse_args()
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
    if a.ready_file:
        httpbase.write_ready_file(a.ready_file, {"app": "prism-home", "pid": os.getpid(),
                                                 "port": srv.server_address[1], "url": url})
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if a.exit_when_idle:
        threading.Thread(target=httpbase.idle_watchdog, args=(srv, PRESENCE), daemon=True).start()
    httpbase.stop_on_signals(srv)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if a.ready_file:
            httpbase.remove_ready_file(a.ready_file)
        httpbase.log("stopped")


if __name__ == "__main__":
    sys.exit(main())

