"""Bringing GitHub's commits into this copy, losing nothing.

    result = integrate.merge(repo, tracking_ref, n, prefix)     # inside repo.changing()
    gitsync applies result.notice, .error, .clash and .kept to what the pages show

A merge that both sides changed the same lines in keeps both versions (keepboth.py); one
that cannot be done is called off, so this copy is exactly as before and GitHub keeps
its version. Only merges prism-local starts carry the messages below: they tell a merge
it left half-done (the editor killed, git cut off by its timeout) from one someone
started in a terminal, which is theirs to finish.

The caller holds the repository (Repository.changing) and has recorded your edits
first: a merge that gives up puts back the files as they were when it started.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import gitenv
import keepboth
from repository import RepoBusy, Repository

# The messages of prism-local's merges. finish_crashed knows them by their start.
MERGE = "Merge changes from GitHub"
KEPT_BOTH = MERGE + " (both versions kept where both changed the same lines)"
COMBINED = MERGE + " (clashing lines: kept the combined version)"
PUT_BACK = "Put back commits dropped from GitHub"
OURS = (MERGE, PUT_BACK)


@dataclass
class Result:
    """What a merge did, for the pages. error is always the new one (None: fine); clash
    None leaves the files that clashed before as they were."""
    notice: str | None = None
    error: str | None = None
    clash: list[str] | None = None
    kept: list[dict] = field(default_factory=list)     # where both versions were kept


def merging(repo: Repository) -> bool:
    return (repo.git_dir / "MERGE_HEAD").exists()


def abort(repo: Repository) -> None:
    """Call off the merge in progress, if any: the files go back to how they were."""
    if merging(repo):
        repo.git("merge", "--abort", timeout=60)


def finish_crashed(repo: Repository) -> None:
    """A merge prism-local started and never finished (the editor was killed half-way, or
    git by its timeout): call it off, so this copy is as it was before it; the next sync
    merges again. A merge someone started in a terminal is theirs to finish, and is left
    alone."""
    if not merging(repo):
        return
    try:
        msg = (repo.git_dir / "MERGE_MSG").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if msg.startswith(OURS):
        # Unless a prism-local process is merging right now (it holds the repository): another
        # project's editor in the same repository, or this project's last editor still
        # closing. A crashed one holds nothing.
        try:
            with repo.changing(wait=0):
                repo.git("merge", "--abort", timeout=60)
        except RepoBusy:
            pass


def merge(repo: Repository, ref: str, n: int, prefix: str, again: bool = True) -> Result:
    """Merge GitHub's branch (`ref`, `n` commits this copy lacks) into this one. Where both
    sides changed the same lines, both versions are kept (keepboth.py), so syncing never
    stops for it. If anything goes wrong, the merge is called off and everything is exactly
    as before. `prefix`: the project's path in a shared repository."""
    plural = "s" if n != 1 else ""
    m = repo.git("merge", "--no-edit", "-m", MERGE, ref, timeout=120)
    if m.returncode == 0:
        return Result(notice=f"Combined your changes with {n} commit{plural} from GitHub (a co-author's work)",
                      clash=[])
    if merging(repo):
        try:
            kept = keep_both(repo, ref, prefix)
            c = repo.git("commit", "-q", "-m", KEPT_BOTH, timeout=60)
            if c.returncode != 0:
                raise RuntimeError((c.stderr or c.stdout).strip()[-300:])
        except (OSError, subprocess.SubprocessError, RuntimeError, keepboth.MergeError) as e:
            abort(repo)
            return clash(repo, ref, prefix, f" ({e})")
        where = ", ".join(sorted({k["path"] for k in kept}))
        return Result(notice=(f"Combined your changes with {n} commit{plural} from GitHub. You both "
                              f"changed the same lines in {where}: both versions are kept there "
                              "(see the GitHub menu)"), clash=[], kept=kept)
    out = m.stderr or m.stdout
    if "would be overwritten" in out and again:
        # Changes not recorded yet (another project of this shared repository, or edits
        # made outside the editor) in files GitHub changed: git touched nothing. Record
        # them, then merge again: they are kept and combined like any other.
        files = [ln.strip() for ln in out.splitlines() if ln.startswith(("\t", "    ")) and ln.strip()]
        if files:
            repo.ok("add", "-A", "--", *files)
            repo.ok("commit", "-q", "-m", "Autosave before combining with GitHub "
                    "(changed outside this editor)", "--", *files)
            return merge(repo, ref, n, prefix, again=False)
    return Result(error="Could not combine your changes with GitHub's: " + out.strip()[-300:])


def combine_mine(repo: Repository, ref: str) -> str | None:
    """After a clash, once you have edited the files to hold what both of you want: merge
    GitHub's branch, taking your version of the lines both changed (git merge -X ours:
    everything else of theirs comes in as usual). Their version stays in their commits;
    nothing leaves the history. None, or why it could not be done (called off)."""
    m = repo.git("merge", "--no-edit", "-X", "ours", "-m", COMBINED, ref, timeout=120)
    if m.returncode != 0:
        abort(repo)
        return "Could not combine: " + (m.stderr or m.stdout).strip()[-300:]
    return None


