"""What git says of a project's folder: where it is in git (place) and what changed in it
(changes), for the editor's tree, the Home page's cards and the sync's commits alike.

Both are asked often (every open page asks for the tree every two seconds, the Home page
for its cards whenever you come back to it) and are the same most of those times: each is
looked up again only when what it depends on changed. A place, when its repository's git
dir went or a .git appeared between the folder and its top; what changed, when the refs,
the index or the configuration did (refs_stamp), or the files as the caller sees them
(`seen`: their mtimes, a walk's stamp). git is the one that knows, each costs a process
(some 20 ms on Windows, and the antivirus's look), and a project's files change far less
often than they are asked about.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import sources
from repository import Place, locate, refs_stamp

_PLACES: dict[str, Place] = {}       # project folder -> its Place, while its git dir is there
_CHANGES: dict[str, tuple] = {}      # project folder -> (what they depend on, its Changes)


def place(root: Path, timeout: float = 8) -> Place:
    """locate(root), looked up again only when its repository's git dir went (the project
    joined a shared repository, or left one), it had none (one may have been made: with no
    .git above it, that look runs no git), or a .git appeared between it and the top of the
    repository it is in (git finds that one)."""
    hit = _PLACES.get(str(root))
    if hit is not None and hit.repo is not None and hit.repo.git_dir.is_dir() \
            and not (hit.prefix and _git_below(root, hit.repo.top)):
        return hit
    found = locate(root, timeout=timeout)
    _PLACES[str(root)] = found
    return found


def _git_below(folder: Path, top: Path) -> bool:
    """A .git in `folder` or a folder above it, below `top`."""
    p, top = Path(folder).resolve(), top.resolve()
    while p != top and p != p.parent:
        if (p / ".git").exists():
            return True
        p = p.parent
    return False


@dataclass(frozen=True)
class Changes:
    """One `git status` of a project: its branch line, and its changed paths (from the
    repository's top, the project's only) with their XY codes."""
    place: Place
    head: str                                # "main...origin/main [ahead 1]"; "" without -b's line
    entries: tuple[tuple[str, str], ...]     # (XY, path from the top); a rename gives both paths

    @property
    def branch(self) -> str:
        head = re.sub(r"^(No commits yet on|Initial commit on) ", "", self.head)
        return re.split(r"\.\.\.| ", head)[0] if head else ""

    @property
    def ahead(self) -> int:
        m = re.search(r"ahead (\d+)", self.head)
        return int(m.group(1)) if m else 0

    @property
    def behind(self) -> int:
        m = re.search(r"behind (\d+)", self.head)
        return int(m.group(1)) if m else 0

    def files(self) -> dict[str, str]:
        """{project-relative path: status} of every path git reports, as the tree marks them."""
        prefix = self.place.prefix
        return {path[len(prefix):]: xy.strip() or "M" for xy, path in self.entries
                if path.startswith(prefix)}

    def recordable(self, outdir: str) -> list[str]:
        """The paths (from the top) autosave would record (sources.recordable): no build
        output (`outdir`, from the project), no new auxiliary file, nothing too big. What
        "changed" means everywhere, sorted."""
        top = self.place.repo.top
        out = self.place.prefix + outdir.strip("/")
        return sorted({path for xy, path in self.entries if sources.recordable(path, xy == "??", top, out)})


def status(at: Place, timeout: float = 30) -> Changes:
    """git status of the project at `at`, now: its own files only (a shared repository's
    other projects, an enclosing repository's other files are not), through its Repository
    (the sync's holds the editor's save lock around it). RuntimeError when git fails;
    OSError or subprocess's errors when it cannot run or times out."""
    if at.repo is None:
        raise RuntimeError("This project has no repository")
    spec = f":(top,literal){at.prefix.rstrip('/')}" if at.prefix else "."
    r = at.repo.git("status", "--porcelain", "-z", "-b", "-uall", "--", spec, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-400:] or "git status failed")
    head, entries = "", []
    for xy, path in sources.parse_status(r.stdout):
        if xy == "##":
            head = path
        else:
            entries.append((xy, path))
    return Changes(at, head, tuple(entries))


def changes(root: Path, seen=None, timeout: float = 10) -> Changes | None:
    """What changed in the project at `root` (status); None when it is in no repository or
    git failed. With `seen` (what the caller knows of the files: their mtimes, a walk's
    stamp; hashable), the last answer is given again while neither it nor the repository's
    refs, index and configuration changed. Without, git is asked."""
    at = place(root)
    if at.repo is None:
        return None
    stamp = refs_stamp(at.repo.git_dir) if seen is not None else None
    key = None if stamp is None else (str(at.repo.git_dir), at.prefix, stamp, seen)
    hit = _CHANGES.get(str(root))
    if key is not None and hit is not None and hit[0] == key:
        return hit[1]
    try:
        found = status(at, timeout=timeout)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return None
    if key is not None:
        _CHANGES[str(root)] = (key, found)
    return found
