"""Which files of a project are its sources: what is recorded in its repository (autosave,
an agent turn), what a new repository ignores, and how to read `git status`.

LaTeX writes its auxiliary files into the output folder, or next to the sources when a
project has none: those are never sources. The list is the one the builder knows (build.py:
the files an engine reads back, and those it writes and never reads), less the extensions a
source may have too (a figure .pdf or .png, a .json of data).
"""
from __future__ import annotations

from pathlib import Path

import build
from fsutil import TEMP_SUFFIX as TEMP

MAX_FILE = 50 * 1024 * 1024    # GitHub refuses files over 100 MB; keep well below
_MAY_BE_SOURCES = {".pdf", ".png", ".jpg", ".jpeg", ".eps", ".json", ".xml", ".gz", ".tmp", TEMP}
# LaTeX's auxiliary files, by the end of their name (".synctex.gz", ".run.xml": two parts).
AUX: tuple[str, ...] = tuple(sorted((build.FEEDBACK | build.NOT_FEEDBACK | build.CLEAN) - _MAY_BE_SOURCES
                                    | {".synctex.gz", ".run.xml"}))
NEVER = AUX + (TEMP,)          # new files never recorded


def recordable(path: str, untracked: bool, top: Path, outdir: str) -> bool:
    """Whether a change to `path` (from the repository's top) is worth recording: nothing
    in the output folder (`outdir`, from the top too) or .git, no new auxiliary or
    temporary file, nothing GitHub would refuse."""
    if path.startswith((outdir.rstrip("/") + "/", ".git/")):
        return False
    if untracked and path.lower().endswith(NEVER):
        return False
    p = top / path
    try:
        return not (p.is_file() and p.stat().st_size > MAX_FILE)
    except OSError:
        return False


def recordable_changes(out: str, top: Path, outdir: str) -> list[str]:
    """The paths in `git status --porcelain -z` output (-b's "## branch" line left out)
    that autosave would record (recordable): what "changed" means everywhere, sorted."""
    return sorted({path for xy, path in parse_status(out)
                   if xy != "##" and recordable(path, xy == "??", top, outdir)})


def gitignore(outdir: str = "build", shared: bool = False) -> list[str]:
    """The lines of a new repository's .gitignore: build output, LaTeX's auxiliary files,
    a save half-way, macOS's folder files; in a repository shared by several projects also
    the .git a project set aside when it joined (sharedrepo.py)."""
    return [f"{outdir.strip('/') or 'build'}/", *([".git-prism-*/"] if shared else []), f"*{TEMP}",
            ".DS_Store", *(f"*{ext}" for ext in AUX)]


def parse_status(out: str) -> list[tuple[str, str]]:
    """`git status --porcelain -z` as (XY, path) pairs, paths from the repository's top. A
    rename or a copy gives its old path too, as a pair of its own: both changed."""
    entries, pairs, i = out.split("\0"), [], 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        xy, path = e[:2], e[3:]
        if "R" in xy or "C" in xy:            # the old path follows
            old = entries[i] if i < len(entries) else ""
            i += 1
            if old:
                pairs.append((xy, old))
        pairs.append((xy, path))
    return pairs
