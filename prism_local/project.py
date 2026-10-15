"""The project an editor works on: its folder, its settings, and the files it shows.

    project = Project(root)
    project.config                  # prism.json as it is now, with the document shown
    project.show("ch1.tex")         # the PDF pane shows another document
    project.resolve("sec/a.tex")    # a file the editor may open, or ValueError
    project.list_files()            # the editable files the Files sidebar lists

One per editor server (server.PROJECT). The settings are read again whenever prism.json
changed, whoever asks (a request, the agent, the sync); the document shown is changed in
one place only (show), under a lock, so a build ending while prism.json changes cannot
put back an older one.
"""
from __future__ import annotations

import fnmatch
import os
import re
import threading
from pathlib import Path

from fsutil import EDITABLE_SUFFIXES, SKIP_DIRS, project_path
from projectconfig import Config

MAX_FILES = 3000


class Project:
    def __init__(self, root: Path):
        self._lock = threading.Lock()
        self._config = Config(root)
        self.root: Path = self._config.root

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
        """Map a project-relative path to a visible file, or raise ValueError."""
        if not rel or rel.startswith("/") or "\\" in rel:
            raise ValueError("bad path")
        p = project_path(self.root, rel)
        r = p.relative_to(self.root).as_posix()
        if (editable_only and p.suffix not in EDITABLE_SUFFIXES) or self.excluded(r, cfg):
            raise ValueError("not an editable file" if editable_only else "not a project file")
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
