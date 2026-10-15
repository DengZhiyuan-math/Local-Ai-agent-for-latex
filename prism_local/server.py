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
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent import AgentManager  # noqa: E402
import gitsync  # noqa: E402
from gitsync import GitSync  # noqa: E402
from projectconfig import Config  # noqa: E402
from repository import FilesLock, RepoBusy  # noqa: E402
from fsutil import (EDITABLE_SUFFIXES, SKIP_DIRS, WINDOWS_RESERVED, bad_name, is_metadata,  # noqa: E402
                    project_path, with_line_ends_of, write_bytes)
import desktop  # noqa: E402
from errors import Conflict, NotFound, Refused  # noqa: E402
import synctex  # noqa: E402
from presence import Presence  # noqa: E402
from texutil import group, plain_text  # noqa: E402
import build  # noqa: E402
import httpbase  # noqa: E402
import instances  # noqa: E402
import publish  # noqa: E402
import registry  # noqa: E402
import selfupdate  # noqa: E402
import sources  # noqa: E402

MAX_FILES = 3000
BUILD_LOCK = threading.Lock()
# The project's files are written under it: saves, uploads, an Undo, and GITSYNC's commits
# and merges; also by the other editors of a shared repository (repository.FilesLock).
SAVE_LOCK = FilesLock(lambda: GITSYNC.git_dir if GITSYNC.check_repo() else None)
SAVE_WAIT = 3           # seconds a save waits for git before the page is told to try again
UPLOAD_WAIT = 10        # seconds an upload waits for git before it is refused (try again)

STANDARD_THEOREMS = ["theorem", "proposition", "lemma", "corollary", "definition",
                     "remark", "example"]


# The project's settings, with the document shown now (CFG.main: its PDF is the one in the
# PDF pane, and the one the agent's compile tool builds when nothing says which). Replaced
# as a whole, never changed in place; a request reads it once (cfg = CFG) and uses that.
CFG: Config = None  # type: ignore[assignment]
ROOT: Path = Path.cwd()


def set_root(root: Path) -> None:
    global CFG, ROOT
    CFG = Config(root)
    ROOT = CFG.root


def refresh_config() -> None:
    """Load prism.json again when it changed, so a new engine or outdir applies at once."""
    global CFG
    try:
        stamp = (ROOT / "prism.json").stat().st_mtime_ns
    except OSError:
        stamp = None
    if stamp != CFG.stamp:
        # A pull, a merge or an agent changed it: a standalone document (not prism.json's
        # main) stays shown; the main follows prism.json.
        old = CFG
        keep = old.main if old.main != old.project_main and (ROOT / old.main).is_file() else None
        CFG = Config(ROOT, main=keep)


# ---------------------------------------------------------------- files

def _excluded(rel: str) -> bool:
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts) or any(p in SKIP_DIRS for p in parts[:-1]):
        return True
    if rel.startswith(CFG.outdir + "/"):
        return True
    return any(fnmatch.fnmatch(rel, g) for g in CFG.exclude)


def resolve(rel: str, *, editable_only: bool = True) -> Path:
    """Map a project-relative path to a visible file, or raise ValueError."""
    if not rel or rel.startswith("/") or "\\" in rel:
        raise ValueError("bad path")
    p = project_path(ROOT, rel)
    r = p.relative_to(ROOT).as_posix()
    if (editable_only and p.suffix not in EDITABLE_SUFFIXES) or _excluded(r):
        raise ValueError("not an editable file" if editable_only else "not a project file")
    return p


def _plain(entry: os.DirEntry, rel: str) -> bool:
    """A file, not a link, whose path resolve() takes as it is."""
    try:
        return "\\" not in rel and not re.match(r"[A-Za-z]:", rel) \
            and not entry.name.endswith((".", " ")) and not entry.is_symlink() and entry.is_file()
    except OSError:
        return False


def _walk(folders: list[str] | None = None):
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
        top = ROOT / reld
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
                if not _excluded(rel):
                    files.append((rel, here and _plain(e, rel)))
            elif not e.name.startswith(".") and e.name not in SKIP_DIRS and reld + e.name != CFG.outdir:
                dirs.append(e.name)
        yield files
        stack += [reld + d + "/" for d in sorted(dirs, reverse=True) if not os.path.islink(top / d)]


def _gather(*wanted: bool, folders: list[str] | None = None) -> list[dict[str, bool]]:
    """For each editable_only in `wanted`, the files list_files considers ({rel: plain}),
    all from one walk. Each list stops growing after the folder that takes it past MAX_FILES."""
    found: list[dict[str, bool]] = [{} for _ in wanted]
    for batch in _walk(folders):
        for seen, editable_only in zip(found, wanted):
            if len(seen) <= MAX_FILES:
                seen.update((rel, plain) for rel, plain in batch
                            if not editable_only or Path(rel).suffix in EDITABLE_SUFFIXES)
        if all(len(seen) > MAX_FILES for seen in found):
            break
    return found


