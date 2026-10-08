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


def project_path(root: Path, rel: str) -> Path:
    """Resolve an in-project path. Internal links are allowed; .git is off limits."""
    rel = str(rel).replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"[A-Za-z]:", rel):
        raise ValueError("give a path relative to the project root")
    root = root.resolve()
    p = (root / rel).resolve()
    if root not in p.parents:
        raise ValueError("the path is outside the project")
    if ".git" in Path(rel).parts or ".git" in p.relative_to(root).parts:
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


def write_bytes(path: Path, data: bytes) -> None:
    """Write a file so that a reader never sees half of it, where the system allows.

    The data goes to a temporary file that then replaces `path`. On Windows that replace
    fails while another program has the file open, for instance a TeX engine reading it
    during a build; after retrying for about a second the file is written in place, which
    such programs allow."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.prism-tmp")
    try:
        tmp.write_bytes(data)
        try:
            shutil.copymode(path, tmp)      # the new file keeps the old one's permissions
        except OSError:
            pass                            # a new file: the default permissions
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    break
                time.sleep(0.05)
        with open(path, "r+b" if path.exists() else "wb") as f:
            f.write(data)
            f.truncate()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
