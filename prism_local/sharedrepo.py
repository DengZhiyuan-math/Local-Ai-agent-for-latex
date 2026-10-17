"""A Home folder's repositories: one shared by all its projects, or one each.

A folder chooses how its projects (its own and its subfolders') are kept on GitHub:
    "separate"  every project has a repository of its own (the default);
    "shared"    one repository for all of them: the projects sit in one folder on disk
                (subfolders become subdirectories), its top has gitsync.SHARED_MARKER,
                and each editor records its own project there (gitsync.py).
The folder keeps {"sync": {"mode", "repo"}}. Switching never deletes anything: a project's
own history comes along into the shared repository, and back out of it (git subtree
split); a .git that is replaced is set aside, renamed.

plan() works out what choosing a mode would do to each project, changing nothing;
apply() does it, and says each step it took in the report. hub.py looks the folder up,
takes the locks and checks that the plan is still the one the page showed
(hub.sync_plan, hub.apply_sync).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import gitenv
import github
import gitsync
import instances
import registry
from fsutil import write_bytes
from publish import (create_github_repo, ensure_ignore, git_init, github_of, project_sync, repo_kind,
                     repo_name, run_git, tell_editor, unstage_big)
from repository import Repository

BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPLIT_BACKUP = ".git-prism-separate"    # a project's own .git, after it joined a shared repository
SHARED_BACKUP = ".git-prism-shared"     # a shared repository's .git, after its projects split up


def disk_name(name: str) -> str:
    """A folder name for disk from a Home folder's name ("Paper A: ergodicity")."""
    return BAD_NAME.sub("-", name).strip(" .") or "folder"


inside = registry.inside


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


def is_running(root: Path) -> bool:
    return instances.inspect(root, timeout=0.8).occupied


def default_repo(f: dict, entries: list[dict], default_parent: str) -> Path:
    """Next to the projects when they share a parent folder, else in the default location."""
    parents = {registry.norm(Path(e["path"]).parent): Path(e["path"]).parent for e in entries}
    base = next(iter(parents.values())) if len(parents) == 1 else Path(default_parent)
    return base / disk_name(f["name"])


# ---------------------------------------------------------------- plan

def plan(data: dict, f: dict, mode: str | None, repo: str | None, default_parent: str) -> dict:
    """What folder f's projects have now, and what choosing `mode` would do to each.
    Nothing is changed. default_parent: where a new repository goes when the projects
    are in different places."""
    cur = f.get("sync") or {}
    mode = mode or cur.get("mode") or "separate"
    if mode not in ("shared", "separate"):
        raise ValueError("unknown sync mode")
    items = folder_projects(data, f["id"])
    repo = (repo or "").strip().strip('"')
    top = Path(os.path.expanduser(repo)).resolve() if repo else \
        Path(cur["repo"]) if cur.get("repo") else default_repo(f, [e for e, _ in items], default_parent)
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
            "others": others[:20], "projects": rows, "github": github.GITHUB.status()}


# ---------------------------------------------------------------- apply

def apply(f: dict, plan: dict, body: dict, report: list[str]) -> None:
    """Do what plan() said, with the projects locked (hub.apply_sync); f["sync"] is set
    to the new choice once it is done. Each step taken is said in `report`. A step that
    fails raises, and the report holds the steps taken before it (going back to separate
    repositories takes them all back: see _apply_separate)."""
    (_apply_shared if plan["plan_mode"] == "shared" else _apply_separate)(f, plan, body, report)