def _checked(seen: dict[str, bool], editable_only: bool) -> list[str]:
    """The files of `seen` that resolve() allows (a plain one is allowed as it is), sorted."""
    safe = []
    for rel, plain in seen.items():
        try:
            if plain or resolve(rel, editable_only=editable_only).is_file():
                safe.append(rel)
        except (ValueError, OSError, RuntimeError):
            pass
    return sorted(safe, key=lambda s: (s != CFG.main, s.count("/") == 0, s))


def list_files(*, editable_only: bool = True) -> list[str]:
    if CFG.files and editable_only:
        seen: dict[str, bool] = {}
        for g in CFG.files:
            for p in ROOT.glob(g):
                rel = p.relative_to(ROOT).as_posix()
                if p.is_file() and p.suffix in EDITABLE_SUFFIXES and not _excluded(rel):
                    seen[rel] = False
        return _checked(seen, True)
    return _checked(_gather(editable_only)[0], editable_only)


def list_both(folders: list[str] | None = None) -> tuple[list[str], list[str]]:
    """list_files(editable_only=False) and list_files(), from one walk of the project; the
    folders it visited, empty ones too, are added to `folders` (not with CFG.files)."""
    if CFG.files:
        return list_files(editable_only=False), list_files()
    every, editable = _gather(False, True, folders=folders)
    return _checked(every, False), _checked(editable, True)


def git(*args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    """Read-only git in the project (gitenv.git)."""
    return gitenv.git(ROOT, *args, timeout=timeout)


_GIT_PREFIX: dict = {}


def git_prefix() -> str:
    """Where the project sits in its repository: "" at the top, "examples/minimal/" …"""
    if ROOT not in _GIT_PREFIX:
        try:
            _GIT_PREFIX[ROOT] = git("rev-parse", "--show-prefix").stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return _GIT_PREFIX[ROOT]


def git_status() -> dict[str, str]:
    """{project-relative path: status} of the files git reports as changed."""
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


def mtime(p: Path) -> float:
    return p.stat().st_mtime_ns / 1e9


def save_file(rel: str, content: str, base_mtime: float | None, force: bool) -> tuple[dict, int]:
    """Write an editor buffer to disk, unless the file changed on disk since the editor
    loaded it (a conflict). The file keeps its line ends (the editor sends \\n)."""
    p = resolve(rel)
    # Check-then-write must not interleave with another save, nor with git committing or
    # merging. git's work takes moments, but a repository's hooks may take long: then the
    # page is told to try again, rather than kept waiting with a connection held.
    if not SAVE_LOCK.acquire(timeout=SAVE_WAIT):
        return {"busy": True, "error": "git is busy with the files; saving as soon as it is done"}, 503
    try:
        if AGENT.editing():
            # Its turn's diff and Undo would take your text for the agent's (also from a
            # page that did not see the turn start, or was reloaded during it).
            return {"busy": True, "error": "the agent is editing the files; your edits are "
                                           "saved when its turn ends"}, 503
        if p.exists() and base_mtime is not None and abs(mtime(p) - base_mtime) > 1e-6 \
                and not force:
            return {"conflict": True, "mtime": mtime(p)}, 409
        write_bytes(p, with_line_ends_of(content, p).encode("utf-8"))
        AGENT.server_wrote([p.relative_to(ROOT).as_posix()])     # yours, not an Ask turn's
        return {"ok": True, "mtime": mtime(p)}, 200
    finally:
        SAVE_LOCK.release()


# ---------------------------------------------------------------- symbols

LABEL_RE = re.compile(r"\\label\{([^}]+)\}")
BEGIN_RE = re.compile(r"\\begin\{([A-Za-z*]+)\}(?:\[([^\]]*)\])?")
# \section{…}, \section*{…}, \section[short]{long}; the title is read with its braces paired.
SECTION_RE = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*\{")
INPUT_RE = re.compile(r"\\(?:input|include)\{([^}]+)\}")
MACRO_RE = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand|DeclareMathOperator|DeclarePairedDelimiter)\*?"
    r"\s*\{?\\([A-Za-z]+)\}?")
BIBKEY_RE = re.compile(r"^\s*@(\w+)\s*\{\s*([^,\s]+)\s*,", re.M)
# \newtheorem{thm}{Theorem}[section], \newtheorem{lem}[thm]{Lemma}, llncs, mdframed, tcolorbox …
NEWTHEOREM_RE = re.compile(r"\\(?:newtheorem|spnewtheorem|newmdtheoremenv|newtcbtheorem)\*?\s*"
                           r"\{([^}]+)\}\s*(?:\[[^\]]*\]\s*)?(?:\{([^}]*)\})?")
# thmtools: \declaretheorem[name=Theorem, numberwithin=section]{thm}
DECLARETHEOREM_RE = re.compile(r"\\declaretheorem\*?\s*(?:\[([^\]]*)\])?\s*\{([^}]+)\}")


