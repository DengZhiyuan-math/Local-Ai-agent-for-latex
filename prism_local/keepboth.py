"""Combine two versions of a text file, keeping both where they clash.

When two co-authors change the same lines at the same time, git cannot know which version
is right. prism-local does not stop syncing over it, and does not drop either version: the
file gets both, one after the other, so nobody's text disappears from the paper. In TeX
files comment lines mark the place and say whose version is whose:

    % [prism-local] Alice and Bob changed these lines at the same time: both versions are kept. ...
    % [prism-local] Bob's version (from GitHub):
    ...Bob's lines...
    % [prism-local] Alice's version:
    ...Alice's lines...
    % [prism-local] end

Other text files (a .bib, where % is not a comment inside an entry) get both versions
without the marker lines. The three-way merge itself is git's (`git merge-file`), with
markers 80 characters long that no line of a paper starts with.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from proc import NO_WINDOW

MARK = "[prism-local]"
TEX_LIKE = (".tex", ".sty", ".cls", ".ltx", ".dtx", ".bbx", ".cbx", ".tikz", ".def", ".cfg", ".clo")
SIZE = 80


class MergeError(Exception):
    pass


def merge_file(ours: str, base: str, theirs: str) -> str:
    """git merge-file's result, with conflict markers SIZE characters long."""
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = Path(d) / name
            p.write_bytes(text.encode("utf-8"))
            paths.append(str(p))
        r = subprocess.run(["git", "merge-file", "-p", f"--marker-size={SIZE}",
                            "-L", "ours", "-L", "base", "-L", "theirs", *paths],
                           capture_output=True, timeout=60, **NO_WINDOW)
    if r.returncode < 0 or r.returncode >= 127:
        raise MergeError(r.stderr.decode("utf-8", "replace").strip() or "git merge-file failed")
    return r.stdout.decode("utf-8")


def keep_both(ours: str, base: str, theirs: str, mine: str, them: str, tex: bool) -> tuple[str, list[int]]:
    """The three-way merge of ours and theirs, with both versions where they clash.
    Returns the text (\\n line ends) and the line numbers (1-based) where each clash begins."""
    merged = merge_file(ours.replace("\r\n", "\n"), base.replace("\r\n", "\n"), theirs.replace("\r\n", "\n"))
    left, mid, right = "<" * SIZE, "=" * SIZE, ">" * SIZE
    out: list[str] = []
    places: list[int] = []
    state, mine_lines, their_lines = None, [], []

    def block(lines):
        return [ln if ln.endswith("\n") else ln + "\n" for ln in lines]

    for line in merged.splitlines(keepends=True):
        bare = line.rstrip("\n")
        if state is None and bare.startswith(left):
            state, mine_lines, their_lines = "ours", [], []
        elif state == "ours" and bare == mid:
            state = "theirs"
        elif state == "theirs" and bare.startswith(right):
            places.append(len(out) + 1)
            if tex:
                out += [f"% {MARK} {mine} and {them} changed these lines at the same time: both versions "
                        f"are kept. Keep what you want, then delete the {MARK} lines.\n",
                        f"% {MARK} {them}'s version (from GitHub):\n", *block(their_lines),
                        f"% {MARK} {mine}'s version:\n", *block(mine_lines),
                        f"% {MARK} end\n"]
            else:
                out += block(their_lines) + block(mine_lines)
            state = None
        elif state == "ours":
            mine_lines.append(line)
        elif state == "theirs":
            their_lines.append(line)
        else:
            out.append(line)
    if state is not None:
        raise MergeError("unfinished conflict in git merge-file's output")
    return "".join(out), places


# A clash this big (an agent or a person rewrote the whole file, reformatted it) would put
# most of the paper in twice: the file keeps this side's version instead, and the other
# side's whole version is saved next to it (gitsync.py).
BIG_LINES, BIG_SHARE = 60, 0.4


def clash_lines(ours: str, base: str, theirs: str) -> tuple[int, int]:
    """How many lines the clashes span (both sides), and how many lines the file has."""
    merged = merge_file(ours.replace("\r\n", "\n"), base.replace("\r\n", "\n"), theirs.replace("\r\n", "\n"))
    left, right = "<" * SIZE, ">" * SIZE
    n, inside = 0, False
    for line in merged.splitlines():
        if not inside and line.startswith(left):
            inside = True
        elif inside and line.startswith(right):
            inside = False
        elif inside and line != "=" * SIZE:
            n += 1
    return n, max(len(ours.splitlines()), len(theirs.splitlines()), 1)


def too_big(ours: str, base: str, theirs: str) -> bool:
    n, total = clash_lines(ours, base, theirs)
    # Both: many lines, and most of the file. A short file (a few-line .bib) always gets both
    # versions inside, however much of it clashes.
    return n > BIG_LINES and n > BIG_SHARE * 2 * total


def is_tex(path: str) -> bool:
    return path.lower().endswith(TEX_LIKE)


def marked_lines(text: str) -> list[int]:
    """Where the marker blocks still are (the first line of each, 1-based)."""
    return [i + 1 for i, ln in enumerate(text.splitlines())
            if ln.startswith(f"% {MARK}") and "changed these lines at the same time" in ln]
