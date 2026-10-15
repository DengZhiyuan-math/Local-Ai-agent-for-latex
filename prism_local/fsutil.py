"""Project files: which ones the editor shows, and writing them safely on every system."""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from pathlib import Path

EDITABLE_SUFFIXES = {".tex", ".bib", ".md", ".sty", ".cls", ".bbx", ".cbx", ".txt", ".tikz"}
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv"}
TEMP_SUFFIX = ".prism-tmp"      # a save half-way (replace_file): never a project's file
METADATA = {".git", ".codex", ".agents", ".aws"}   # repository and agent metadata
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
                    *(f"LPT{i}" for i in range(10))}


def bad_name(name: str) -> str | None:
    """Why `name` cannot name a project's file or folder on every system, or None."""
    if not name.strip() or name in (".", ".."):
        return "give a name"
    if re.search(r'[\x00-\x1f<>:"/\\|?*]', name):
        return 'a name cannot contain / \\ : * ? " < > |'
    if name != name.strip() or name.endswith("."):
        return "a name cannot start or end with a space, or end with a dot"
    if name.startswith("."):
        return "a name starting with a dot would be hidden in the editor"
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        return f"{name} is a reserved name on Windows"
    if len(name) > 200:
        return "the name is too long"
    return None


def is_metadata(path: Path) -> bool:
    # Case-insensitive everywhere: Windows and macOS file systems usually are.
    return any(part.casefold() in METADATA for part in path.parts)


def metadata_paths(root: Path, onerror=None):
    """The metadata folders and files at any depth below the resolved `root`, not entered,
    without following folder links or Windows junctions. A folder removed meanwhile is
    skipped; `onerror(error)` gets any other folder that could not be looked at."""
    def unreadable(error):
        if onerror and not isinstance(error, FileNotFoundError):
            onerror(error)

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        parent = Path(directory)
        for name in sorted(dirs + files):
            if is_metadata(Path(name)):
                yield parent / name
        # resolve also detects Windows junctions on Python versions before is_junction.
        dirs[:] = [name for name in dirs if not is_metadata(Path(name))
                   and (parent / name).resolve() == parent / name]


def project_path(root: Path, rel: str) -> Path:
    """Resolve an in-project path. Internal links are allowed; .git is off limits."""
    rel = str(rel).replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"[A-Za-z]:", rel):
        raise ValueError("give a path relative to the project root")
    root = root.resolve()
    p = (root / rel).resolve()
    if root not in p.parents:
        raise ValueError("the path is outside the project")
    # Case-insensitive: on macOS (as on Windows) .GIT names the same directory.
    if any(part.casefold() == ".git" for part in (*Path(rel).parts, *p.relative_to(root).parts)):
        raise ValueError("the .git directory is off limits")
    return p


def uses_crlf(data: bytes) -> bool:
    """Whether most line ends in `data` are Windows ones (\\r\\n)."""
    crlf = data.count(b"\r\n")
    return crlf > 0 and crlf * 2 >= data.count(b"\n")


def with_line_ends_of(text: str, path: Path) -> str:
    """`text` (with \\n line ends, as an editor or a model writes it) with the line ends the
    file at `path` already uses, so a save never turns a whole file into a diff."""
    text = text.replace("\r\n", "\n")
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return text
    return text.replace("\n", "\r\n") if uses_crlf(head) else text


def replace_file(path: Path, data: bytes, tries: int = 40) -> None:
    """Write a file so that a reader never sees half of it: the data goes to a temporary
    file beside it, which then replaces `path`. Windows refuses the replace while another
    process has the file open (a server reading it, a TeX engine during a build), so it is
    tried every 0.05 seconds, `tries` times in all, before PermissionError is raised."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}{TEMP_SUFFIX}")
    try:
        tmp.write_bytes(data)
        try:
            shutil.copymode(path, tmp)      # the new file keeps the old one's permissions
        except OSError:
            pass                            # a new file: the default permissions
        for attempt in range(tries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == tries - 1:
                    raise
                time.sleep(0.05)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def write_bytes(path: Path, data: bytes) -> None:
    """Write a project file so that a reader never sees half of it, where the system allows.

    On Windows replace_file fails while another program has the file open, for instance a
    TeX engine reading it during a build; after retrying for about a second the file is
    written in place, which such programs allow. Should that write fail half-way (a full
    disk), the old contents are put back and the error is raised: a file is never left cut
    short without a word."""
    try:
        replace_file(path, data, tries=20)
        return
    except PermissionError:
        pass
    try:
        old = path.read_bytes()
    except FileNotFoundError:
        old = None
    with open(path, "wb" if old is None else "r+b") as f:
        try:
            f.write(data)
            f.truncate()
            f.flush()
        except OSError as e:
            try:
                f.seek(0)
                f.write(old or b"")
                f.truncate()
                f.flush()
            except OSError:
                raise OSError(f"{path.name} was written only in part ({e}); its earlier "
                              "version could not be put back") from e
            raise
