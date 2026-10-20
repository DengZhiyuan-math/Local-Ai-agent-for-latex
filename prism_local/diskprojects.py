"""Follow Explorer changes in registered project containers, without moving any files.

Directory identities survive same-volume renames. Only a unique identity is used to
repair a path; a missing drive or an unrelated copy never replaces a registered project.
Named Home folders can bind to matching disk containers. Discovery stays inside those
containers; other project parents are searched only to recover moved projects.
"""
from __future__ import annotations

import os
import copy
import secrets
import threading
import time
from pathlib import Path

import registry
import diskwatch
from fsutil import SKIP_DIRS

MAX_DIRS = 10000
MAX_DEPTH = 8
_lock = threading.Lock()
_states: dict[str, dict] = {}


def identity(path: Path) -> list | None:
    try:
        st = path.stat()
        if not path.is_dir() or not st.st_ino:
            return None
        return [st.st_dev, st.st_ino, getattr(st, "st_birthtime_ns", None)]
    except OSError:
        return None


def _linked(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return True


def _walk(roots):
    """Bounded walk, excluding outputs, hidden directories and directory links."""
    seen, stack = set(), [(r, 0) for r in roots]
    while stack and len(seen) < MAX_DIRS:
        path, depth = stack.pop()
        key = registry.norm(path)
        if key in seen or _linked(path):
            continue
        seen.add(key)
        ident = identity(path)
        if ident is None:
            continue
        try:
            with os.scandir(path) as entries:
                entries = list(entries)
            names = {e.name for e in entries if e.is_file(follow_symlinks=False)}
            yield path, ident, "prism.json" in names or "main.tex" in names
            if depth < MAX_DEPTH:
                for e in entries:
                    if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") \
                            and e.name not in SKIP_DIRS | {"build", "conversations", "prism-uploads"}:
                        stack.append((Path(e.path), depth + 1))
        except OSError:
            continue


def _bind(data):
    """Migrate existing imported folder groups only when their disk name agrees."""
    for folder in data.get("folders", []):
        if folder.get("disk_path"):
            continue
        paths = [Path(e["path"]) for e in data["projects"]
                 if e.get("folder_id") == folder["id"]]
        candidates = {p.parent for p in paths if p.parent.name == folder["name"]}
        if len(candidates) == 1:
            root = candidates.pop()
            if all(registry.inside(p, root) for p in paths) and root.is_dir():
                folder["disk_path"] = str(root)
    # Imported trees often have projects only in their leaf groups.
    for _ in range(len(data.get("folders", []))):
        changed = False
        for folder in data.get("folders", []):
            if folder.get("disk_path"):
                continue
            children = [Path(f["disk_path"]) for f in data["folders"]
                        if f.get("parent") == folder["id"] and f.get("disk_path")]
            parents = {p.parent for p in children}
            if len(parents) == 1 and next(iter(parents)).name == folder["name"]:
                folder["disk_path"] = str(parents.pop())
                changed = True
        if not changed:
            break


def _roots(data):
    roots = set()
    for entry in [*data["projects"], *[f for f in data.get("folders", []) if f.get("disk_path")]]:
        path = Path(entry.get("disk_path") or entry["path"])
        # Watch the containing directory too, to find a renamed container itself.
        parent = path.parent
        if parent != Path(parent.anchor) and parent != Path.home():
            roots.add(parent)
    roots = sorted(roots, key=lambda p: len(p.parts))
    return [r for i, r in enumerate(roots) if not any(registry.inside(r, p) for p in roots[:i])]


def reconcile(data: dict) -> None:
    """Update a registry snapshot. Caller holds the registry lock before saving it."""
    _bind(data)
    folders = data.setdefault("folders", [])
    tracked = [*data["projects"], *[f for f in folders if f.get("disk_path")]]
    for entry in tracked:
        path = Path(entry.get("disk_path") or entry["path"])
        ident = identity(path)
        if ident is not None:
            # Do not overwrite a remembered identity when another folder occupies
            # the old name. It may be the replacement left behind by a move.
            entry.setdefault("disk_identity", ident)
    found = list(_walk(_roots(data)))
    moved = set()
    relocations = []
    by_identity = {}
    for path, ident, _ in found:
        by_identity.setdefault(tuple(ident), []).append(path)
    for entry in tracked:
        field = "disk_path" if "disk_path" in entry else "path"
        old = Path(entry[field])
        ident = entry.get("disk_identity")
        if ident and identity(old) != ident:
            matches = by_identity.get(tuple(ident), [])
            if len(matches) == 1:
                entry[field] = str(matches[0])
                moved.add(id(entry))
                relocations.append((old, matches[0]))
                if entry.get("name") == old.name:
                    entry["name"] = matches[0].name
            elif not matches:
                # A folder restored from backup has a new identity at the same
                # registered path. Remember it so later renames still follow it.
                restored = identity(old)
                if restored is not None:
                    entry["disk_identity"] = restored
    for folder in folders:
        sync = folder.get("sync") or {}
        if sync.get("repo"):
            old_repo = Path(sync["repo"])
            for old, new in sorted(relocations, key=lambda pair: len(pair[0].parts), reverse=True):
                if registry.inside(old_repo, old):
                    sync["repo"] = str(new / old_repo.relative_to(old))
                    break
    bound = {registry.norm(Path(f["disk_path"])): f for f in folders if f.get("disk_path")}
    registered = {registry.norm(Path(e["path"])): e for e in data["projects"]}
    ignored = set(data.get("disk_ignored", []))

    def group(path):
        key = registry.norm(path)
        if key in bound:
            return bound[key]
        owners = [f for f in bound.values() if registry.inside(path, Path(f["disk_path"]))]
        if not owners:
            return None
        parent = group(path.parent)
        folder = {"id": secrets.token_hex(4), "name": path.name, "parent": parent["id"],
                  "disk_path": str(path), "disk_identity": identity(path)}
        folders.append(folder)
        bound[key] = folder
        return folder

    for path, ident, project in found:
        key = registry.norm(path)
        entry = registered.get(key)
        if entry is None and project and key not in ignored:
            owner = group(path.parent)
            if owner is not None:
                entry = {"path": str(path), "added": time.time(), "disk_identity": ident}
                data["projects"].append(entry)
                registered[key] = entry
                moved.add(id(entry))
        if entry is not None and id(entry) in moved:
            # A project moved outside a disk-bound group keeps its manual grouping.
            owner = group(path.parent)
            if owner is not None:
                entry["folder_id"] = owner["id"]
    # Reparent a disk container moved into another watched container as well.
    for folder in list(bound.values()):
        if id(folder) in moved:
            parent = group(Path(folder["disk_path"]).parent)
            if parent is not None:
                folder["parent"] = parent["id"]


def _signature(data):
    return (tuple(e["path"] for e in data["projects"]),
            tuple((f["id"], f["name"], f.get("parent"), f.get("disk_path"))
                  for f in data.get("folders", [])))


def reset():
    """Release native handles, also useful when an isolated state directory closes."""
    with _lock:
        for state in _states.values():
            state["events"].close()
        _states.clear()


def refresh(missing=None) -> dict:
    """Reconcile once on startup, on notifications, or on a newly missing path.

    Ordinary Home status requests read the registry without walking directories. A
    missing path already searched remains quiet until a notification or registry change.
    """
    key = str(registry.state_dir())
    with _lock:
        data = registry.load_projects()
        if key not in _states:
            _states[key] = {"events": diskwatch.Changes((registry.state_dir(),), state_root=registry.state_dir()),
                            "seen": -1, "signature": None, "missing": set()}
        state = _states[key]
        events = state["events"]
        missing = state["missing"] if missing is None else set(missing)
        signature = _signature(data)
        generation = events.stamp()
        if generation == state["seen"] and signature == state["signature"] \
                and missing == state["missing"]:
            return data
        events.configure(_roots(data))
        generation = events.stamp()
        path = registry._registry_path()
        with registry.FileLock(path.with_suffix(".lock"), timeout=10):
            data = registry.load_projects()
            before = copy.deepcopy(data)
            reconcile(data)
            if data != before:
                registry.write_state(path, data)
        state.update(seen=generation, signature=_signature(data), missing=missing)
        events.configure(_roots(data))
        return data


def notifications():
    """One event source per state directory, shared by every open Home page."""
    refresh()
    with _lock:
        return _states[str(registry.state_dir())]["events"]
