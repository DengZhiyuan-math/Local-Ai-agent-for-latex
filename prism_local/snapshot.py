"""The project's files around an agent turn: what was there before it and after it, what
it changed, and putting back what it wrote.

    before, issues = take(root)             # every file: its bytes, mode, link
    ... the turn ...
    after, issues = take(root)
    changes(before, after)                  # [{"path", "diff", "created", "deleted", "undoable"}]
    restore(root, before, after, paths)     # back to `before`, where still as `after`

Files are kept as bytes: Undo puts back exactly what was there (line ends, encoding), and a
file that is not UTF-8 cannot stop a turn. A file too big to keep (MAX_FILE, or once
MAX_BYTES are kept) is known by its digest only: its change is seen, not undone.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from fsutil import project_path, write_bytes

MAX_FILE = 25 * 1024 * 1024
MAX_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class FileState:
    data: bytes | None = field(compare=False)     # None: too big to keep (digest only)
    mode: int
    link: str | None = None
    digest: str | None = None


def file_state(path: Path, budget: int = MAX_FILE) -> FileState:
    """What is at `path`: a regular file or a link (OSError otherwise)."""
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return FileState(b"", mode, link=os.readlink(path))
    if not stat.S_ISREG(info.st_mode):
        raise OSError("not a regular file")
    if info.st_size <= min(MAX_FILE, budget):
        data = path.read_bytes()
        return FileState(data, mode, digest=hashlib.sha256(data).hexdigest())
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return FileState(None, mode, digest=digest.hexdigest())


def state_of(path: Path) -> FileState | None:
    """file_state(), or None when there is no file (gone, or a folder)."""
    try:
        return file_state(path)
    except OSError:
        return None


def take(root: Path, skip: str | None = None) -> tuple[dict[str, FileState], set[str]]:
    """Every file under `root` (project-relative path -> state), and what could not be
    looked at. Binary files, hidden files and excluded folders too: the editor's file list
    is not the list of what may change. Never follows a folder link or enters .git, nor
    the folder `skip` (project-relative: the build's output, which a build may be writing)."""
    snap: dict[str, FileState] = {}
    issues: set[str] = set()
    budget = MAX_BYTES
    skip = Path(skip).as_posix() if skip and Path(skip).parts else None    # "." is the project
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=lambda e: issues.add(str(e))):
        parent = Path(directory)
        links = [d for d in dirs if (parent / d).is_symlink() and d != ".git"]
        dirs[:] = sorted(d for d in dirs if d != ".git" and d not in links
                         and (parent / d).relative_to(root).as_posix() != skip)
        for name in sorted(files + links):
            if name == ".git":
                continue
            path = parent / name
            rel = path.relative_to(root).as_posix()
            try:
                # A link's own metadata, never its target (outside, or broken); a regular
                # file passes the same path check as the editor's reads.
                if not path.is_symlink():
                    project_path(root, rel)
                state = file_state(path, budget)
                snap[rel] = state
                budget -= len(state.data or b"")
            except (OSError, ValueError, RuntimeError) as exc:
                issues.add(f"{rel}: {exc}")
    return snap, issues


def diff(rel: str, a: FileState | None, b: FileState | None) -> str:
    """How `rel` changed from `a` to `b` (None: absent), for the author to read."""
    if any(s and s.link is not None for s in (a, b)):
        return f"Link changed: {a.link if a else '(absent)'} -> {b.link if b else '(absent)'}\n"
    if any(s and s.data is None for s in (a, b)):
        return "File changed; content exceeds the Undo snapshot limit.\n"
    ad, bd = a.data if a else b"", b.data if b else b""
    try:
        for data in (ad, bd):
            data.decode("utf-8")
            if b"\x00" in data:
                raise UnicodeError
    except UnicodeError:
        return f"Binary file changed ({len(ad)} -> {len(bd)} bytes).\n"
    text = lambda d: d.decode("utf-8", "replace").replace("\r\n", "\n")  # noqa: E731
    out = "".join(difflib.unified_diff(
        text(ad).splitlines(keepends=True), text(bd).splitlines(keepends=True),
        fromfile=f"a/{rel}" if a is not None else "/dev/null",
        tofile=f"b/{rel}" if b is not None else "/dev/null", n=2))
    return out or f"(line ends, encoding or file permissions of {rel} changed)\n"


def changed_paths(before: dict, after: dict) -> list[str]:
    return [rel for rel in sorted(set(before) | set(after)) if before.get(rel) != after.get(rel)]


def changes(before: dict, after: dict) -> list[dict]:
    """What changed from `before` to `after`, file by file."""
    out = []
    for rel in changed_paths(before, after):
        a, b = before.get(rel), after.get(rel)
        out.append({"path": rel, "diff": diff(rel, a, b), "created": a is None, "deleted": b is None,
                    "undoable": not any(s and s.data is None for s in (a, b))})
    return out


def rebase(before: dict, written: dict, folders: set[str]) -> None:
    """Take what someone else wrote meanwhile (`written`: path -> state, None: gone; and
    everything in `folders`, also what is gone from them) as part of `before`, so that it is
    no change of the turn's."""
    for folder in folders:
        for rel in [r for r in before if r.startswith(folder) and r not in written]:
            before.pop(rel)
    for rel, state in written.items():
        if state is None:
            before.pop(rel, None)
        else:
            before[rel] = state


def restore(root: Path, before: dict, after: dict, rels) -> list[str]:
    """Put `rels` back as they were in `before`, where they still are as in `after` (never
    over what was written since). Returns those put back."""
    restored = []
    for rel in rels:
        a, b = before.get(rel), after.get(rel)
        p = root / rel
        try:
            # Do not follow a parent folder that was replaced by an external link.
            parent = p.parent.resolve()
            if parent != root and root not in parent.parents:
                continue
            if ".git" in parent.relative_to(root).parts:
                continue
            cur = file_state(p, MAX_BYTES) if p.exists() or p.is_symlink() else None
        except (OSError, ValueError, RuntimeError):
            continue                     # cannot tell what is there now; leave it
        if cur != b or any(s and s.data is None for s in (a, b)):
            continue                     # edited again since; never clobber
        try:
            if a is None:
                p.unlink()
            elif a.link is not None:
                if p.exists() or p.is_symlink():
                    p.unlink()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.symlink_to(a.link)
            else:
                if p.is_symlink():
                    p.unlink()
                write_bytes(p, a.data)
                p.chmod(a.mode)
        except OSError:
            continue                     # locked by another program: reported as not restored
        restored.append(rel)
    return restored