def strip_comment(line: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", line)


def _scan_tex(text: str) -> dict:
    """What a .tex, .sty or .cls file holds for document_order and symbols, line by line
    in the order symbols() takes it: its \\input's, theorem definitions, and its sections,
    \\begin's, macros and labels (events: (what, line, name, title))."""
    inputs, theorems, events = [], [], []
    for n, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw)
        inputs += [m.group(1).strip() for m in INPUT_RE.finditer(line)]
        for m in NEWTHEOREM_RE.finditer(line):
            theorems.append((m.group(1).strip(), (m.group(2) or m.group(1)).strip()))
        for m in DECLARETHEOREM_RE.finditer(line):
            name = m.group(2).strip()
            opt = re.search(r"(?:^|,)\s*(?:name|title)\s*=\s*\{?([^,}]+)", m.group(1) or "")
            theorems.append((name, opt.group(1).strip() if opt else name.capitalize()))
        m = SECTION_RE.search(line)
        if m:
            g = group(line, m.end() - 1)          # a title that runs on: this line's part
            events.append(("section", n, m.group(1), plain_text(g[0] if g else line[m.end():])))
        for m in BEGIN_RE.finditer(line):
            events.append(("begin", n, m.group(1), plain_text(m.group(2) or "")))
        for m in MACRO_RE.finditer(line):
            events.append(("macro", n, m.group(1), None))
        for m in LABEL_RE.finditer(line):
            events.append(("label", n, m.group(1), None))
    return {"inputs": inputs, "theorems": theorems, "events": events}


def _scan_bib(text: str) -> list[tuple]:
    """The entries of a .bib file: (key, type, line)."""
    keys, line, at = [], 1, 0
    for m in BIBKEY_RE.finditer(text):
        line += text.count("\n", at, m.start())
        at = m.start()
        if m.group(1).lower() not in ("string", "preamble", "comment"):
            keys.append((m.group(2), m.group(1).lower(), line))
    return keys


# Each file's scan, kept while its size and modification time stay the same: the page asks
# for the outline after every save, and for the file order every two seconds.
_SCANS: dict[tuple, tuple] = {}


def scan(rel: str, kind: str = "tex"):
    """_scan_tex (kind "tex") or _scan_bib ("bib") of a project file, read only when it
    changed since the last scan; None when it cannot be read."""
    p = ROOT / rel
    try:
        st = p.stat()
        key, stamp = (ROOT, rel, kind), (st.st_mtime_ns, st.st_size)
        hit = _SCANS.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = _scan_bib(text) if kind == "bib" else _scan_tex(text)
    if len(_SCANS) > 4 * MAX_FILES:      # files deleted or renamed long ago
        _SCANS.clear()
    _SCANS[key] = (stamp, found)
    return found


def document_order(files: list[str] | None = None) -> list[str]:
    """Project .tex files in the order the main file \\input's them (depth first).
    `files`: list_files(), when the caller has it already."""
    order = input_chain(CFG.main)
    rest = [f for f in (list_files() if files is None else files) if f.endswith(".tex") and f not in order]
    return order + rest


def theorem_envs(files: list[str]) -> dict[str, str]:
    """Theorem-like environments and the names they print: {"thm": "Theorem", …}."""
    envs: dict[str, str] = {}
    for rel in files:
        if Path(rel).suffix not in (".tex", ".sty", ".cls"):
            continue
        for name, title in (scan(rel) or {"theorems": []})["theorems"]:
            envs.setdefault(name, title)
    return envs or {e: e.capitalize() for e in STANDARD_THEOREMS}


def symbols() -> dict:
    labels, outline, macros = [], [], []
    files = list_files()
    envs = theorem_envs(files)
    outline_envs = set(envs)
    for rel in document_order(files):
        found = scan(rel)
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
            for rel in files if rel.endswith(".bib") for key, kind, line in scan(rel, "bib") or []]
    return {"labels": labels, "bibkeys": keys, "outline": outline, "macros": macros,
            "environments": list(envs), "env_titles": envs}


# ---------------------------------------------------------------- build

def resolve_tex_name(name: str) -> str | None:
    return build.project_file(ROOT, name)


# The build in progress (for /api/build/stop), and the PDF it writes with that PDF's mtime
# from before it began: the engine rewrites the PDF on every run of a build, so while it
# works the PDF is reported as it was (pdf_state) and not served half-written.
RUNNING: dict = {"build": None, "pdf": None}


def pdf_state(cfg: Config) -> float | None:
    """The mtime of cfg's PDF as pages should see it; None when there is none yet."""
    writing = RUNNING["pdf"]
    if writing and writing[0] == cfg.pdf:
        return writing[1]
    return mtime(cfg.pdf) if cfg.pdf.exists() else None

# Files added to an agent message (the + button, drag and drop, a pasted image) are
# saved in the project, so the agent reads them with its file tools.
UPLOAD_DIR = "prism-uploads"
MAX_UPLOAD = 25 * 1024 * 1024


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


