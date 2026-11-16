"""The project's versions: its git history as the editor and the agent see it, versions
pinned under a name (v1, submitted, ...), and the files put back as they were in one.

    v = Versions(gitsync)                   # GitSync: the repository, its prefix, its locks
    v.pins()                                # [{"name", "hash", "at", "note"}], newest first
    v.log(path=None)                        # the project's commits (or a file's), with their pins
    v.show("v1")                            # a version: its message, what it changed, its files
    v.show("v1", "main.tex")                # a file as it was in it
    v.diff("v1")                            # from that version to the files now
    v.restore("v1", paths=None, writable)   # the files put back (written, not committed)
    v.pin("v1", note)                       # a name for HEAD: an annotated tag

A version is named by a pin (v1), a commit hash (4 to 40 hex digits), or HEAD, HEAD~N.
Nothing else is passed to git: no option, range or reflog syntax can slip in.

Pins are git tags. In a repository shared by several projects, each project's are under
its own folder (refs/tags/<prefix>v1), so two projects can each have a v1.

restore() writes the files only; whoever calls it records them: the editor commits the
state before and after (GitSync.restore_version: nothing is lost, the restore itself can
be undone from the history), and an agent's turn commits them with its other changes
(and its Undo puts them back). History is never rewritten: no reset, no checkout of a branch.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import quote
from pathlib import Path

import gitenv
from errors import Refused
from fsutil import project_path, write_bytes

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,49}")
HEX = re.compile(r"[0-9a-fA-F]{4,40}")
HEAD = re.compile(r"HEAD(~\d{1,5})?")
MAX_FILES_LISTED = 400
MAX_CONTENT = 400_000        # characters of a file shown
MAX_DIFF = 120_000           # characters of a diff shown
FILE_MODES = ("100644", "100755")


class VersionError(Refused):
    """Said to whoever asked: a version that does not exist, a name taken, no repository."""


def check_name(name: str) -> str:
    name = str(name or "").strip()
    if not NAME.fullmatch(name) or ".." in name or name.endswith((".", ".lock")):
        raise VersionError("A version name is 1 to 50 letters, digits, '.', '-' or '_', "
                           "starting with a letter or digit (v1, draft-2, submitted).")
    return name


def blob_id(data: bytes, like: str) -> str:
    """git's object id of a blob holding `data`, in the hash `like` (an id) is in."""
    h = hashlib.sha256() if len(like) == 64 else hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