def put_back(repo: Repository, tip: str) -> str | None:
    """Merge `tip`, the commits a rewrite dropped from GitHub, back into this branch. None,
    or git's reason it could not be done (called off)."""
    m = repo.git("merge", "--no-edit", "-m", PUT_BACK, tip, timeout=120)
    if m.returncode != 0:
        abort(repo)
        return (m.stderr or m.stdout).strip()[-200:]
    return None


def clash(repo: Repository, ref: str, prefix: str, why: str = "") -> Result:
    """The automatic merge could not be done: say so; both versions stay where they are."""
    paths = [x for x in repo.git("diff", "--name-only", "HEAD", ref, timeout=30).stdout.splitlines() if x]
    names = [x[len(prefix):] if x.startswith(prefix) else x for x in paths]
    who = repo.git("log", "-1", "--format=%an, %ar", ref, timeout=20).stdout.strip()
    return Result(clash=names, error=(
        f"Your changes and GitHub's could not be combined by themselves{why}"
        f"{f' (last there: {who})' if who else ''}. Nothing was lost: GitHub keeps "
        "its version, this copy keeps yours (committed). In the GitHub menu: compare "
        "with GitHub's version, edit the file so it has what both of you want, then "
        "choose \"I've combined them\"."))


def unmerged(repo: Repository) -> dict[str, set[int]]:
    """The files a merge stopped at: path (from the top) -> the stages present
    (1 the common version, 2 yours, 3 GitHub's)."""
    out = repo.git("ls-files", "-u", "-z", timeout=30).stdout
    files: dict[str, set[int]] = {}
    for entry in out.split("\0"):
        if "\t" not in entry:
            continue
        meta, path = entry.split("\t", 1)
        files.setdefault(path, set()).add(int(meta.split()[2]))
    return files


def _blob(repo: Repository, spec: str) -> bytes:
    r = gitenv.git(repo.top, "show", spec, binary=True)
    if r.returncode != 0:
        raise RuntimeError(f"git show {spec}: " + r.stderr.decode("utf-8", "replace").strip())
    return r.stdout


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name).strip("-")[:30] or "github"


def keep_both(repo: Repository, ref: str, prefix: str) -> list[dict]:
    """Settle every file the merge stopped at, losing nothing: text gets both versions;
    a binary file (a figure) keeps yours, with GitHub's saved next to it; a file one side
    deleted and the other changed keeps the changed version. Where both were kept:
    {"path" (project-relative), "line", "who", "mtime", "side" (a file saved next to it)}."""
    top = repo.top
    mine = repo.git("config", "user.name", timeout=20).stdout.strip() or "you"
    them = repo.git("log", "-1", "--format=%an", ref, timeout=20).stdout.strip() or "a co-author"
    if them == mine:
        them = f"{them} (another copy)"
    kept = []

    def side_by_side(path: str, full: Path, rel: str, theirs: bytes) -> None:
        repo.ok("checkout", "--ours", "--", path)
        side = full.with_name(f"{full.stem}.from-{_slug(them)}{full.suffix}")
        side.write_bytes(theirs)
        repo.ok("add", "--", path, str(side.relative_to(top).as_posix()))
        kept.append({"path": rel, "line": None, "who": them, "side": side.name,
                     "mtime": full.stat().st_mtime})

    for path, stages in unmerged(repo).items():
        full = top / path
        rel = path[len(prefix):] if prefix and path.startswith(prefix) else path
        if 2 in stages and 3 in stages:
            ours, theirs = _blob(repo, f":2:{path}"), _blob(repo, f":3:{path}")
            base = _blob(repo, f":1:{path}") if 1 in stages else b""
            try:
                texts = [b.decode("utf-8") for b in (ours, base, theirs)]
                if any("\0" in t for t in texts):
                    raise UnicodeDecodeError("utf-8", b"", 0, 1, "binary")
            except UnicodeDecodeError:
                side_by_side(path, full, rel, theirs)
                continue
            if keepboth.too_big(texts[0], texts[1], texts[2]):
                # Most of the file clashes (rewritten, reformatted): both inside would double
                # the paper. Yours stays; GitHub's whole version is saved next to it.
                side_by_side(path, full, rel, theirs)
                continue
            text, places = keepboth.keep_both(texts[0], texts[1], texts[2], mine, them, keepboth.is_tex(path))
            crlf = b"\r\n" in ours or (full.exists() and b"\r\n" in full.read_bytes()[:65536])
            full.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
            repo.ok("add", "--", path)
            for line in places:
                kept.append({"path": rel, "line": line, "who": them, "mtime": full.stat().st_mtime})
        elif 2 in stages:                    # GitHub deleted it, you changed it: yours stays
            repo.ok("add", "--", path)
        elif 3 in stages:                    # you deleted it, GitHub changed it: theirs stays
            repo.ok("checkout", "--theirs", "--", path)
            repo.ok("add", "--", path)
        else:
            raise RuntimeError(f"unexpected merge state for {path}")
    if unmerged(repo):
        raise RuntimeError("files left unmerged")
    return kept
