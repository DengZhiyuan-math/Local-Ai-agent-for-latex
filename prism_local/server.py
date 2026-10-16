#!/usr/bin/env python3
"""prism-local: a local web studio for LaTeX projects.

Editor + PDF preview with SyncTeX + an AI agent panel, served from
127.0.0.1 with the Python standard library only.

    python3 prism_local/server.py [PROJECT_DIR] [--port 8765] [--no-browser]
                                  [--exit-when-idle] [--port-tries N] [--ready-file F]

Besides the builds (build.py) it runs only read-only `git status` / `git diff`
and, for the agent panel, the chosen AI backend (agent.py, backends.py): the
Claude Code or Codex CLI, or calls to an OpenAI-compatible API such as DeepSeek.
Optional per-project settings live in PROJECT_DIR/prism.json (see README).
"""
from __future__ import annotations

import gitenv

import argparse
import hashlib
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent import AgentManager  # noqa: E402
import gitsync  # noqa: E402
from gitsync import GitSync  # noqa: E402
from project import UPLOAD_DIR, UPLOAD_WAIT, Project, mtime  # noqa: E402
from repository import FilesLock, RepoBusy, locate, refs_stamp  # noqa: E402
from fsutil import EDITABLE_SUFFIXES  # noqa: E402
import desktop  # noqa: E402
from errors import Conflict, NotFound, Refused  # noqa: E402
import synctex  # noqa: E402
from presence import Presence  # noqa: E402
import httpbase  # noqa: E402
import instances  # noqa: E402
import publish  # noqa: E402
import registry  # noqa: E402
import selfupdate  # noqa: E402
import settings  # noqa: E402
import sources  # noqa: E402

# The project's files are written under it: saves, uploads, an Undo, and GITSYNC's commits
# and merges; also by the other editors of a shared repository (repository.FilesLock).
SAVE_LOCK = FilesLock(lambda: GITSYNC.git_dir if GITSYNC.check_repo() else None)


# The project this editor works on (project.py): its folder, its settings with the
# document shown, and the files the editor shows.
PROJECT: Project = None  # type: ignore[assignment]


def set_root(root: Path) -> None:
    global PROJECT
    PROJECT = Project(root, files_lock=SAVE_LOCK, turn=AGENT, on_change=lambda: GITSYNC.touched())


