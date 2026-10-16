"""The project's files around an agent turn: what was there before it and after it, what
it changed, and putting back what it wrote.

    before, issues = take(root)             # every file: its bytes, mode, link
    ... the turn ...
    after, issues = take(root, since=before)    # what did not change is not read again
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
import time
from dataclasses import dataclass, field
from pathlib import Path

from fsutil import EDITABLE_SUFFIXES, project_path, write_bytes

MAX_FILE = 25 * 1024 * 1024
MAX_BYTES = 100 * 1024 * 1024
# A file changed this close to the snapshot (seconds) may change again within its clock's
# step without its stat showing it ("racy git"): it is read again, whatever its stat says.
RACY = 2.0
# Beyond these a file's change is described, not diffed (difflib is slow on big files, and
# the page shows the diff of every changed file), and no diff is longer than DIFF_MAX.
DIFF_BYTES, DIFF_LINES, DIFF_MAX = 1024 * 1024, 20000, 200_000


@dataclass(frozen=True)
class FileState:
    data: bytes | None = field(compare=False)     # None: too big to keep (digest only)
    mode: int
    link: str | None = None
    digest: str | None = None
    seen: tuple | None = field(compare=False, default=None)   # its lstat then, and when (take's `since`)


def _seen(info: os.stat_result) -> tuple:
    return info.st_size, info.st_mtime_ns, info.st_ino, info.st_mode


def file_state(path: Path, budget: int = MAX_FILE, info: os.stat_result | None = None) -> FileState:
    """What is at `path`: a regular file or a link (OSError otherwise). `info`: its lstat,
    when the caller has it."""
    at = time.time_ns()
    info = info or path.lstat()
    seen = (*_seen(info), at)
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return FileState(b"", mode, link=os.readlink(path), seen=seen)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("not a regular file")
    if info.st_size <= min(MAX_FILE, budget):
        data = path.read_bytes()
        return FileState(data, mode, digest=hashlib.sha256(data).hexdigest(), seen=seen)
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return FileState(None, mode, digest=digest.hexdigest(), seen=seen)


def state_of(path: Path) -> FileState | None:
    """file_state(), or None when there is no file (gone, or a folder)."""
    try:
        return file_state(path)
    except OSError:
        return None


def take(root: Path, skip: str | None = None, since: dict | None = None) -> tuple[dict[str, FileState], set[str]]:
    """Every file under `root` (project-relative path -> state), and what could not be
    looked at. Binary files, hidden files and excluded folders too: the editor's file list
    is not the list of what may change. Never follows a folder link or enters .git, nor
    the folder `skip` (project-relative: the build's output, which a build may be writing).

    `since`: an earlier take (the turn's start). A file whose lstat is what it was then is
    not read again: its state then is its state now (a project with gigabytes of data is
    then read once a turn, not twice); one changed within RACY seconds before it was looked
    at then is read again.
    The MAX_BYTES kept for Undo go to the sources first (EDITABLE_SUFFIXES), then to the
    smallest files: a big data folder walked first no longer leaves a 2 KB .tex undoable."""
    snap: dict[str, FileState] = {}
    issues: set[str] = set()
    skip = Path(skip).as_posix() if skip and Path(skip).parts else None    # "." is the project
    found: list[tuple[str, Path, os.stat_result]] = []
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
                found.append((rel, path, path.lstat()))
            except (OSError, ValueError, RuntimeError) as exc:
                issues.add(f"{rel}: {exc}")
    budget = MAX_BYTES
    for rel, path, info in sorted(found, key=lambda f: (Path(f[0]).suffix not in EDITABLE_SUFFIXES, f[2].st_size)):
        old = since.get(rel) if since else None
        if old is not None and old.seen and old.seen[:4] == _seen(info) \
                and info.st_mtime_ns < old.seen[4] - int(RACY * 1e9) \
                and (old.data is not None or info.st_size > min(MAX_FILE, budget)):
            snap[rel] = old
            budget -= len(old.data or b"")
            continue
        try:
            state = file_state(path, budget, info)
            snap[rel] = state
            budget -= len(state.data or b"")
        except (OSError, ValueError, RuntimeError) as exc:
            issues.add(f"{rel}: {exc}")
    return dict(sorted(snap.items())), issues


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
    a_lines, b_lines = text(ad).splitlines(keepends=True), text(bd).splitlines(keepends=True)
    if max(len(ad), len(bd)) > DIFF_BYTES or max(len(a_lines), len(b_lines)) > DIFF_LINES:
        return (f"Large file changed ({len(ad)} -> {len(bd)} bytes, {len(a_lines)} -> {len(b_lines)} "
                "lines): too big to show here; Undo works as for any file.\n")
    out = "".join(difflib.unified_diff(
        a_lines, b_lines,
        fromfile=f"a/{rel}" if a is not None else "/dev/null",
        tofile=f"b/{rel}" if b is not None else "/dev/null", n=2))
    if len(out) > DIFF_MAX:
        out = out[:DIFF_MAX] + "\n… (the rest of this diff is not shown)\n"
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
