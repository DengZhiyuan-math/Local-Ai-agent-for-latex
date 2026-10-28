"""The project an editor works on: its folder, its settings, and the files it shows.

    project = Project(root)
    project.config                  # prism.json as it is now, with the document shown
    project.show("ch1.tex")         # the PDF pane shows another document
    project.resolve("sec/a.tex")    # a file the editor may open, or ValueError
    project.list_files()            # the editable files the Files sidebar lists

    project.save("main.tex", text, base_mtime, force)     # and upload, rename, new, paste, trash
    project.build("draft", active="ch1.tex")             # one at a time; project.busy()

One per editor server (server.PROJECT). The settings are read again whenever prism.json
changed, whoever asks (a request, the agent, the sync); the document shown is changed in
one place only (show), under a lock, so a build ending while prism.json changes cannot
put back an older one.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

import build
import build_results
import desktop
from errors import Conflict, NotFound, Refused
from fsutil import (EDITABLE_SUFFIXES, SKIP_DIRS, WINDOWS_RESERVED, bad_name, is_metadata,
                    project_path, with_line_ends_of, write_bytes)
from projectconfig import Config
from symbols import STANDARD_THEOREMS, _scan_bib, _scan_tex

MAX_FILES = 3000
SAVE_WAIT = 3           # seconds a save waits for git before the page is told to try again
UPLOAD_WAIT = 10        # seconds an upload or a menu action waits for git before "try again"
# Files added to an agent message (the + button, drag and drop, a pasted image) are
# saved in the project, so the agent reads them with its file tools.
UPLOAD_DIR = "prism-uploads"
MAX_UPLOAD = 25 * 1024 * 1024


class Project:
    def __init__(self, root: Path, *, files_lock=None, turn=None,
                 on_change: Callable[[], None] = lambda: None, builder=build.Build):
        """files_lock: held while the files are written (repository.FilesLock; git holds it
        too). turn: the agent's turn (agent.AgentManager: editing, busy, server_wrote).
        on_change: called after the editor changed the files (the sync's autosave).
        builder: what runs a build (build.Build; a fake one in a test)."""
        self._lock = threading.Lock()
        self._config = Config(root)
        self.root: Path = self._config.root
        self.files_lock = files_lock or threading.RLock()
        self.turn = turn or NoTurn()
        self.on_change = on_change
        self._scans: dict[tuple, tuple] = {}
        self.builder = builder
        self._build_lock = threading.Lock()
        self._running = None                    # the build in progress (stop_build)
        self._running_pdf: tuple | None = None  # its PDF, with that PDF's mtime before it

    # ------------------------------------------------------------ settings
    @property
    def config(self) -> Config:
        """The settings, read again when prism.json changed (a new engine or outdir applies
        at once). Replaced as a whole, never changed in place: read it once (cfg =
        project.config) for a consistent view."""
        try:
            stamp = (self.root / "prism.json").stat().st_mtime_ns
        except OSError:
            stamp = None
        with self._lock:
            old = self._config
            if stamp != old.stamp:
                # A pull, a merge or an agent changed it: a standalone document (not
                # prism.json's main) stays shown; the main follows prism.json.
                keep = old.main if old.main != old.project_main and (self.root / old.main).is_file() else None
                self._config = Config(self.root, main=keep)
            return self._config

    def show(self, main: str) -> Config:
        """Show the document `main` (its PDF in the PDF pane; the agent's compile tool
        builds it when nothing says which)."""
        with self._lock:
            if main != self._config.main:
                self._config = Config(self.root, main=main)
            return self._config

    # ------------------------------------------------------------ files
    def excluded(self, rel: str, cfg: Config | None = None) -> bool:
        """Not shown: hidden, a dependency or build folder, or excluded by prism.json."""
        cfg = cfg or self.config
        parts = rel.split("/")
        if any(p.startswith(".") for p in parts) or any(p in SKIP_DIRS for p in parts[:-1]):
            return True
        if rel.startswith(cfg.outdir + "/"):
            return True
        return any(fnmatch.fnmatch(rel, g) for g in cfg.exclude)

    def resolve(self, rel: str, *, editable_only: bool = True, cfg: Config | None = None) -> Path:
        """Map a project-relative path to a visible file, or raise Refused (a ValueError)."""
        if not rel or rel.startswith("/") or "\\" in rel:
            raise Refused("bad path")
        p = project_path(self.root, rel)
        r = p.relative_to(self.root).as_posix()
        if (editable_only and p.suffix not in EDITABLE_SUFFIXES) or self.excluded(r, cfg):
            raise Refused("not an editable file" if editable_only else "not a project file")
        return p

    def list_files(self, *, editable_only: bool = True) -> list[str]:
        cfg = self.config
        if cfg.files and editable_only:
            seen: dict[str, bool] = {}
            for g in cfg.files:
                for p in self.root.glob(g):
                    rel = p.relative_to(self.root).as_posix()
                    if p.is_file() and p.suffix in EDITABLE_SUFFIXES and not self.excluded(rel, cfg):
                        seen[rel] = False
            return self._checked(seen, True, cfg)
        return self._checked(self._gather(cfg, editable_only)[0], editable_only, cfg)

    def list_both(self, folders: list[str] | None = None) -> tuple[list[str], list[str]]:
        """list_files(editable_only=False) and list_files(), from one walk of the project; the
        folders it visited, empty ones too, are added to `folders` (not with config.files)."""
        cfg = self.config
        if cfg.files:
            return self.list_files(editable_only=False), self.list_files()
        every, editable = self._gather(cfg, False, True, folders=folders)
        return self._checked(every, False, cfg), self._checked(editable, True, cfg)

    @staticmethod
    def _plain(entry: os.DirEntry, rel: str) -> bool:
        """A file, not a link, whose path resolve() takes as it is."""
        try:
            return "\\" not in rel and not re.match(r"[A-Za-z]:", rel) \
                and not entry.name.endswith((".", " ")) and not entry.is_symlink() and entry.is_file()
        except OSError:
            return False

    def _walk(self, cfg: Config, folders: list[str] | None = None):
        """The project's files folder by folder, in the order of os.walk (sorted, depth first,
        linked folders left out): lists of (rel, plain). plain: a _plain file in a folder that
        resolves to where the walk found it (not reached through a junction), which is inside
        the project without resolving the file itself (slow on Windows, for every file).
        Each folder visited, empty ones too, is added to `folders`."""
        stack = [""]
        while stack:
            reld = stack.pop()
            if reld and folders is not None:
                folders.append(reld[:-1])
            top = self.root / reld
            try:
                with os.scandir(top) as it:
                    entries = list(it)
                here = top.resolve() == top
            except (OSError, RuntimeError):
                continue
            files, dirs = [], []
            for e in entries:
                try:
                    is_dir = e.is_dir()
                except OSError:
                    is_dir = False
                if not is_dir:
                    rel = reld + e.name
                    if not self.excluded(rel, cfg):
                        files.append((rel, here and self._plain(e, rel)))
                elif not e.name.startswith(".") and e.name not in SKIP_DIRS and reld + e.name != cfg.outdir:
                    dirs.append(e.name)
            yield files
            stack += [reld + d + "/" for d in sorted(dirs, reverse=True) if not os.path.islink(top / d)]

    def _gather(self, cfg: Config, *wanted: bool, folders: list[str] | None = None) -> list[dict[str, bool]]:
        """For each editable_only in `wanted`, the files list_files considers ({rel: plain}),
        all from one walk. Each list stops growing after the folder that takes it past MAX_FILES."""
        found: list[dict[str, bool]] = [{} for _ in wanted]
        for batch in self._walk(cfg, folders):
            for seen, editable_only in zip(found, wanted):
                if len(seen) <= MAX_FILES:
                    seen.update((rel, plain) for rel, plain in batch
                                if not editable_only or Path(rel).suffix in EDITABLE_SUFFIXES)
            if all(len(seen) > MAX_FILES for seen in found):
                break
        return found

    def _checked(self, seen: dict[str, bool], editable_only: bool, cfg: Config) -> list[str]:
        """The files of `seen` that resolve() allows (a plain one is allowed as it is), sorted."""
        safe = []
        for rel, plain in seen.items():
            try:
                if plain or self.resolve(rel, editable_only=editable_only, cfg=cfg).is_file():
                    safe.append(rel)
            except (ValueError, OSError, RuntimeError):
                pass
        return sorted(safe, key=lambda s: (s != cfg.main, s.count("/") == 0, s))

    # ------------------------------------------------------------ what the sources hold
    # Each file's scan is kept while its size and modification time stay the same: the
    # page asks for the outline after every save, and for the file order every two seconds.

    def scan(self, rel: str, kind: str = "tex"):
        """_scan_tex (kind "tex") or _scan_bib ("bib") of a project file, read only when it
        changed since the last scan; None when it cannot be read."""
        p = self.root / rel
        try:
            st = p.stat()
            key, stamp = (self.root, rel, kind), (st.st_mtime_ns, st.st_size)
            hit = self._scans.get(key)
            if hit and hit[0] == stamp:
                return hit[1]
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        found = _scan_bib(text) if kind == "bib" else _scan_tex(text)
        if len(self._scans) > 4 * MAX_FILES:      # files deleted or renamed long ago
            self._scans.clear()
        self._scans[key] = (stamp, found)
        return found

    def document_order(self, files: list[str] | None = None) -> list[str]:
        """Project .tex files in the order the main file \\input's them (depth first).
        `files`: self.list_files(), when the caller has it already."""
        order = self.input_chain(self.config.main)
        rest = [f for f in (self.list_files() if files is None else files) if f.endswith(".tex") and f not in order]
        return order + rest

    def theorem_envs(self, files: list[str]) -> dict[str, str]:
        """Theorem-like environments and the names they print: {"thm": "Theorem", …}."""
        envs: dict[str, str] = {}
        for rel in files:
            if Path(rel).suffix not in (".tex", ".sty", ".cls"):
                continue
            for name, title in (self.scan(rel) or {"theorems": []})["theorems"]:
                envs.setdefault(name, title)
        return envs or {e: e.capitalize() for e in STANDARD_THEOREMS}

    def symbols(self) -> dict:
        labels, outline, macros = [], [], []
        files = self.list_files()
        envs = self.theorem_envs(files)
        outline_envs = set(envs)
        for rel in self.document_order(files):
            found = self.scan(rel)
            if found is None:
                continue
            env = None
            for what, n, name, title in found["events"]:
                if what == "section":
                    outline.append({"file": rel, "line": n, "kind": name, "title": title})
                    env = name
                elif what == "begin":
                    if name in outline_envs:
                        outline.append({"file": rel, "line": n, "kind": name, "title": title})
                    env = name
                elif what == "macro":
                    macros.append({"name": name, "file": rel, "line": n})
                else:
                    labels.append({"label": name, "file": rel, "line": n, "kind": env or ""})
        keys = [{"key": key, "type": kind, "file": rel, "line": line}
                for rel in files if rel.endswith(".bib") for key, kind, line in self.scan(rel, "bib") or []]
        return {"labels": labels, "bibkeys": keys, "outline": outline, "macros": macros,
                "environments": list(envs), "env_titles": envs}

    def resolve_tex_name(self, name: str) -> str | None:
        return build.project_file(self.root, name)

    def stale_document(self) -> str | None:
        """A document whose .tex is newer than its PDF (just edited): the one shown if it is,
        else the most recently edited. None when every PDF is up to date."""
        stale, cfg = [], self.config
        for rel in self.list_files(editable_only=False):
            src = self.root / rel
            if src.suffix != ".tex" or rel.split("/")[0] == cfg.outdir:
                continue
            try:
                if not build.is_document(src.read_text(encoding="utf-8", errors="replace")):
                    continue
                pdf = self.root / cfg.outdir / f"{src.stem}.pdf"
                # Never built: only when edited after the last build (not an old draft lying there).
                built = pdf if pdf.exists() else cfg.pdf
                if mtime(src) > (mtime(built) if built.exists() else 0):
                    stale.append((rel == cfg.main, mtime(src), rel))
            except OSError:
                continue
        return max(stale)[2] if stale else None

    def input_chain(self, main: str) -> list[str]:
        """`main` and the files it \\input's or \\include's, depth first."""
        order: dict[str, None] = {}

        def visit(rel: str) -> None:
            if rel in order:
                return
            order[rel] = None
            for name in (self.scan(rel) or {"inputs": []})["inputs"]:
                child = self.resolve_tex_name(name)
                if child:
                    visit(child)
        visit(main)
        return list(order)

    def document_of(self, rel: str, cfg: Config) -> str:
        """The document a build started from `rel` builds: rel when it is a document; else the
        document shown now if it includes rel (a chapter of it), else prism.json's main if
        that does, else another document that does, else prism.json's main."""
        def is_doc(r: str) -> bool:
            try:
                return r.endswith(".tex") and build.is_document((self.root / r).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                return False
        if is_doc(rel):
            return rel
        firsts = [m for m in dict.fromkeys((cfg.main, cfg.project_main)) if (self.root / m).is_file()]
        others = [f for f in self.list_files() if f.endswith(".tex") and f not in firsts]
        for m in firsts + [f for f in others if is_doc(f)]:
            if rel in self.input_chain(m):
                return m
        return cfg.project_main

    def show_last_built(self) -> None:
        """Show the PDF of the document built last (its PDF is the newest), not always the
        project's main, so a reopened editor shows the PDF it showed before."""
        best, cfg = None, self.config
        for rel in self.list_files(editable_only=False):
            src = self.root / rel
            if src.suffix != ".tex" or rel.split("/")[0] == cfg.outdir:
                continue
            pdf = self.root / cfg.outdir / f"{src.stem}.pdf"
            try:
                if not pdf.exists() or not build.is_document(src.read_text(encoding="utf-8", errors="replace")):
                    continue
            except OSError:
                continue
            best = max(best or (0.0, ""), (mtime(pdf), rel))
        if best:
            self.show(best[1])

    # ------------------------------------------------------------ building
    # One build at a time. While it runs, its PDF is reported as it was before (pdf_state):
    # the engine rewrites the PDF on every run of a build, so it is not served half-written.

    @property
    def building(self) -> bool:
        return self._build_lock.locked()

    def busy(self) -> bool:
        """A build or an agent's turn runs: what changes the files or the server (a restart,
        a rename, resolving a clash with GitHub) waits for it."""
        return self.building or self.turn.busy()

    def writing(self, pdf: Path) -> bool:
        """Whether the running build is writing `pdf` now."""
        running = self._running_pdf
        return running is not None and running[0] == pdf

    def pdf_state(self, cfg: Config) -> float | None:
        """The mtime of cfg's PDF as pages should see it; None when there is none yet."""
        running = self._running_pdf
        if running and running[0] == cfg.pdf:
            return running[1]
        return mtime(cfg.pdf) if cfg.pdf.exists() else None

    def build(self, mode: str, clean: bool = False, active: str | None = None) -> dict:
        """One build (see build.py) of the document `active` belongs to (None: the one just
        edited, else the one shown); {"busy": True} while another runs."""
        shown = self.config
        if mode not in shown.modes:
            return {"busy": False, "exit": 127, "mode": mode, "seconds": 0, "diagnostics": [],
                    "output": f"There is no '{mode}' build.", "main": shown.main, "pdf_mtime": None}
        if not self._build_lock.acquire(blocking=False):
            return {"busy": True}
        try:
            if active is None:
                # No open file says which (the agent's compile tool, auto-compile after its
                # turn): the document edited since its PDF was built, else the one shown now.
                main = self.stale_document() or (shown.main if (self.root / shown.main).is_file() else shown.project_main)
            else:
                if not isinstance(active, str):
                    raise Refused("active must be a project file path")
                source = self.resolve(active)
                if not source.is_file():
                    raise FileNotFoundError(active)
                main = self.document_of(self.rel(source), shown)
            # Select outputs in memory. Opening a standalone document does not change
            # prism.json. It is shown once its build has ended: until then the PDF pane keeps
            # what it shows.
            cfg = Config(self.root, main=main)
            t0 = time.time()
            before = mtime(cfg.pdf) if cfg.pdf.exists() else None
            self._running_pdf = (cfg.pdf, before)
            proof = build_results.begin(cfg)
            cmd = cfg.custom.get(mode)
            b = self.builder(self.root, cfg.main, cfg.outdir, mode, builder=cfg.builder, engine=cfg.engine,
                             command=cfg.expand(cmd) if cmd else None, clean=clean,
                             shell_escape=cfg.shell_escape)
            self._running = b
            try:
                r = b.run()
            except Exception:
                build_results.finish(cfg, proof, {"exit": None, "pdf_updated": False})
                raise
            finally:
                self._running_pdf = None
                self.show(cfg.main)
                self.turn.server_wrote([cfg.outdir])    # a build's output is no agent turn's change
            if cfg.error:
                r["output"] = f"prism-local: {cfg.error}\n" + r["output"]
            pdf = mtime(cfg.pdf) if cfg.pdf.exists() else None
            result = {**r, "busy": False, "mode": mode, "seconds": round(time.time() - t0, 1),
                      "main": cfg.main, "pdf_mtime": pdf, "pdf_updated": pdf is not None and pdf != before}
            build_results.finish(cfg, proof, result)
            return result
        finally:
            self._running = self._running_pdf = None
            self._build_lock.release()

    def stop_build(self) -> bool:
        b = self._running
        if b is not None:
            b.stop()
        return b is not None

    # ------------------------------------------------------------ writing the files
    # Saves, uploads and the Files sidebar's menu: each holds the files (files_lock: git
    # commits and merges hold it too) and tells the agent's turn that the editor wrote it
    # (turn.server_wrote), so the turn neither reports it as its change nor reverts it.

    def save(self, rel: str, content: str, base_mtime: float | None, force: bool) -> tuple[dict, int]:
        """Write an editor buffer to disk, unless the file changed on disk since the editor
        loaded it (a conflict). The file keeps its line ends (the editor sends \\n). Busy
        (git, an Edit turn) is answered at once with 503 and "busy": the page tries again."""
        p = self.resolve(rel)
        # Check-then-write must not interleave with another save, nor with git committing or
        # merging. git's work takes moments, but a repository's hooks may take long: then the
        # page is told to try again, rather than kept waiting with a connection held.
        if not self.files_lock.acquire(timeout=SAVE_WAIT):
            return {"busy": True, "error": "git is busy with the files; saving as soon as it is done"}, 503
        try:
            if self.turn.editing():
                # Its turn's diff and Undo would take your text for the agent's (also from a
                # page that did not see the turn start, or was reloaded during it).
                return {"busy": True, "error": "the agent is editing the files; your edits are "
                                               "saved when its turn ends"}, 503
            if p.exists() and base_mtime is not None and abs(mtime(p) - base_mtime) > 1e-6 \
                    and not force:
                return {"conflict": True, "mtime": mtime(p)}, 409
            write_bytes(p, with_line_ends_of(content, p).encode("utf-8"))
            self.turn.server_wrote([self.rel(p)])        # yours, not an Ask turn's
        finally:
            self.files_lock.release()
        self.on_change()
        return {"ok": True, "mtime": mtime(p)}, 200

    def save_conditional(self, rel: str, content: str, expected_sha256: str) -> tuple[dict, int]:
        """A separate endpoint so older services cannot silently ignore the hash guard."""
        if (not isinstance(content, str) or not 1 <= len(content.encode("utf-8")) <= 4_000_000
                or not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)):
            raise Refused("invalid conditional source edit")
        p = self.resolve(rel)
        if not self.files_lock.acquire(timeout=SAVE_WAIT):
            return {"busy": True, "error": "the source files are busy"}, 503
        try:
            if self.turn.editing():
                return {"busy": True, "error": "the agent is editing the files"}, 503
            before = p.read_bytes()
            if hashlib.sha256(before).hexdigest() != expected_sha256:
                return {"conflict": True}, 409
            raw = content.encode("utf-8")
            write_bytes(p, raw)
            self.turn.server_wrote([self.rel(p)])
        finally:
            self.files_lock.release()
        self.on_change()
        return {"ok": True, "sha256": hashlib.sha256(raw).hexdigest(), "mtime": mtime(p)}, 200

    def upload(self, name: str, data: bytes, folder: str = UPLOAD_DIR) -> str:
        """Save an upload in the project (attachments under prism-uploads/). Reuse identical
        files; give different files with the same name a number (notes-2.pdf)."""
        if len(data) > MAX_UPLOAD:
            raise Refused(f"the file is larger than {MAX_UPLOAD // (1024 * 1024)} MB")
        clean = upload_name(name)
        stem, dot, ext = clean.rpartition(".")
        if not dot:
            stem, ext = clean, ""
        if not self.files_lock.acquire(timeout=UPLOAD_WAIT):
            raise Conflict("git is busy with the files; try the upload again in a moment")
        try:
            for n in range(1, 1000):
                cand = clean if n == 1 else f"{stem}-{n}" + (f".{ext}" if ext else "")
                rel = f"{folder}/{cand}" if folder else cand
                p = project_path(self.root, rel)
                if not folder and self.excluded(self.rel(p)):
                    raise Refused("the file is excluded from this project")
                if p.exists():
                    try:
                        if p.read_bytes() == data:
                            return rel
                    except OSError:
                        pass
                    continue
                write_bytes(p, data)
                self.turn.server_wrote([self.rel(p)])    # yours, not a turn's
                return rel
        finally:
            self.files_lock.release()
        raise Refused("too many files with this name")

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    # The Files sidebar's menu: copy and paste (the system's clipboard is the server's
    # business: these take what to copy), rename, new file or folder, and delete to the
    # Recycle Bin. Each acts on what the sidebar shows (a file or folder as it is, never
    # through a link), and never during an agent's turn (its diff and Undo would take it).

    def item(self, rel: str, *, folder_only: bool = False) -> Path:
        """The project's file or folder `rel` ("" is the project), as the sidebar shows it."""
        if not rel:
            return self.root
        p = project_path(self.root, rel)
        if p != self.root / rel or self.excluded(rel + ("/" if p.is_dir() else "")) or is_metadata(Path(rel)):
            raise Refused(f"not a file or folder the editor shows: {rel}")
        if not p.exists():
            raise NotFound(f"not found: {rel}")
        if folder_only and not p.is_dir():
            raise Refused(f"not a folder: {rel}")
        return p

    @contextmanager
    def _changing(self):
        """Hold the files (as a save does); refuse during an agent's turn."""
        if not self.turn.busy() and self.files_lock.acquire(timeout=UPLOAD_WAIT):
            try:
                if self.turn.busy():            # it started while we waited for git
                    raise FilesBusy("wait until the agent's turn has finished")
                yield
            finally:
                self.files_lock.release()
        elif self.turn.busy():
            raise FilesBusy("wait until the agent's turn has finished")
        else:
            raise FilesBusy("git is busy with the files; try again in a moment")

    def paste(self, folder: str, sources: list[Path]) -> dict:
        """Copy files and folders (from the clipboard) into the project's `folder`. Metadata
        (.git, .codex …) inside copied folders is left out, so it becomes no nested repository."""
        target = self.item(folder, folder_only=True)
        pasted, skipped = [], []
        with self._changing():
            for src in sources:
                src = Path(src)
                if not src.exists():
                    skipped.append(f"{src.name}: no longer exists")
                    continue
                if is_metadata(Path(src.name)) or bad_name(src.name):
                    skipped.append(f"{src.name}: {bad_name(src.name) or 'repository metadata is not copied'}")
                    continue
                real = src.resolve()
                if src.is_dir() and (real == target or real in target.parents):
                    skipped.append(f"{src.name}: a folder cannot be pasted into itself")
                    continue
                dest = target / free_name(target, src.name)
                if src.is_dir():
                    shutil.copytree(src, dest, symlinks=True,
                                    ignore=lambda _, names: [n for n in names if is_metadata(Path(n))])
                else:
                    shutil.copy2(src, dest)
                pasted.append(self.rel(dest))
            self.turn.server_wrote(pasted)
        if pasted:
            self.on_change()
        return {"pasted": pasted, "skipped": skipped}

    def rename(self, rel: str, name: str) -> dict:
        src = self.item(rel)
        if src == self.root:
            raise Refused("rename the project from its name at the top")
        why = bad_name(name)
        if why:
            raise Refused(why)
        dest = src.with_name(name)
        with self._changing():
            # On Windows and macOS, main.tex -> Main.tex finds the file itself there.
            if os.path.lexists(dest) and not (dest.exists() and os.path.samefile(src, dest)):
                raise Conflict(f"{name} already exists in this folder")
            os.rename(src, dest)
            self.turn.server_wrote([rel, self.rel(dest)])
        self.on_change()
        return {"path": self.rel(dest)}

    def new(self, folder: str, name: str, kind: str) -> dict:
        target = self.item(folder, folder_only=True)
        why = bad_name(name)
        if why:
            raise Refused(why)
        p = target / name
        if self.excluded(self.rel(p) + ("/" if kind == "folder" else "")):
            raise Refused("this project excludes that name; the editor would not show it")
        with self._changing():
            if os.path.lexists(p):
                raise Conflict(f"{name} already exists in this folder")
            if kind == "folder":
                p.mkdir()
            else:
                p.touch(exist_ok=False)
            self.turn.server_wrote([self.rel(p)])
        self.on_change()
        return {"path": self.rel(p), "kind": "folder" if kind == "folder" else "file"}

    def trash(self, rels: list[str]) -> dict:
        paths = [self.item(rel) for rel in rels]
        if self.root in paths:
            raise Refused("delete the project from the Home page")
        with self._changing():
            desktop.trash(paths)
            self.turn.server_wrote(rels)
        self.on_change()
        return {"trashed": rels}


def mtime(p: Path) -> float:
    return p.stat().st_mtime_ns / 1e9


def upload_name(name: str) -> str:
    """A safe file name from the browser's: no folders, no characters Windows refuses."""
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", str(name).replace("\\", "/").split("/")[-1]).strip(" .")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = stem[:100] or "file"
    if stem.upper() in WINDOWS_RESERVED:
        stem += "_"
    return stem + (f".{ext[:12]}" if ext else "")


def free_name(folder: Path, name: str) -> str:
    """`name`, or name-copy, name-copy-2 … when it is taken (no spaces: LaTeX's \\input)."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    for n in range(1000):
        cand = name if n == 0 else f"{stem}-copy" + (f"-{n}" if n > 1 else "") + (f".{ext}" if ext else "")
        if not os.path.lexists(folder / cand):
            return cand
    raise Refused(f"too many copies of {name}")


class FilesBusy(Conflict):
    """The files cannot change now (git or an agent's turn): try again later."""


class NoTurn:
    """No agent: nothing is ever being edited by one (a Project on its own, in a test)."""

    def editing(self) -> bool:
        return False

    def busy(self) -> bool:
        return False

    def server_wrote(self, paths: list[str]) -> None:
        pass