def git(*args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    """Read-only git in the project (gitenv.git)."""
    return gitenv.git(PROJECT.root, *args, timeout=timeout)


_GIT_PLACE: dict = {}


def git_place():
    """Where the project is in git (repository.locate), looked up once."""
    if PROJECT.root not in _GIT_PLACE:
        _GIT_PLACE[PROJECT.root] = locate(PROJECT.root)
    return _GIT_PLACE[PROJECT.root]


def git_prefix() -> str:
    """Where the project sits in its repository: "" at the top, "examples/minimal/" …"""
    return git_place().prefix


_STATUS: list = [(None, {})]         # (key, status), replaced as one (requests run in threads)


def git_status(listed=None) -> dict[str, str]:
    """{project-relative path: status} of the files git reports as changed. With `listed`
    (the files the tree shows, with their mtimes: what a change to them changes), the last
    answer is given again while neither they nor the repository changed (refs_stamp): the
    tree is asked for every two seconds by every open page, and nothing has changed most
    of those times."""
    place = git_place()
    stamp = refs_stamp(place.repo.git_dir) if listed is not None and place.repo else None
    key = None if stamp is None else (str(place.repo.git_dir), stamp, tuple(listed))
    last_key, last = _STATUS[0]
    if key is not None and key == last_key:
        return last
    status = _git_status()
    if key is not None:
        _STATUS[0] = (key, status)
    return status


def _git_status() -> dict[str, str]:
    try:
        # --no-optional-locks: a look, which never holds the index from a commit meanwhile.
        out = git("--no-optional-locks", "status", "--porcelain", "-z", "-uall", "--", ".").stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    prefix = git_prefix()
    return {path[len(prefix):]: xy.strip() or "M" for xy, path in sources.parse_status(out)
            if path.startswith(prefix)}


def project_diff(spec: str) -> dict:
    """What changed since the last commit (staged or not, and new files), and when nothing
    did, what the last commit changed: with sync on, edits are committed a moment after you
    stop typing, so "nothing uncommitted" is the usual state, not an empty one.
    --relative: paths as the project sees them, and nothing from outside it."""
    if git("rev-parse", "--git-dir", timeout=10).returncode != 0:
        return {"repo": False, "diff": ""}
    has_head = git("rev-parse", "-q", "--verify", "HEAD", timeout=10).returncode == 0
    diff = git("diff", "--no-color", "--relative", *(["HEAD"] if has_head else []), "--", spec, timeout=20).stdout
    new = [x for x in git("ls-files", "--others", "--exclude-standard", "--", spec, timeout=20).stdout.splitlines() if x]
    out = {"repo": True, "diff": diff, "untracked": new[:50], "last": None}
    if not diff and has_head:
        log = git("log", "-1", "--format=%h%x1f%ct%x1f%s", "--", spec, timeout=20).stdout.strip().split("\x1f")
        if len(log) == 3:
            shown = git("show", "--no-color", "--relative", "--format=", log[0], "--", spec, timeout=20).stdout
            out["last"] = {"hash": log[0], "at": int(log[1]), "message": log[2], "diff": shown}
    return out


# Every upload is committed in the project's own repository, alone (other changes stay as
# they are), and pushed, through the same machinery as the other commits (gitsync.py).
UPLOAD_SYNC: dict[str, dict] = {}     # rel -> {"state": syncing|synced|local|failed, "message"}


def sync_upload(rel: str, upload_id: str = "") -> None:
    """Commit the upload `rel` and push it (in a thread; the state is in UPLOAD_SYNC)."""
    if upload_id and ATTACHMENTS.taken_back(upload_id):
        UPLOAD_SYNC.pop(rel, None)  # removed from the message before its commit: none is made
        return
    try:
        o = GITSYNC.record_file(rel, f"Add {rel} (a file used in the agent chat)")
        UPLOAD_SYNC[rel] = {"state": o.state, "message": o.message}
    except RepoBusy:
        GITSYNC.touched()           # the next autosave commits it
        UPLOAD_SYNC[rel] = {"state": "local", "message": "Saved here; another prism-local window is "
                            "using the repository, so it is committed with the next autosave"}
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as e:
        if upload_id and ATTACHMENTS.taken_back(upload_id):
            UPLOAD_SYNC.pop(rel, None)      # deleted while git was adding it: nothing to say
            return
        UPLOAD_SYNC[rel] = {"state": "failed", "message": f"git: {str(e)[-300:]}"}


class Attachments:
    """The chat's uploads, each under the id its page gave it, so a chip removed from the
    message box takes its file back (/api/upload/cancel). A file is deleted only when this
    upload wrote it, it is unchanged since, and no message has sent it: a file sent with a
    message, or one that was there before (an identical upload reuses it), always stays."""

    KEEP = 1000                     # ids taken back before their upload came, remembered

    def __init__(self):
        self.lock = threading.Lock()
        self.uploads: dict[str, dict] = {}      # id -> {"path", "digest", "wrote"}
        self.early: dict[str, None] = {}        # ids taken back before their upload arrived
        self.gone: set[str] = set()             # ids taken back
        self.sent: set[str] = set()             # paths a message has sent

    def taken_back(self, upload_id: str) -> bool:
        with self.lock:
            return upload_id in self.gone

    def saved(self, upload_id: str, rel: str, data: bytes, wrote: bool) -> None:
        with self.lock:
            self.uploads[upload_id] = {"path": rel, "digest": hashlib.sha256(data).digest(), "wrote": wrote}

    def send(self, paths: list) -> str | None:
        """A message sends `paths`: from now on they stay. An error if one was taken back."""
        paths = [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []
        with self.lock:
            gone = {self.uploads[i]["path"] for i in self.gone if i in self.uploads}
            bad = next((p for p in paths if p in gone and p not in self.sent), None)
            if bad is not None:
                return f"{bad} was removed from the message; add it again"
            self.sent.update(paths)
            return None

    def cancel(self, upload_id: str) -> tuple[dict, int]:
        """Take the upload back: delete its file when that is safe. Under the files lock, as
        an upload is written: one arriving meanwhile is then either here or refused."""
        if not SAVE_LOCK.acquire(timeout=UPLOAD_WAIT):
            raise Conflict("git is busy with the files; try again in a moment")
        try:
            with self.lock:
                up = self.uploads.get(upload_id)
                if up is None:          # not arrived yet (or never will): it is not written
                    self.gone.add(upload_id)
                    self.early[upload_id] = None
                    while len(self.early) > self.KEEP:
                        self.gone.discard(self.early.pop(next(iter(self.early))))
                    return {"path": None, "deleted": False}, 200
                rel = up["path"]
                if rel in self.sent:
                    return {"error": "this file was sent with a message, so it stays", "path": rel}, 409
                self.gone.add(upload_id)
                claimed = any(u["path"] == rel for i, u in self.uploads.items() if i not in self.gone)
                if not up["wrote"] or claimed:
                    return {"path": rel, "deleted": False}, 200
            p = PROJECT.root / rel
            try:
                if hashlib.sha256(p.read_bytes()).digest() != up["digest"]:
                    return {"path": rel, "deleted": False}, 200      # changed since: yours now
                p.unlink()
            except FileNotFoundError:
                return {"path": rel, "deleted": False}, 200
        finally:
            SAVE_LOCK.release()
        UPLOAD_SYNC.pop(rel, None)
        GITSYNC.touched()               # if it was committed, the next autosave records the deletion
        return {"path": rel, "deleted": True}, 200


ATTACHMENTS = Attachments()

# A server keeps running the code it started with. When prism-local is updated, the
# editor offers to restart it (/api/restart): the new server starts with the same
# command line, on the same port, and the page reloads.
STARTED_CODE = httpbase.code_stamp()
STARTED_AT = time.time()                 # tells the page when a new server answers
RESTART: dict = {"server": None, "again": False, "move_to": None, "args": None, "port": None}
LIFETIME: instances.Serving | None = None
# A folder rename that did not work (in use?): the server came back at the old folder.
MOVE: dict = {"error": None, "from": None}


def rename_project(name: str) -> tuple[dict, int]:
    """Rename the project: the name in the list, and its folder on disk (a name with
    characters no folder may have keeps them in the list only). The folder cannot be
    renamed while this server works in it, so the server stops, and starts again at the
    new folder on the same port (relaunch, --moved-from); the page waits, then reloads."""
    _NAME["mtime"] = None
    folder = registry.folder_name(name)
    if not name or not folder or folder == PROJECT.root.name:
        return {"name": registry.rename(PROJECT.root, name), "moving": False}, 200
    new = PROJECT.root.parent / folder
    if new.exists() and registry.norm(new) != registry.norm(PROJECT.root):
        return {"error": f"a folder named {folder} already exists next to this project"}, 409
    if PROJECT.busy() or RESTART["server"] is None:
        return {"error": "wait until the build or the agent's turn has finished"}, 409
    if LIFETIME is None:
        return {"error": "not ready to restart"}, 409
    try:
        transition = LIFETIME.transition(new)
    except (instances.Busy, TimeoutError, OSError) as exc:
        return {"error": str(exc)}, 409
    try:
        registry.rename(PROJECT.root, name)      # the list keeps the exact name; move_folder tidies it
    except BaseException:
        transition.close()
        raise
    RESTART.update(again=True, move_to=new)
    threading.Thread(target=RESTART["server"].shutdown, daemon=True).start()
    return {"name": name, "moving": True, "folder": folder}, 200


def move_argv(new: Path, moving: bool = True) -> list[str]:
    """This server's command line for the project at `new`, on the port it has now."""
    a = RESTART["args"]
    argv = [sys.executable, *(["-u"] if "-u" in (getattr(sys, "orig_argv", None) or []) else []),
            str(Path(__file__).resolve()), str(new), "--port", str(RESTART["port"]),
            "--port-tries", "1", "--no-browser"]
    if moving:
        argv += ["--moved-from", str(PROJECT.root)]
    if a.exit_when_idle:
        argv.append("--exit-when-idle")
    if a.idle_timings:
        argv += ["--idle-timings", a.idle_timings]
    if a.ready_file:
        alias = LIFETIME.alias if LIFETIME else None
        if alias and moving and registry.inside(alias, PROJECT.root):
            alias = new / alias.relative_to(PROJECT.root)
        argv += ["--ready-file", str(alias or instances.path(new))]
    return argv


# ---------------------------------------------------------------- synctex

SYNC = synctex.SyncIndex()           # the shown document's, read once until it changes
AGENT = AgentManager(lambda: PROJECT.root, lambda: PROJECT.list_files(), lambda rel: PROJECT.resolve(rel),
                     output_fn=lambda: PROJECT.config.outdir)
# The project's changes, recorded in its repository and synced with GitHub (gitsync.py).
GITSYNC = GitSync(lambda: PROJECT.root, lambda: PROJECT.config.outdir, busy=lambda: AGENT.busy(), writes=SAVE_LOCK)
AGENT.on_start = GITSYNC.before_turn
AGENT.on_changed = lambda paths, why: GITSYNC.after_turn(paths, why)
AGENT.writes = SAVE_LOCK


# The project's name as the Home page lists it (Rename): read again only when the list changed.
_NAME: dict = {"mtime": None, "name": None}


def project_name() -> str:
    try:
        m = (registry.state_dir() / "projects.json").stat().st_mtime_ns
    except OSError:
        m = None
    if m != _NAME["mtime"] or _NAME["name"] is None:
        try:
            _NAME["name"] = registry.display_name(PROJECT.root)
        except (OSError, ValueError):
            _NAME["name"] = PROJECT.root.name
        _NAME["mtime"] = m
    return _NAME["name"]


def github_url() -> str | None:
    return GITSYNC.status().get("github")
PRESENCE = Presence()


# ---------------------------------------------------------------- http

class Handler(httpbase.Handler):
    """The editor's requests. The checks every request passes are in httpbase.py."""

    presence = PRESENCE
    pages = {"/": "index.html", "/index.html": "index.html", "/viewer": "viewer.html"}

    def get(self, path, q):
        return self._get(path, q)      # errors: httpbase._answer

    def _get(self, path, q):
        if self._foreign_project(q):
            return
        if path == "/api/ping":
            return self._json({"app": "prism-local", "root": str(PROJECT.root), "project_key": registry.project_key(PROJECT.root), "pid": os.getpid(),
                               "pages": PRESENCE.count(), "token": LIFETIME.token if LIFETIME else None})
        if path == "/api/pdfstat":
            cfg = PROJECT.config
            # project_key: the pop-out PDF polls only this, and so learns whose PDF it shows.
            return self._json({"main": cfg.main, "mtime": PROJECT.pdf_state(cfg),
                               "project_key": registry.project_key(PROJECT.root)})
        if path == "/pdf":
            cfg = PROJECT.config
            if q.get("main") and q["main"] != cfg.main:
                return self._err(409, "the compiled document changed")
            if PROJECT.writing(cfg.pdf):
                return self._err(503, "the PDF is being built; it is served when the build ends")
            if not cfg.pdf.exists():
                return self._err(404, "no PDF yet")
            return self._send(200, cfg.pdf.read_bytes(), "application/pdf")
        if path == "/api/tree":
            walked: list[str] = []
            files, (listed, editable) = [], PROJECT.list_both(walked)
            folders = [f for f in walked if not PROJECT.excluded(f + "/")]
            for f in listed:
                try:
                    files.append({"path": f, "mtime": mtime(PROJECT.root / f),
                                  "editable": Path(f).suffix in EDITABLE_SUFFIXES})
                except FileNotFoundError:       # deleted since it was listed (a build, git)
                    pass
            st = git_status([(f["path"], f["mtime"]) for f in files])
            for f in files:
                f["git"] = st.get(f["path"], "")
            return self._json({"root": PROJECT.root.name, "name": project_name(), "path": str(PROJECT.root),
                               "project_key": registry.project_key(PROJECT.root),
                               "move_error": MOVE["error"], "files": files, "folders": folders,
                               "order": PROJECT.document_order(editable),
                               "pdf_main": (cfg := PROJECT.config).main, "pdf_mtime": PROJECT.pdf_state(cfg),
                               "updated": httpbase.code_stamp() > STARTED_CODE + 1, "server": STARTED_AT,
                               "sync": GITSYNC.status()})
        if path == "/api/git/publish":
            return self._json(publish.publish_info(PROJECT.root))
        if path == "/api/git/theirs":            # GitHub's version, after a clash
            PROJECT.resolve(q["path"])
            return self._json(GITSYNC.theirs(q["path"]))
        if path == "/api/git/github":
            return self._json({"github": github_url()})
        if path == "/api/git/log":
            if not GITSYNC.check_repo():
                return self._json({"commits": [], "error": "This project has no git repository to sync with."})
            return self._json({"commits": GITSYNC.log(q["path"])})
        if path == "/api/git/show":
            return self._json(GITSYNC.show(q["rev"], q["path"]))
        if path == "/api/config":
            cfg = PROJECT.config
            return self._json({"main": cfg.project_main, "outdir": cfg.outdir, "modes": cfg.modes,
                               "builder": cfg.builder, "engine": cfg.engine, "error": cfg.error,
                               "build": {m: cfg.describe(m) for m in cfg.modes}})
        if path == "/api/agent/events":
            if q.get("project_key") != registry.project_key(PROJECT.root):
                return self._err(409, "This page belongs to another project. Reopen the project's editor.")
            # A page waits up to 20 s for new events; a hidden one asks without waiting (it
            # would hold one of the few connections a browser keeps to this server).
            wait = min(20.0, max(0.0, float(q.get("wait", 20))))
            got = AGENT.events(int(q["job"]), int(q.get("after", 0)), wait)
            if got is None:
                return self._err(404, "unknown job")
            return self._json({"events": got[0], "done": got[1]})
        if path == "/api/agent/commands":
            r = AGENT.commands(q.get("provider") or None, refresh=q.get("refresh") == "1")
            return self._json(r, 502 if "error" in r else 200)
        if path == "/api/agent/account":
            return self._json(AGENT.account(q.get("provider") or None, PROJECT.root, fresh=q.get("fresh") == "1"))
        if path == "/api/agent/info":
            return self._json(AGENT.info())
        if path == "/api/symbols":
            return self._json(PROJECT.symbols())
        if path == "/api/snippets":
            # Your snippets file and how they expand, both set in Home → Settings.
            st = settings.load()
            return self._json({**settings.snippets(), "on": st["snippets"],
                               "fraction": st["snippets_fraction"], "matrix": st["snippets_matrix"]})
        if path == "/api/file":
            p = PROJECT.resolve(q.get("path", ""))
            return self._json({"path": q["path"], "content": p.read_text(encoding="utf-8"),
                               "mtime": mtime(p)})
        if path == "/api/asset":
            from urllib.parse import quote
            p = PROJECT.resolve(q.get("path", ""), editable_only=False)
            ctype = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(p.suffix.lower())
            extra = {} if ctype else {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(p.name)}
            return self._send(200, p.read_bytes(), ctype or "application/octet-stream", extra)
        if path == "/api/diff":
            if q.get("path"):
                PROJECT.resolve(q["path"])
            return self._json(project_diff(q.get("path") or "."))
        if path == "/api/upload/sync":             # how the uploads' commits and pushes went
            return self._json({p: UPLOAD_SYNC.get(p) for p in q.get("paths", "").split("\n") if p})
        if path == "/api/synctex/forward":
            try:
                records = SYNC.lookup(PROJECT.config.synctex, PROJECT.root)
            except synctex.NoData as e:
                return self._err(404, str(e))
            r = records.forward(q["file"], int(q["line"]))
            return self._json(r or {"error": "no match"}, 200 if r else 404)
        if path == "/api/synctex/inverse":
            try:
                records = SYNC.lookup(PROJECT.config.synctex, PROJECT.root)
            except synctex.NoData as e:
                return self._err(404, str(e))
            r = records.inverse(int(q["page"]), float(q["x"]), float(q["y"]), PROJECT.config.outdir,
                                lambda: [f for f in PROJECT.list_files() if f.endswith(".bib")])
            return self._json(r or {"error": "no match"}, 200 if r else 404)
        return self._err(404, "not found")

    def _files(self, path, body) -> dict:
        """The Files sidebar's menu (see item_path)."""
        rels = body.get("paths")
        if rels is not None and (not isinstance(rels, list) or not all(isinstance(r, str) for r in rels)):
            raise ValueError("paths must be a list of project paths")
        if path == "/api/files/reveal":
            rel = str(body.get("path") or "")
            p = PROJECT.item(rel)
            if p == PROJECT.root:
                desktop.open_folder(p)
            else:
                desktop.reveal(p)
            return {"ok": True}
        if path == "/api/files/copy":
            try:
                desktop.copy_files([PROJECT.item(r) for r in rels or []])
            except desktop.Unsupported as e:   # the page keeps them: paste works in this project
                return {"system": False, "message": str(e)}
            return {"system": True}
        if path == "/api/files/paste":
            # The page's own copy where the system has no file clipboard, else the clipboard.
            sources = [PROJECT.item(r) for r in rels] if rels else desktop.clipboard_files()
            if not sources:
                raise ValueError("the clipboard holds no files; copy some in the editor, Explorer or Finder")
            return PROJECT.paste(str(body.get("folder") or ""), sources)
        if path == "/api/files/rename":
            return PROJECT.rename(str(body["path"]), str(body.get("name") or ""))
        if path == "/api/files/new":
            return PROJECT.new(str(body.get("folder") or ""), str(body.get("name") or ""),
                            str(body.get("kind") or "file"))
        if path == "/api/files/trash":
            return PROJECT.trash(rels or [])
        raise NotFound("unknown file action")

    def post(self, path, body):
        return self._post(path, body)      # errors: httpbase._answer

    def _foreign_project(self, q=None):
        """A page of another project (the port was reused) is refused. It names its project
        in a header, or in the query where it cannot add one (a tab it opens, the pop-out PDF)."""
        key = self.headers.get("X-Prism-Project") or (q.get("project_key") if q is not None else None)
        if key and key != registry.project_key(PROJECT.root):
            self._json({"error": "This port now belongs to another project. Reopen it from Home.",
                        "other_project": True}, 409)
            return True
        return False

    def _post(self, path, body):
        if self._foreign_project():
            return
        if path.startswith(("/api/agent", "/api/files/")) and body.get("project_key") != registry.project_key(PROJECT.root):
            return self._err(409, "This page belongs to another project. Reopen the project's editor.")
        if path.startswith("/api/files/"):
            try:
                return self._json(self._files(path, body))
            except desktop.Unsupported as e:
                raise Refused(str(e)) from e
        if path == "/api/git/publish":
            if PROJECT.busy():
                return self._err(409, "wait until the build or the agent's turn has finished")
            with GITSYNC.lock:
                r = publish.publish_project(PROJECT.root, str(body.get("name") or ""), str(body.get("owner") or ""),
                                        list(body.get("leave_out") or []), files_lock=SAVE_LOCK)
            GITSYNC.recheck()                   # a repository exists now
            return self._json({**r, "sync": GITSYNC.status()}, 502 if "error" in r else 200)
        if path == "/api/git/recheck":
            GITSYNC.recheck()
            return self._json(GITSYNC.status())
        if path == "/api/project/rename":
            return self._json(*rename_project(str(body["name"] or "").strip()))
        if path == "/api/file":
            # A save calls GITSYNC.touched: an autosave commit follows once you stop editing.
            return self._json(*PROJECT.save(body["path"], str(body["content"]), body.get("base_mtime"),
                                            bool(body.get("force"))))
        if path == "/api/git/sync":
            action = str(body.get("action") or "")
            if action == "bind":
                if not isinstance(body.get("target"), dict):
                    return self._err(400, "Open the Git menu and confirm the displayed target")
                GITSYNC.bind_target(body["target"])
            elif action in ("on", "off"):
                GITSYNC.set_enabled(action == "on")
            elif action in ("commit", "pull", "resolve"):
                if action == "resolve" and PROJECT.busy():
                    return self._err(409, "wait until the build or the agent's turn has finished")
                GITSYNC.now(action)
            else:
                return self._err(400, "unknown action")
            return self._json({**GITSYNC.status(), "github": github_url()})
        if path == "/api/agent":
            scope = body.get("scope") or None
            if scope is not None:
                if not isinstance(scope, list) or not all(isinstance(f, str) for f in scope):
                    raise ValueError("scope must be a list of files")
                for f in scope:
                    PROJECT.resolve(f)          # an editable project file, or ValueError
            refused = ATTACHMENTS.send(body.get("attachments") or [])
            if refused:
                return self._err(409, refused)
            r = AGENT.start(body["prompt"], body.get("session_id") or None,
                            body.get("mode", "ask"), body.get("model") or None,
                            body.get("effort") or None, scope, body.get("provider") or None,
                            body.get("attachments") or [])
            return self._json(r, 409 if "error" in r else 200)
        if path == "/api/home":
            r = instances.ensure(None)
            return self._json(r, 502 if "error" in r else 200)
        if path == "/api/agent/usage":
            return self._json(AGENT.probe_rate(body.get("provider") or None))
        if path == "/api/agent/stop":
            return self._json(AGENT.stop(int(body["job"])))
        if path == "/api/agent/undo":
            return self._json(AGENT.undo(int(body["turn"])))
        if path == "/api/build":
            if body.get("by") == "agent" and not AGENT.editing():
                return self._err(403, "Agent compilation is available only during an Edit turn")
            r = PROJECT.build(str(body.get("mode", "draft")), bool(body.get("clean")), body.get("active"))
            if body.get("by") == "agent" and not r.get("busy"):
                AGENT.built(r)          # the editor shows the agent's build like its own
            return self._json(r, 409 if r.get("busy") else 200)
        if path == "/api/upload":
            import base64
            destination = body.get("destination", "attachment")
            if destination not in ("attachment", "project"):
                return self._err(400, "unknown upload destination")
            if destination == "project" and AGENT.busy():
                return self._err(409, "wait until the agent's turn has finished")
            try:
                data = base64.b64decode(str(body.get("data") or ""), validate=True)
            except ValueError:
                return self._err(400, "the file data is not base64")
            if destination == "project":
                rel = PROJECT.upload(str(body.get("name") or "file"), data, folder="")
                GITSYNC.touched()
                return self._json({"path": rel, "size": len(data)})
            upload_id = str(body.get("upload_id") or "")
            if not SAVE_LOCK.acquire(timeout=UPLOAD_WAIT):
                raise Conflict("git is busy with the files; try the upload again in a moment")
            try:                        # one taken back meanwhile waits for this (Attachments.cancel)
                if upload_id and ATTACHMENTS.taken_back(upload_id):
                    return self._err(409, "the file was removed from the message")
                # What was there before (in any case: Windows' names ignore it) is never "written".
                folder = PROJECT.root / UPLOAD_DIR
                before = {p.name.casefold() for p in folder.iterdir()} if folder.is_dir() else set()
                rel = PROJECT.upload(str(body.get("name") or "file"), data, folder=UPLOAD_DIR)
                if upload_id:
                    wrote = rel.rsplit("/", 1)[-1].casefold() not in before
                    ATTACHMENTS.saved(upload_id, rel, data, wrote)
            finally:
                SAVE_LOCK.release()
            UPLOAD_SYNC[rel] = {"state": "syncing", "message": "Committing and pushing…"}
            threading.Thread(target=sync_upload, args=(rel, upload_id), daemon=True).start()
            return self._json({"path": rel, "size": len(data), "sync": UPLOAD_SYNC[rel]})
        if path == "/api/upload/cancel":
            if not body.get("upload_id"):
                return self._err(400, "missing upload_id")
            return self._json(*ATTACHMENTS.cancel(str(body["upload_id"])))
        if path == "/api/restart":
            if PROJECT.busy() or RESTART["server"] is None:
                return self._err(409, "wait until the build or the agent's turn has finished")
            if LIFETIME is None:
                return self._err(409, "not ready to restart")
            try:
                LIFETIME.transition()
            except (instances.Busy, TimeoutError, OSError) as exc:
                return self._err(409, str(exc))
            RESTART["again"] = True
            threading.Thread(target=RESTART["server"].shutdown, daemon=True).start()
            return self._json({"ok": True})
        if path == "/api/build/stop":
            return self._json({"ok": PROJECT.stop_build()})
        return self._err(404, "not found")


def is_scratch(root: Path) -> bool:
    """A project in the system's temp folder (a test, a throwaway copy)."""
    import tempfile
    tmp = Path(tempfile.gettempdir()).resolve()
    return root == tmp or tmp in root.parents


def finish_move(old: Path, new: Path) -> Path:
    """After rename_project: rename the folder (the old server has let go of it), commit the
    move if the folder is in a shared repository, and return where the project now is.
    If the folder cannot be renamed, the project stays where it was, and the page says why."""
    os.chdir(old.parent)
    try:
        registry.move_folder(old, new)
    except registry.MoveIncomplete as e:
        MOVE.update(error=str(e), **{"from": str(old)})
        return e.actual
    except (OSError, ValueError, TimeoutError) as e:
        MOVE.update(error=f"The folder was not renamed: {e}", **{"from": str(old)})
        return old
    try:
        gitsync.record_move(old, new)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        MOVE["error"] = (f"The folder was renamed, but the rename could not be committed ({e}). "
                         "Commit it from a terminal, so the other copies of the repository get it.")
    MOVE["from"] = str(old)
    return new


def main():
    httpbase.quiet_stdio()
    ap = argparse.ArgumentParser(description="prism-local: local LaTeX studio "
                                 "(editor, PDF + SyncTeX, AI agent panel)")
    ap.add_argument("project", nargs="?", type=Path, default=Path.cwd(),
                    help="LaTeX project directory (default: current directory)")
    ap.add_argument("--port", type=int, default=8765, help="port (0: any free port)")
    ap.add_argument("--port-tries", type=int, default=1,
                    help="if the port is taken, try this many ports upwards")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--exit-when-idle", action="store_true",
                    help="exit shortly after the last editor or PDF page is closed")
    ap.add_argument("--ready-file", type=Path,
                    help="once listening, write {pid, port, url, root} as JSON to this file")
    ap.add_argument("--idle-timings", help=argparse.SUPPRESS)   # "first,grace,stale" for tests
    ap.add_argument("--root", type=Path, help=argparse.SUPPRESS)   # backwards compatible
    ap.add_argument("--moved-from", type=Path, help=argparse.SUPPRESS)   # see rename_project
    ap.add_argument("--instance-token", help=argparse.SUPPRESS)
    a = ap.parse_args()
    project = Path(a.root or a.project).resolve()
    global LIFETIME
    try:
        with instances.serving(project, a.ready_file, a.instance_token) as life:
            LIFETIME = life
            if a.moved_from:
                actual = finish_move(a.moved_from.resolve(), project)
                if actual != project:
                    life.retarget(actual)
                project = actual
            return serve(a, project, life)
    except (instances.Busy, TimeoutError) as exc:
        sys.exit(f"prism-local: {exc}")


def serve(a, project, life):
    set_root(project)
    PROJECT.show_last_built()
    if a.idle_timings:
        f, g, st = (float(x) for x in a.idle_timings.split(","))
        PRESENCE.first_wait, PRESENCE.grace, PRESENCE.stale = f, g, st
    if not (PROJECT.root / PROJECT.config.main).is_file():
        print(f"prism-local: warning: main file {PROJECT.config.main} not found in {PROJECT.root}", file=sys.stderr)
    try:
        srv = httpbase.listen(Handler, a.port, a.port_tries)
    except OSError as e:
        sys.exit(f"prism-local: {e}")
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    AGENT.server_url = url              # the agent's compile tool builds through this server
    stop = "closes after the last page" if a.exit_when_idle else "Ctrl-C to stop"
    print(f"prism-local: {url}\n  project: {PROJECT.root}\n  main:    {PROJECT.config.main}\n"
          f"  builds:  {', '.join(PROJECT.config.modes)}\n  {stop}", flush=True)
    if PROJECT.config.error:
        print(f"  warning: {PROJECT.config.error}", flush=True)
    if not is_scratch(PROJECT.root):
        registry.safe_touch(PROJECT.root)       # list the project on the Home page
    life.publish(url, port)
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if a.exit_when_idle:
        busy = lambda: PROJECT.busy()  # noqa: E731
        threading.Thread(target=httpbase.idle_watchdog, daemon=True,
                         args=(srv, PRESENCE, busy, "build or agent turn")).start()
    httpbase.stop_on_signals(srv)
    RESTART.update(server=srv, args=a, port=port)
    GITSYNC.start()
    # Update prism-local in the background; the editor then offers a restart.
    threading.Thread(target=selfupdate.update, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        transition = life.pending or life.transition()
        try:
            srv.server_close()
            life.unpublish()
            PROJECT.stop_build()
            AGENT.shutdown()
            GITSYNC.flush()             # what is left is committed and pushed
            if RESTART["again"]:
                destination = RESTART["move_to"] or PROJECT.root
                argv = move_argv(destination, moving=RESTART["move_to"] is not None)
                result = transition.handover(argv, str(PROJECT.root.parent))
                httpbase.log("restart failed: " + result["error"] if "error" in result
                             else "restart handed to the successor")
            else:
                httpbase.log("stopped")
        finally:
            transition.close()


if __name__ == "__main__":
    sys.exit(main())