class Versions:
    def __init__(self, sync):
        if not sync.check_repo():
            raise VersionError("This project has no git repository of its own, so it has no "
                               "versions. Create one from the GitHub menu.")
        self.sync = sync
        self.prefix = sync.prefix             # "" or "paper/": the project's folder in the repository
        # Git ref components cannot contain spaces, '~', or several forms of dots.
        # Keep file paths unchanged and encode only the tag namespace.
        self.tag_prefix = quote(self.prefix, safe="/_-").replace(".", "%2E").replace("~", "%7E")
        self.top: Path = sync.top

    # ------------------------------------------------------------ git
    def _git(self, *args: str, timeout: float = 60) -> str:
        r = self.sync.git(*args, timeout=timeout)
        if r.returncode != 0:
            raise VersionError((r.stderr or r.stdout).strip()[-400:] or f"git {args[0]} failed")
        return r.stdout

    def _bytes(self, *args: str, timeout: float = 60) -> bytes:
        r = gitenv.git(self.top, *args, timeout=timeout, binary=True)
        if r.returncode != 0:
            raise VersionError(r.stderr.decode("utf-8", "replace").strip()[-400:] or f"git {args[0]} failed")
        return r.stdout

    def _spec(self) -> str:
        """The project's files, as a pathspec from the repository's top."""
        return self.prefix.rstrip("/") or "."

    def _own(self, full: str) -> str | None:
        """A path from the repository's top as the project sees it; None outside it."""
        if not self.prefix:
            return full
        return full[len(self.prefix):] if full.startswith(self.prefix) else None

    def _has_head(self) -> bool:
        return self.sync.git("rev-parse", "-q", "--verify", "HEAD").returncode == 0

    # ------------------------------------------------------------ pins
    def _ref(self, name: str) -> str:
        return f"refs/tags/{self.tag_prefix}{name}"

    def pins(self) -> list[dict]:
        out = self._git("for-each-ref", "--sort=-creatordate",
                        "--format=%(refname)%1f%(objectname)%1f%(*objectname)%1f%(creatordate:unix)%1f%(contents:subject)",
                        f"refs/tags/{self.tag_prefix}")
        rows = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 5:
                continue
            ref, obj, peeled, at, note = parts
            name = ref[len("refs/tags/") + len(self.tag_prefix):]
            if not NAME.fullmatch(name):
                continue                     # another project's namespace, not one of our pins
            rows.append({"name": name, "hash": peeled or obj,
                         "at": int(at or 0), "note": note if peeled else ""})
        return rows

    def _pins_by_hash(self) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for p in self.pins():
            found.setdefault(p["hash"], []).append(p["name"])
        return found

    def resolve(self, rev: str) -> str:
        """The full hash of the commit `rev` names (a pin, a hash, HEAD or HEAD~N)."""
        rev = str(rev or "").strip()
        if not rev:
            raise VersionError("Name a version: a pinned name (v1), a commit hash, or HEAD.")
        candidates = []
        if NAME.fullmatch(rev):
            candidates.append(self._ref(rev))
        if HEX.fullmatch(rev) or HEAD.fullmatch(rev):
            candidates.append(rev)
        for c in candidates:
            r = self.sync.git("rev-parse", "-q", "--verify", f"{c}^{{commit}}")
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        pinned = ", ".join(p["name"] for p in self.pins()[:20]) or "none yet"
        raise VersionError(f"No version {rev!r} in this project's history. Pinned versions: {pinned}. "
                           "A commit can also be named by its hash (see the history).")

    # ------------------------------------------------------------ looking
    def log(self, path: str | None = None, n: int = 40) -> list[dict]:
        """The commits of the project (or that changed `path`, following renames), newest first."""
        if not self._has_head():
            return []
        n = max(1, min(int(n or 40), 500))
        args = ["log", f"-n{n}", "--format=%H%x1f%ct%x1f%an%x1f%s"]
        args += ["--follow", "--", self.sync.rel(path)] if path else ["--", self._spec()]
        pins = self._pins_by_hash()
        rows = []
        for line in self._git(*args, timeout=30).splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                rows.append({"hash": parts[0], "at": int(parts[1]), "author": parts[2],
                             "message": parts[3], "pins": pins.get(parts[0], [])})
        return rows

    def grouped(self, path: str | None = None, n: int = 300) -> dict:
        """The history folded by pinned version: `groups`, newest first, one per pinned commit
        (its names, note, when), and each commit of log() with the `version` it belongs to:
        the hash of the oldest pinned commit that contains it, so that a version's group holds
        what was done between the version before it and itself (merges too). A commit no pin
        contains yet has version None: the work since the last pin."""
        commits = self.log(path, n)
        groups: dict[str, dict] = {}
        for p in reversed(self.pins()):                 # oldest first: its note and date lead
            g = groups.setdefault(p["hash"], {"hash": p["hash"], "names": [], "note": "", "at": p["at"]})
            g["names"].append(p["name"])
            g["note"] = g["note"] or p["note"]
        ancestors = {h: set(self._git("rev-list", h, timeout=60).split()) for h in groups}
        for c in commits:
            inside = [h for h in groups if c["hash"] in ancestors[h]]
            c["version"] = min(inside, key=lambda h: len(ancestors[h])) if inside else None
        order = sorted(groups.values(), key=lambda g: (-len(ancestors[g["hash"]]), -g["at"]))
        for g in order:
            g["count"] = sum(1 for c in commits if c["version"] == g["hash"])
        return {"groups": order, "commits": commits}

    def files_at(self, commit: str) -> list[tuple[str, str, str, str]]:
        """(mode, project path, full path, object id) of each file the project had at `commit`."""
        out = self._git("ls-tree", "-r", "-z", commit, "--", self._spec(), timeout=30)
        rows = []
        for entry in out.split("\0"):
            if "\t" not in entry:
                continue
            meta, full = entry.split("\t", 1)
            own = self._own(full)
            if own is not None:
                mode, _, obj = meta.split(" ")
                rows.append((mode, own, full, obj))
        return rows

    def show(self, rev: str, path: str | None = None) -> dict:
        """A version: what its commit says and changed, and the files it has; with `path`,
        that file as it was in it."""
        h = self.resolve(rev)
        if path:
            full = self.sync.rel(path)
            r = gitenv.git(self.top, "cat-file", "--filters", f"{h}:{full}", timeout=30, binary=True)
            if r.returncode != 0:
                names = [f[1] for f in self.files_at(h)]
                raise VersionError(f"{path} is not in version {rev} ({h[:7]}). Its files: "
                                   + (", ".join(names[:60]) or "none"))
            data = r.stdout
            text = None if b"\0" in data[:8000] else data.decode("utf-8", "replace")
            return {"hash": h, "path": path, "size": len(data), "binary": text is None,
                    "content": text[:MAX_CONTENT] if text is not None else None,
                    "truncated": text is not None and len(text) > MAX_CONTENT}
        head = self._git("log", "-1", "--format=%H%x1f%ct%x1f%an%x1f%B", h).split("\x1f", 3)
        changed = []
        out = self._git("diff-tree", "-r", "--root", "-M", "-z", "--name-status", "--no-commit-id",
                        "-m", "--first-parent", h, "--", self._spec(), timeout=30)
        parts = [p for p in out.split("\0")]
        i = 0
        while i < len(parts) and parts[i]:
            status = parts[i]
            n = 2 if status[:1] in "RC" else 1
            names = [self._own(p) or p for p in parts[i + 1:i + 1 + n]]
            changed.append({"status": status[:1], "path": names[-1],
                            **({"from": names[0]} if n == 2 else {})})
            i += 1 + n
        files = [f[1] for f in self.files_at(h)]
        return {"hash": h, "at": int(head[1]), "author": head[2], "message": head[3].strip(),
                "pins": self._pins_by_hash().get(h, []), "changed": changed,
                "files": files[:MAX_FILES_LISTED], "file_count": len(files)}

    def diff(self, rev: str, to: str | None = None, path: str | None = None) -> dict:
        """What differs from version `rev` to version `to`, or to the files now (what is
        saved, committed or not; files git does not track are left out)."""
        h = self.resolve(rev)
        h2 = self.resolve(to) if to else None
        rel = [f"--relative={self.prefix}"] if self.prefix else []
        spec = self.sync.rel(path) if path else self._spec()
        ends = [h] + ([h2] if h2 else [])
        stat = self._git("diff", "--no-color", "-M", "--stat=120", *rel, *ends, "--", spec, timeout=60)
        text = self._git("diff", "--no-color", "-M", *rel, *ends, "--", spec, timeout=60)
        return {"from": h, "to": h2, "stat": stat, "diff": text[:MAX_DIFF], "truncated": len(text) > MAX_DIFF}

    # ------------------------------------------------------------ changing
    def restore(self, rev: str, paths: list[str] | None = None, writable=None) -> dict:
        """Put the project's files (or `paths`) back as they were in version `rev`: each file
        it had is written as it was (line ends as git checks them out), and a file the
        project has committed since, which that version did not have, is deleted. Files git
        does not track (build output, ignored ones) are left alone. `writable(rel)` False:
        not touched (skipped). Writes only: the caller records them."""
        h = self.resolve(rev)
        then = {own: (mode, full, obj) for mode, own, full, obj in self.files_at(h)}
        now = {f[1] for f in self.files_at("HEAD")} if self._has_head() else set()
        if paths:
            wanted = {str(p).replace("\\", "/").strip("/") for p in paths if str(p).strip()}
            unknown = sorted(p for p in wanted if p not in then and p not in now
                             and not any(t.startswith(p + "/") for t in set(then) | now))
            if unknown:
                raise VersionError(f"Not in version {rev} ({h[:7]}): {', '.join(unknown)}")
            inside = lambda p: any(p == w or p.startswith(w + "/") for w in wanted)  # noqa: E731
            then = {p: v for p, v in then.items() if inside(p)}
            now = {p for p in now if inside(p)}
        root = self.sync.root_fn().resolve()
        written, deleted, skipped, unchanged = [], [], [], 0
        writes, removals, empty_dirs = {}, {}, set()

        def target_path(rel):
            target = project_path(root, rel)
            # Never follow a link into another project file, even within the root.
            lexical = root / rel
            if any(p.is_symlink() for p in (lexical, *lexical.parents) if p != root and root in p.parents):
                raise VersionError(f"Cannot restore through a symbolic link: {rel}")
            return target

        for rel in sorted(now - set(then)):
            try:
                target = target_path(rel)
            except ValueError as e:
                skipped.append({"path": rel, "why": str(e)})
                continue
            if not target.is_file():
                continue
            if writable is not None and not writable(rel):
                skipped.append({"path": rel, "why": "outside what this turn may change"})
                continue
            removals[rel] = target
        for rel, (mode, full, obj) in sorted(then.items()):
            if mode not in FILE_MODES:                  # a link or a submodule: not put back
                skipped.append({"path": rel, "why": "not a regular file"})
                continue
            try:
                target = target_path(rel)
            except ValueError as e:
                skipped.append({"path": rel, "why": str(e)})
                continue
            try:
                had = target.read_bytes() if target.is_file() else None
            except OSError:
                had = None
            # The same as then, as git stores it (line ends aside: core.autocrlf): not
            # rewritten, or a turn would show it as changed.
            if had is not None and blob_id(had, obj) == obj:
                unchanged += 1
                continue
            data = self._bytes("cat-file", "--filters", f"{h}:{full}")
            if had == data:
                unchanged += 1
                continue
            if writable is not None and not writable(rel):
                skipped.append({"path": rel, "why": "outside what this turn may change"})
                continue
            if target.is_file() and rel not in now:
                raise VersionError(f"Cannot restore over an untracked or ignored file: {rel}. "
                                   "Move or save that draft first.")
            writes[rel] = (target, data)

        # Validate every collision before touching any file. Only tracked, permitted
        # removals may clear the way for a file/directory layout change.
        removable = set(removals.values())
        for rel, (target, _) in writes.items():
            for parent in target.parents:
                if parent == root:
                    break
                if parent.is_file() and parent not in removable:
                    raise VersionError(f"Cannot restore {rel}: an untracked, ignored or protected file blocks it.")
            if target.is_dir():
                for child in target.rglob("*"):
                    if child.is_symlink() or (not child.is_dir() and child not in removable):
                        raise VersionError(f"Cannot replace {rel}: it contains an untracked, ignored or protected file.")
                    if child.is_dir():
                        empty_dirs.add(child)
                empty_dirs.add(target)
        for rel, target in removals.items():
            target.unlink()
            deleted.append(rel)
        for directory in sorted(empty_dirs, key=lambda p: len(p.parts), reverse=True):
            directory.rmdir()                 # empty only; never recursive deletion
        for rel, (target, data) in writes.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            write_bytes(target, data)
            written.append(rel)
        return {"hash": h, "written": written, "deleted": deleted, "skipped": skipped, "unchanged": unchanged}

    def free(self, name: str) -> str:
        """`name`, checked: a valid name no pin has yet (VersionError otherwise)."""
        name = check_name(name)
        if self.sync.git("rev-parse", "-q", "--verify", self._ref(name)).returncode == 0:
            had = self.resolve(name)
            raise VersionError(f"A version is already pinned as {name} ({had[:7]}). Choose another "
                               "name, or unpin that one first.")
        return name

    def pin(self, name: str, note: str = "", rev: str = "HEAD") -> dict:
        """Name the version `rev` (an annotated tag). A name already taken is refused."""
        name = self.free(name)
        h = self.resolve(rev)
        note = " ".join(str(note or "").split())[:200] or f"Pinned version {name}"
        self._git("tag", "-a", "-m", note, "--", self._ref(name)[len("refs/tags/"):], h)
        obj = self._git("rev-parse", self._ref(name)).strip()
        self._git("update-ref", self._pending_ref(name), obj)
        return {"name": name, "hash": h, "note": note}

    def _pending_ref(self, name: str) -> str:
        return f"refs/prism-local/pending-tags/{self.tag_prefix}{name}"

    def import_pins(self) -> None:
        """Import fetched pins without moving a local pin or reviving a locally hidden one."""
        prefix = f"refs/prism-local/fetched-tags/{self.tag_prefix}"
        imports = []
        for line in self._git("for-each-ref", "--format=%(refname) %(objectname)", prefix).splitlines():
            ref, obj = line.split()
            name = ref[len(prefix):]
            if not NAME.fullmatch(name):
                continue
            hidden = self.sync.git("rev-parse", "-q", "--verify", f"refs/prism-local/hidden-tags/{self.tag_prefix}{name}")
            if hidden.returncode == 0 and hidden.stdout.strip() == obj:
                continue
            local = self.sync.git("rev-parse", "-q", "--verify", self._ref(name))
            if local.returncode == 0:
                if local.stdout.strip() != obj:
                    raise VersionError(f"Pinned version {name} differs on GitHub; both versions are kept. "
                                       "Unpin or rename the local version before syncing this name.")
            else:
                imports.append((self._ref(name), obj))
        for ref, obj in imports:
            self._git("update-ref", ref, obj, "0" * len(obj))   # create only; never overwrite a racing pin

    def pending(self) -> list[tuple[str, str, str]]:
        """Durable upload queue: (tag ref, object id, queue ref), scoped to this project."""
        prefix = f"refs/prism-local/pending-tags/{self.tag_prefix}"
        common = self.sync.git_dir
        if (common / "commondir").is_file():
            common = (common / (common / "commondir").read_text(encoding="utf-8").strip()).resolve()
        folder = common / prefix
        # An idle sync must not spawn git every five seconds. Queue refs can also be
        # packed by git gc, so both loose refs and packed-refs participate in the key.
        try:
            paths = [common / "packed-refs", *sorted(folder.rglob("*"))]
            stamps = []
            for path in paths:
                if path.is_file():
                    st = path.stat()
                    stamps.append((str(path), st.st_mtime_ns, st.st_size))
            key = (str(common), prefix, tuple(stamps))
        except OSError:
            key = None
        cached = getattr(self.sync, "_pending_tags", None)
        if key is not None and cached and cached[0] == key:
            return list(cached[1])
        rows = []
        for line in self._git("for-each-ref", "--format=%(refname) %(objectname)", prefix).splitlines():
            ref, obj = line.split()
            name = ref[len(prefix):]
            if NAME.fullmatch(name):
                rows.append((self._ref(name), obj, ref))
        self.sync._pending_tags = (key, rows)
        return rows

    def unpin(self, name: str) -> dict:
        name = check_name(name)
        h = self.resolve(name)
        obj = self._git("rev-parse", self._ref(name)).strip()
        self._git("update-ref", f"refs/prism-local/hidden-tags/{self.tag_prefix}{name}", obj)
        self._git("tag", "-d", "--", self._ref(name)[len("refs/tags/"):])
        self._git("update-ref", "-d", self._pending_ref(name))
        return {"name": name, "hash": h}