def _publish(root: Path, create: bool, owner: str, name: str, report: list[str]) -> str | None:
    """Push to origin, or, with `create`, make a private GitHub repository for `root` first."""
    sync = project_sync(root)
    if run_git(root, "remote").stdout.strip():
        if not sync.validate_target():
            report.append("Local files kept; sync paused: " + sync.blocked_reason)
            return None
        url = github_of(root)
        pushed = sync.push(manual=True)
        if pushed.state != "synced":
            report.append("Not pushed: " + str(pushed.message))
        return url
    if not create:
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
    ensure_ignore(top)
    marker = top / gitsync.SHARED_MARKER
    if not gitsync.shared_marker(top):
        marker.write_bytes((json.dumps({"shared": True, "folder": f["name"]}, ensure_ascii=False,
                                       indent=2) + "\n").encode("utf-8"))
    for r in plan["projects"]:
        if r["action"] not in ("moves", "stays"):
            continue
        root = Path(r["path"])
        if r["action"] == "moves":
            dst = Path(r["target"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            registry.move_folder(root, dst, across=True)
            report.append(f"Moved {root.name} to {dst}")
            root = dst
        dot = root / ".git"
        if dot.is_file():
            raise ValueError(f"{root.name} is a git worktree or submodule; set it up by hand")
        if dot.is_dir() and r["kind"] == "own":
            # Its history is kept as refs/prism/imported/<id> until the shared commit takes
            # it in: a run that failed after this is finished by the next (_imported).
            if gitenv.git(root, "rev-parse", "-q", "--verify", "HEAD^{commit}", timeout=20).returncode == 0:
                run_git(top, "fetch", "-q", "--no-tags", str(root), f"+HEAD:refs/prism/imported/{r['id']}",
                        timeout=300)
            if (root / SPLIT_BACKUP).exists():
                raise ValueError(f"{root / SPLIT_BACKUP} is in the way")
            os.replace(dot, root / SPLIT_BACKUP)
            report.append(f"{root.name}: its history is kept in the shared repository; "
                          f"its own .git is now {SPLIT_BACKUP}")
    repo = Repository.find(top)
    with repo.changing():
        repo.ok("add", "-A", timeout=300)
        big = unstage_big(repo)
        if big:
            report.append("Not committed, too large for GitHub: " + ", ".join(big))
        tree = repo.ok("write-tree").strip()
        head = repo.git("rev-parse", "-q", "--verify", "HEAD^{commit}").stdout.strip()
        old_tree = repo.ok("rev-parse", "HEAD^{tree}").strip() if head else None
        imports = _imported(repo, head)
        if imports:
            report.append(f"The histories of {len(imports)} project{'s' if len(imports) != 1 else ''} "
                          "come along in the shared commit")
        if imports or tree != old_tree:
            parents = [x for h in ([head] if head else []) + imports for x in ("-p", h)]
            n = len(plan["projects"])
            msg = f"{f['name']}: {n} project{'s' if n != 1 else ''} in one repository"
            sha = repo.ok("commit-tree", tree, *parents, "-m", msg).strip()
            repo.ok("update-ref", "HEAD", sha)
            report.append(f"Committed “{msg}”")
    _publish(top, bool(body.get("github")), str(body.get("github_owner") or ""),
             str(body.get("github_name") or "") or repo_name(top.name), report)
    f["sync"] = {"mode": "shared", "repo": str(top)}


def _imported(repo: Repository, head: str) -> list[str]:
    """The projects' histories fetched into the shared repository (refs/prism/imported/)
    that its branch does not have yet: this run's, and those of a run that failed before
    its commit, whose projects' .git are set aside already (a retry sees them as in the
    shared repository, and fetches nothing)."""
    refs = repo.ok("for-each-ref", "--format=%(objectname)", "refs/prism/imported/").split()
    return [sha for sha in dict.fromkeys(refs)
            if not head or repo.git("merge-base", "--is-ancestor", sha, head).returncode != 0]


def _init_own(root: Path) -> None:
    """A repository of the project's own, which leaves a set-aside .git out of it."""
    git_init(root)
    exclude = root / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    have = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".git-prism-*/" not in have:
        lines = [*have.splitlines(), ".git-prism-*/"]
        write_bytes(exclude, ("\n".join(lines) + "\n").encode("utf-8"))


def _remove(p: Path) -> bool:
    """A file or folder this operation made, taken back (git's read-only objects too).
    False when it is still there (a program holds a file in it)."""
    def writable(fn, path, _):
        os.chmod(path, 0o700)
        fn(path)
    try:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, onerror=writable)
        elif p.exists():
            p.unlink()
    except OSError:
        pass
    return not (p.exists() or p.is_symlink())


def _apply_separate(f: dict, plan: dict, body: dict, report: list[str]) -> None:
    rows = [r for r in plan["projects"] if r["action"] in ("keeps", "splits", "new")]
    tops: dict[str, list[dict]] = {}
    for r in rows:
        if r["action"] == "splits":
            tops.setdefault(r["repo"], []).append(r)
    data = registry.load_projects()
    # Every shared repository is looked at before any is changed.
    for top_s, members in tops.items():
        top, ids = Path(top_s), {r["id"] for r in members}
        strangers = [e.get("name") or Path(e["path"]).name for e in data["projects"]
                     if inside(Path(e["path"]), top) and registry.project_key(Path(e["path"])) not in ids]
        if strangers:
            raise ValueError(f"the repository {top} also holds {', '.join(strangers[:5])}, "
                             "which are not in this folder; move them in first")
        if (top / SHARED_BACKUP).exists():
            raise ValueError(f"{top / SHARED_BACKUP} is in the way")
    for top_s, members in tops.items():
        top = Path(top_s)
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
    # Every project gets its repository and its history (from its shared one, still in
    # place) before that is set aside: a step failing for one of them takes back what was
    # made for all, and every shared repository already set aside is put back, so the
    # folder stays shared and whole. (Setting it aside first left the projects after the
    # failing one with no repository, and their history only in the set-aside .git, which
    # a retry did not use.)
    made: list[Path] = []
    set_aside: list[Path] = []
    member = None
    try:
        for top_s, members in tops.items():
            top = Path(top_s)
            for r in members:
                root = member = Path(r["path"])
                made += [p for p in (root / ".git", root / ".gitignore", root / ".gitattributes") if not p.exists()]
                _init_own(root)
                if r.get("split"):
                    run_git(root, "fetch", "-q", "--no-tags", str(top), r["split"], timeout=300)
                    run_git(root, "reset", "-q", "FETCH_HEAD")
            member = None
            os.replace(top / ".git", top / SHARED_BACKUP)
            set_aside.append(top)
    except BaseException:
        if member is not None:
            report.append(f"{member.name}: its own repository could not be made")
        left = []
        for top in reversed(set_aside):
            try:
                os.replace(top / SHARED_BACKUP, top / ".git")
            except OSError:
                left.append(top / SHARED_BACKUP)
        left += [p for p in reversed(made) if not _remove(p)]
        report.append("Nothing was changed: every project stays in its shared repository." if not left else
                      "Could not take back: " + ", ".join(map(str, left)) + ". Close the programs using "
                      "them, then delete them (a set-aside .git: rename it back to .git) and try again.")
        raise
    for top_s, members in tops.items():
        top = Path(top_s)
        try:
            (top / gitsync.SHARED_MARKER).unlink()
        except OSError:
            pass
        report.append(f"The shared repository in {top} is set aside as {SHARED_BACKUP}")
        for r in members:
            report.append(f"{Path(r['path']).name}: a repository of its own"
                          + (", with its history" if r.get("split") else ""))
    for r in rows:
        if r["action"] == "new":
            _init_own(Path(r["path"]))
            report.append(f"{Path(r['path']).name}: new repository")
    owner = str(body.get("github_owner") or "")
    for r in rows:
        root = Path(r["path"])
        if body.get("github") or github_of(root):
            _publish(root, bool(body.get("github")), owner, repo_name(root.name), report)
        tell_editor(root)
    f["sync"] = {"mode": "separate"}