def save_upload(name: str, data: bytes, *, folder: str = UPLOAD_DIR) -> str:
    """Save an upload in the project (attachments under prism-uploads/). Reuse identical
    files; give different files with the same name a number (notes-2.pdf)."""
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"the file is larger than {MAX_UPLOAD // (1024 * 1024)} MB")
    clean = upload_name(name)
    stem, dot, ext = clean.rpartition(".")
    if not dot:
        stem, ext = clean, ""
    if not SAVE_LOCK.acquire(timeout=UPLOAD_WAIT):
        raise ValueError("git is busy with the files; try the upload again in a moment")
    try:
        for n in range(1, 1000):
            cand = clean if n == 1 else f"{stem}-{n}" + (f".{ext}" if ext else "")
            rel = f"{folder}/{cand}" if folder else cand
            p = project_path(ROOT, rel)
            if not folder and _excluded(p.relative_to(ROOT).as_posix()):
                raise ValueError("the file is excluded from this project")
            if p.exists():
                try:
                    if p.read_bytes() == data:
                        return rel
                except OSError:
                    pass
                continue
            write_bytes(p, data)
            AGENT.server_wrote([p.relative_to(ROOT).as_posix()])     # yours, not a turn's
            return rel
    finally:
        SAVE_LOCK.release()
    raise ValueError("too many files with this name")


# The Files sidebar's menu: show in Explorer/Finder, copy and paste through the system's
# clipboard, rename, new file or folder, and delete to the Recycle Bin. Each acts on what
# the sidebar shows (a file or folder as it is, never through a link) and, like a save,
# waits for git and never runs during an agent's turn (its diff and Undo would take it).

def item_path(rel: str, *, folder_only: bool = False) -> Path:
    """The project's file or folder `rel` ("" is the project), as the sidebar shows it."""
    if not rel:
        return ROOT
    p = project_path(ROOT, rel)
    if p != ROOT / rel or _excluded(rel + ("/" if p.is_dir() else "")) or is_metadata(Path(rel)):
        raise ValueError(f"not a file or folder the editor shows: {rel}")
    if not p.exists():
        raise ValueError(f"not found: {rel}")
    if folder_only and not p.is_dir():
        raise ValueError(f"not a folder: {rel}")
    return p


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


class FilesBusy(Conflict):
    """The files cannot change now (git or an agent's turn): try again later."""


@contextmanager
def files_changing():
    """Hold the files (as a save does); refuse during an agent's turn."""
    if not AGENT.busy() and SAVE_LOCK.acquire(timeout=UPLOAD_WAIT):
        try:
            if AGENT.busy():            # it started while we waited for git
                raise FilesBusy("wait until the agent's turn has finished")
            yield
        finally:
            SAVE_LOCK.release()
    elif AGENT.busy():
        raise FilesBusy("wait until the agent's turn has finished")
    else:
        raise FilesBusy("git is busy with the files; try again in a moment")


def free_name(folder: Path, name: str) -> str:
    """`name`, or name-copy, name-copy-2 … when it is taken (no spaces: LaTeX's \\input)."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    for n in range(1000):
        cand = name if n == 0 else f"{stem}-copy" + (f"-{n}" if n > 1 else "") + (f".{ext}" if ext else "")
        if not os.path.lexists(folder / cand):
            return cand
    raise ValueError(f"too many copies of {name}")


def paste_files(folder: str, sources: list[Path]) -> dict:
    """Copy files and folders (from the clipboard) into the project's `folder`. Metadata
    (.git, .codex …) inside copied folders is left out, so it becomes no nested repository."""
    target = item_path(folder, folder_only=True)
    pasted, skipped = [], []
    with files_changing():
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
            pasted.append(_rel(dest))
        AGENT.server_wrote(pasted)
    if pasted:
        GITSYNC.touched()
    return {"pasted": pasted, "skipped": skipped}


def rename_item(rel: str, name: str) -> dict:
    src = item_path(rel)
    if src == ROOT:
        raise ValueError("rename the project from its name at the top")
    why = bad_name(name)
    if why:
        raise ValueError(why)
    dest = src.with_name(name)
    with files_changing():
        # On Windows and macOS, main.tex -> Main.tex finds the file itself there.
        if os.path.lexists(dest) and not (dest.exists() and os.path.samefile(src, dest)):
            raise ValueError(f"{name} already exists in this folder")
        os.rename(src, dest)
        AGENT.server_wrote([rel, _rel(dest)])
    GITSYNC.touched()
    return {"path": _rel(dest)}


def new_item(folder: str, name: str, kind: str) -> dict:
    target = item_path(folder, folder_only=True)
    why = bad_name(name)
    if why:
        raise ValueError(why)
    p = target / name
    if _excluded(_rel(p) + ("/" if kind == "folder" else "")):
        raise ValueError("this project excludes that name; the editor would not show it")
    with files_changing():
        if os.path.lexists(p):
            raise ValueError(f"{name} already exists in this folder")
        if kind == "folder":
            p.mkdir()
        else:
            p.touch(exist_ok=False)
        AGENT.server_wrote([_rel(p)])
    GITSYNC.touched()
    return {"path": _rel(p), "kind": "folder" if kind == "folder" else "file"}


def trash_items(rels: list[str]) -> dict:
    paths = [item_path(rel) for rel in rels]
    if ROOT in paths:
        raise ValueError("delete the project from the Home page")
    with files_changing():
        desktop.trash(paths)
        AGENT.server_wrote(rels)
    GITSYNC.touched()
    return {"trashed": rels}


# Every upload is committed in the project's own repository, alone (other changes stay as
# they are), and pushed, through the same machinery as the other commits (gitsync.py).
UPLOAD_SYNC: dict[str, dict] = {}     # rel -> {"state": syncing|synced|local|failed, "message"}


def sync_upload(rel: str) -> None:
    """Commit the upload `rel` and push it (in a thread; the state is in UPLOAD_SYNC)."""
    try:
        o = GITSYNC.record_file(rel, f"Add {rel} (a file used in the agent chat)")
        UPLOAD_SYNC[rel] = {"state": o.state, "message": o.message}
    except RepoBusy:
        GITSYNC.touched()           # the next autosave commits it
        UPLOAD_SYNC[rel] = {"state": "local", "message": "Saved here; another prism-local window is "
                            "using the repository, so it is committed with the next autosave"}
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as e:
        UPLOAD_SYNC[rel] = {"state": "failed", "message": f"git: {str(e)[-300:]}"}

# Your snippets (static/snippets.js): one file for every project. JavaScript, run by the
# editor page, so it lives with your settings and not in a project co-authors share.
def snippets_file() -> Path:
    return Path(os.environ.get("PRISM_SNIPPETS") or Path.home() / ".prism-local" / "snippets.js")


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
    if not name or not folder or folder == ROOT.name:
        return {"name": registry.rename(ROOT, name), "moving": False}, 200
    new = ROOT.parent / folder
    if new.exists() and registry.norm(new) != registry.norm(ROOT):
        return {"error": f"a folder named {folder} already exists next to this project"}, 409
    if BUILD_LOCK.locked() or AGENT.busy() or RESTART["server"] is None:
        return {"error": "wait until the build or the agent's turn has finished"}, 409
    if LIFETIME is None:
        return {"error": "not ready to restart"}, 409
    try:
        transition = LIFETIME.transition(new)
    except (instances.Busy, TimeoutError, OSError) as exc:
        return {"error": str(exc)}, 409
    try:
        registry.rename(ROOT, name)      # the list keeps the exact name; move_folder tidies it
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
        argv += ["--moved-from", str(ROOT)]
    if a.exit_when_idle:
        argv.append("--exit-when-idle")
    if a.idle_timings:
        argv += ["--idle-timings", a.idle_timings]
    if a.ready_file:
        alias = LIFETIME.alias if LIFETIME else None
        if alias and moving and registry.inside(alias, ROOT):
            alias = new / alias.relative_to(ROOT)
        argv += ["--ready-file", str(alias or instances.path(new))]
    return argv


def stale_document() -> str | None:
    """A document whose .tex is newer than its PDF (just edited): the one shown if it is,
    else the most recently edited. None when every PDF is up to date."""
    stale = []
    for rel in list_files(editable_only=False):
        src = ROOT / rel
        if src.suffix != ".tex" or rel.split("/")[0] == CFG.outdir:
            continue
        try:
            if not build.is_document(src.read_text(encoding="utf-8", errors="replace")):
                continue
            pdf = ROOT / CFG.outdir / f"{src.stem}.pdf"
            # Never built: only when edited after the last build (not an old draft lying there).
            built = pdf if pdf.exists() else CFG.pdf
            if mtime(src) > (mtime(built) if built.exists() else 0):
                stale.append((rel == CFG.main, mtime(src), rel))
        except OSError:
            continue
    return max(stale)[2] if stale else None


def input_chain(main: str) -> list[str]:
    """`main` and the files it \\input's or \\include's, depth first."""
    order: dict[str, None] = {}

    def visit(rel: str) -> None:
        if rel in order:
            return
        order[rel] = None
        for name in (scan(rel) or {"inputs": []})["inputs"]:
            child = resolve_tex_name(name)
            if child:
                visit(child)
    visit(main)
    return list(order)


def document_of(rel: str, cfg: Config) -> str:
    """The document a build started from `rel` builds: rel when it is a document; else the
    document shown now if it includes rel (a chapter of it), else prism.json's main if
    that does, else another document that does, else prism.json's main."""
    def is_doc(r: str) -> bool:
        try:
            return r.endswith(".tex") and build.is_document((ROOT / r).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return False
    if is_doc(rel):
        return rel
    firsts = [m for m in dict.fromkeys((cfg.main, cfg.project_main)) if (ROOT / m).is_file()]
    others = [f for f in list_files() if f.endswith(".tex") and f not in firsts]
    for m in firsts + [f for f in others if is_doc(f)]:
        if rel in input_chain(m):
            return m
    return cfg.project_main


def show_last_built() -> None:
    """Show the PDF of the document built last (its PDF is the newest), not always the
    project's main, so a reopened editor shows the PDF it showed before."""
    global CFG
    best = None
    for rel in list_files(editable_only=False):
        src = ROOT / rel
        if src.suffix != ".tex" or rel.split("/")[0] == CFG.outdir:
            continue
        pdf = ROOT / CFG.outdir / f"{src.stem}.pdf"
        try:
            if not pdf.exists() or not build.is_document(src.read_text(encoding="utf-8", errors="replace")):
                continue
        except OSError:
            continue
        best = max(best or (0.0, ""), (mtime(pdf), rel))
    if best and best[1] != CFG.main:
        CFG = Config(ROOT, main=best[1])


def run_build(mode: str, clean: bool = False, active: str | None = None) -> dict:
    """One build (see build.py). Only one runs at a time."""
    global CFG
    shown = CFG
    if mode not in shown.modes:
        return {"busy": False, "exit": 127, "mode": mode, "seconds": 0, "diagnostics": [],
                "output": f"There is no '{mode}' build.", "main": shown.main, "pdf_mtime": None}
    if not BUILD_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        if active is None:
            # No open file says which (the agent's compile tool, auto-compile after its turn):
            # the document edited since its PDF was built, else the document shown now.
            main = stale_document() or (shown.main if (ROOT / shown.main).is_file() else shown.project_main)
        else:
            if not isinstance(active, str):
                raise ValueError("active must be a project file path")
            source = resolve(active)
            if not source.is_file():
                raise FileNotFoundError(active)
            main = document_of(source.relative_to(ROOT).as_posix(), shown)
        # Select outputs in memory. Opening a standalone document does not change prism.json.
        # It is shown once its build has ended: until then the PDF pane keeps what it shows.
        cfg = Config(ROOT, main=main)
        t0 = time.time()
        before = mtime(cfg.pdf) if cfg.pdf.exists() else None
        RUNNING["pdf"] = (cfg.pdf, before)
        cmd = cfg.custom.get(mode)
        b = build.Build(ROOT, cfg.main, cfg.outdir, mode, builder=cfg.builder, engine=cfg.engine,
                        command=cfg.expand(cmd) if cmd else None, clean=clean,
                        shell_escape=cfg.shell_escape)
        RUNNING["build"] = b
        try:
            r = b.run()
        finally:
            RUNNING["pdf"] = None
            CFG = cfg
            AGENT.server_wrote([cfg.outdir])    # a build's output is no agent turn's change
        if cfg.error:
            r["output"] = f"prism-local: {cfg.error}\n" + r["output"]
        pdf = mtime(cfg.pdf) if cfg.pdf.exists() else None
        return {**r, "busy": False, "mode": mode, "seconds": round(time.time() - t0, 1),
                "main": cfg.main, "pdf_mtime": pdf, "pdf_updated": pdf is not None and pdf != before}
    finally:
        RUNNING["build"] = RUNNING["pdf"] = None
        BUILD_LOCK.release()


def stop_build() -> bool:
    b = RUNNING["build"]
    if b is not None:
        b.stop()
    return b is not None


# ---------------------------------------------------------------- synctex

SYNC = synctex.SyncIndex()           # the shown document's, read once until it changes
AGENT = AgentManager(lambda: ROOT, lambda: list_files(), resolve, output_fn=lambda: CFG.outdir)
# The project's changes, recorded in its repository and synced with GitHub (gitsync.py).
GITSYNC = GitSync(lambda: ROOT, lambda: CFG.outdir, busy=lambda: AGENT.busy(), writes=SAVE_LOCK)
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
            _NAME["name"] = registry.display_name(ROOT)
        except (OSError, ValueError):
            _NAME["name"] = ROOT.name
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
        refresh_config()
        return self._get(path, q)      # errors: httpbase._answer

    def _get(self, path, q):
        if path == "/api/ping":
            return self._json({"app": "prism-local", "root": str(ROOT), "project_key": registry.project_key(ROOT), "pid": os.getpid(),
                               "pages": PRESENCE.count(), "token": LIFETIME.token if LIFETIME else None})
        if path == "/api/pdfstat":
            cfg = CFG
            return self._json({"main": cfg.main, "mtime": pdf_state(cfg)})
        if path == "/pdf":
            cfg = CFG
            if q.get("main") and q["main"] != cfg.main:
                return self._err(409, "the compiled document changed")
            if RUNNING["pdf"] and RUNNING["pdf"][0] == cfg.pdf:
                return self._err(503, "the PDF is being built; it is served when the build ends")
            if not cfg.pdf.exists():
                return self._err(404, "no PDF yet")
            return self._send(200, cfg.pdf.read_bytes(), "application/pdf")
        if path == "/api/tree":
            st = git_status()
            walked: list[str] = []
            files, (listed, editable) = [], list_both(walked)
            folders = [f for f in walked if not _excluded(f + "/")]
            for f in listed:
                try:
                    files.append({"path": f, "git": st.get(f, ""), "mtime": mtime(ROOT / f),
                                  "editable": Path(f).suffix in EDITABLE_SUFFIXES})
                except FileNotFoundError:       # deleted since it was listed (a build, git)
                    pass
            return self._json({"root": ROOT.name, "name": project_name(), "path": str(ROOT),
                               "move_error": MOVE["error"], "files": files, "folders": folders,
                               "order": document_order(editable),
                               "pdf_main": (cfg := CFG).main, "pdf_mtime": pdf_state(cfg),
                               "updated": httpbase.code_stamp() > STARTED_CODE + 1, "server": STARTED_AT,
                               "sync": GITSYNC.status()})
        if path == "/api/git/publish":
            return self._json(publish.publish_info(ROOT))
        if path == "/api/git/theirs":            # GitHub's version, after a clash
            resolve(q["path"])
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
            cfg = CFG
            return self._json({"main": cfg.project_main, "outdir": cfg.outdir, "modes": cfg.modes,
                               "builder": cfg.builder, "engine": cfg.engine, "error": cfg.error,
                               "build": {m: cfg.describe(m) for m in cfg.modes}})
        if path == "/api/agent/events":
            if q.get("project_key") != registry.project_key(ROOT):
                return self._err(409, "This page belongs to another project. Reopen the project's editor.")
            got = AGENT.events(int(q["job"]), int(q.get("after", 0)), 20.0)
            if got is None:
                return self._err(404, "unknown job")
            return self._json({"events": got[0], "done": got[1]})
        if path == "/api/agent/commands":
            r = AGENT.commands(q.get("provider") or None, refresh=q.get("refresh") == "1")
            return self._json(r, 502 if "error" in r else 200)
        if path == "/api/agent/account":
            return self._json(AGENT.account(q.get("provider") or None, ROOT, fresh=q.get("fresh") == "1"))
        if path == "/api/agent/info":
            return self._json(AGENT.info())
        if path == "/api/symbols":
            return self._json(symbols())
        if path == "/api/snippets":
            f = snippets_file()
            return self._json({"path": str(f), "content": f.read_text(encoding="utf-8") if f.is_file() else None})
        if path == "/api/file":
            p = resolve(q.get("path", ""))
            return self._json({"path": q["path"], "content": p.read_text(encoding="utf-8"),
                               "mtime": mtime(p)})
        if path == "/api/asset":
            from urllib.parse import quote
            p = resolve(q.get("path", ""), editable_only=False)
            ctype = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(p.suffix.lower())
            extra = {} if ctype else {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(p.name)}
            return self._send(200, p.read_bytes(), ctype or "application/octet-stream", extra)
        if path == "/api/diff":
            if q.get("path"):
                resolve(q["path"])
            return self._json(project_diff(q.get("path") or "."))
        if path == "/api/upload/sync":             # how the uploads' commits and pushes went
            return self._json({p: UPLOAD_SYNC.get(p) for p in q.get("paths", "").split("\n") if p})
        if path == "/api/synctex/forward":
            try:
                records = SYNC.lookup(CFG.synctex, ROOT)
            except synctex.NoData as e:
                return self._err(404, str(e))
            r = records.forward(q["file"], int(q["line"]))
            return self._json(r or {"error": "no match"}, 200 if r else 404)
        if path == "/api/synctex/inverse":
            try:
                records = SYNC.lookup(CFG.synctex, ROOT)
            except synctex.NoData as e:
                return self._err(404, str(e))
            r = records.inverse(int(q["page"]), float(q["x"]), float(q["y"]), CFG.outdir,
                                lambda: [f for f in list_files() if f.endswith(".bib")])
            return self._json(r or {"error": "no match"}, 200 if r else 404)
        return self._err(404, "not found")

    def _files(self, path, body) -> dict:
        """The Files sidebar's menu (see item_path)."""
        rels = body.get("paths")
        if rels is not None and (not isinstance(rels, list) or not all(isinstance(r, str) for r in rels)):
            raise ValueError("paths must be a list of project paths")
        if path == "/api/files/reveal":
            rel = str(body.get("path") or "")
            p = item_path(rel)
            if p == ROOT:
                desktop.open_folder(p)
            else:
                desktop.reveal(p)
            return {"ok": True}
        if path == "/api/files/copy":
            try:
                desktop.copy_files([item_path(r) for r in rels or []])
            except desktop.Unsupported as e:   # the page keeps them: paste works in this project
                return {"system": False, "message": str(e)}
            return {"system": True}
        if path == "/api/files/paste":
            # The page's own copy where the system has no file clipboard, else the clipboard.
            sources = [item_path(r) for r in rels] if rels else desktop.clipboard_files()
            if not sources:
                raise ValueError("the clipboard holds no files; copy some in the editor, Explorer or Finder")
            return paste_files(str(body.get("folder") or ""), sources)
        if path == "/api/files/rename":
            return rename_item(str(body["path"]), str(body.get("name") or ""))
        if path == "/api/files/new":
            return new_item(str(body.get("folder") or ""), str(body.get("name") or ""),
                            str(body.get("kind") or "file"))
        if path == "/api/files/trash":
            return trash_items(rels or [])
        raise NotFound("unknown file action")

    def post(self, path, body):
        refresh_config()
        return self._post(path, body)      # errors: httpbase._answer

    def _post(self, path, body):
        if path.startswith(("/api/agent", "/api/files/")) and body.get("project_key") != registry.project_key(ROOT):
            return self._err(409, "This page belongs to another project. Reopen the project's editor.")
        if path.startswith("/api/files/"):
            try:
                return self._json(self._files(path, body))
            except desktop.Unsupported as e:
                raise Refused(str(e)) from e
        if path == "/api/git/publish":
            if BUILD_LOCK.locked() or AGENT.busy():
                return self._err(409, "wait until the build or the agent's turn has finished")
            with GITSYNC.lock:
                r = publish.publish_project(ROOT, str(body.get("name") or ""), str(body.get("owner") or ""),
                                        list(body.get("leave_out") or []), files_lock=SAVE_LOCK)
            GITSYNC.recheck()                   # a repository exists now
            return self._json({**r, "sync": GITSYNC.status()}, 502 if "error" in r else 200)
        if path == "/api/git/recheck":
            GITSYNC.recheck()
            return self._json(GITSYNC.status())
        if path == "/api/project/rename":
            return self._json(*rename_project(str(body["name"] or "").strip()))
        if path == "/api/snippets":
            f = snippets_file()         # every project's server reads it: never half-written
            write_bytes(f, with_line_ends_of(str(body.get("content") or ""), f).encode("utf-8"))
            return self._json({"path": str(f)})
        if path == "/api/file":
            r, code = save_file(body["path"], str(body["content"]), body.get("base_mtime"),
                                bool(body.get("force")))
            if code == 200:
                GITSYNC.touched()       # an autosave commit follows once you stop editing
            return self._json(r, code)
        if path == "/api/git/sync":
            action = str(body.get("action") or "")
            if action == "bind":
                if not isinstance(body.get("target"), dict):
                    return self._err(400, "Open the Git menu and confirm the displayed target")
                GITSYNC.bind_target(body["target"])
            elif action in ("on", "off"):
                GITSYNC.set_enabled(action == "on")
            elif action in ("commit", "pull", "resolve"):
                if action == "resolve" and (BUILD_LOCK.locked() or AGENT.busy()):
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
                    resolve(f)          # an editable project file, or ValueError
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
            r = run_build(str(body.get("mode", "draft")), bool(body.get("clean")), body.get("active"))
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
            rel = save_upload(str(body.get("name") or "file"), data,
                              folder="" if destination == "project" else UPLOAD_DIR)
            if destination == "project":
                GITSYNC.touched()
                return self._json({"path": rel, "size": len(data)})
            UPLOAD_SYNC[rel] = {"state": "syncing", "message": "Committing and pushing…"}
            threading.Thread(target=sync_upload, args=(rel,), daemon=True).start()
            return self._json({"path": rel, "size": len(data), "sync": UPLOAD_SYNC[rel]})
        if path == "/api/restart":
            if BUILD_LOCK.locked() or AGENT.busy() or RESTART["server"] is None:
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
            return self._json({"ok": stop_build()})
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
    show_last_built()
    if a.idle_timings:
        f, g, st = (float(x) for x in a.idle_timings.split(","))
        PRESENCE.first_wait, PRESENCE.grace, PRESENCE.stale = f, g, st
    if not (ROOT / CFG.main).is_file():
        print(f"prism-local: warning: main file {CFG.main} not found in {ROOT}", file=sys.stderr)
    try:
        srv = httpbase.listen(Handler, a.port, a.port_tries)
    except OSError as e:
        sys.exit(f"prism-local: {e}")
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    AGENT.server_url = url              # the agent's compile tool builds through this server
    stop = "closes after the last page" if a.exit_when_idle else "Ctrl-C to stop"
    print(f"prism-local: {url}\n  project: {ROOT}\n  main:    {CFG.main}\n"
          f"  builds:  {', '.join(CFG.modes)}\n  {stop}", flush=True)
    if CFG.error:
        print(f"  warning: {CFG.error}", flush=True)
    if not is_scratch(ROOT):
        registry.safe_touch(ROOT)       # list the project on the Home page
    life.publish(url, port)
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if a.exit_when_idle:
        busy = lambda: BUILD_LOCK.locked() or AGENT.busy()  # noqa: E731
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
            stop_build()
            AGENT.shutdown()
            GITSYNC.flush()             # what is left is committed and pushed
            if RESTART["again"]:
                destination = RESTART["move_to"] or ROOT
                argv = move_argv(destination, moving=RESTART["move_to"] is not None)
                result = transition.handover(argv, str(ROOT.parent))
                httpbase.log("restart failed: " + result["error"] if "error" in result
                             else "restart handed to the successor")
            else:
                httpbase.log("stopped")
        finally:
            transition.close()


if __name__ == "__main__":
    sys.exit(main())
