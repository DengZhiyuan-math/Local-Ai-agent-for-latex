"""Private, root-bound research authority and recoverable project projections."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from errors import Conflict, Refused
from fsutil import replace_file
from guarded_compile import _read_plain
import registry
from research_edits import sign_receipt, verify_receipt


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _record_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("The received research record repeats an object key.")
        result[key] = value
    return result


def read_plain(path, limit):
    """Read one bounded regular file without following a substituted leaf."""
    path = Path(path)
    if path.is_absolute() and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"):
        return _read_plain(Path(path.anchor), path.relative_to(path.anchor).as_posix(), limit)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= limit:
        raise Refused("Research provenance requires bounded regular files.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not 0 <= opened.st_size <= limit:
            raise Refused("Research provenance requires bounded regular files.")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    if len(raw) > limit or not identity(before) == identity(opened) == identity(after) == identity(current):
        raise Conflict("The research record changed while it was read.")
    return raw


class Provenance:
    def __init__(self, root, record, limit):
        self.root = Path(root).resolve()
        self.key = registry.project_key(self.root)
        self.record, self.limit = self.root / record, limit
        self.folder = registry.state_dir().resolve() / "research-provenance" / digest(str(self.root).encode())
        self.path = self.folder / "state.json"

    def _paths(self):
        if any(path.is_symlink() for path in (self.folder.parent, self.folder, self.path)):
            raise Refused("Private research provenance cannot use linked files.")

    def _public(self):
        try:
            if any(path.is_symlink() for path in (self.record.parent, self.record)):
                raise Refused("The received research record is linked.")
            raw = read_plain(self.record, self.limit)
        except FileNotFoundError:
            return {"raw": None, "received": None, "sha256": None, "error": None}
        except (OSError, ValueError) as error:
            return {"raw": None, "received": None, "sha256": None, "error": str(error)}
        try:
            received = json.loads(raw, object_pairs_hook=_record_object)
            if not isinstance(received, dict):
                raise ValueError("The received research record is not an object.")
            json.dumps(received, ensure_ascii=False, allow_nan=False).encode("utf-8")
            return {"raw": raw, "received": received, "sha256": digest(raw), "error": None}
        except (UnicodeError, ValueError, RecursionError) as error:
            return {"raw": raw, "received": None, "sha256": digest(raw), "error": str(error)}

    def _state(self):
        self._paths()
        try:
            state = json.loads(read_plain(self.path, self.limit * 3))
        except FileNotFoundError:
            return {"schema": 1, "root": str(self.root), "project_key": self.key, "committed": None, "pending": None}
        except (OSError, ValueError) as error:
            raise Refused("Private research provenance could not be verified; no received policy was trusted.") from error
        if (not isinstance(state, dict) or state.get("schema") != 1 or state.get("root") != str(self.root)
                or state.get("project_key") != self.key):
            raise Refused("Private research provenance belongs to another project.")
        self._verify_entry(state.get("committed"))
        pending = state.get("pending")
        if pending is not None:
            if not isinstance(pending, dict) or not isinstance(pending.get("next"), dict):
                raise Refused("The pending research projection is damaged.")
            self._verify_entry(pending.get("next"))
            receipt = pending.get("receipt")
            if (not isinstance(receipt, dict) or receipt.get("kind") != "research_projection"
                    or receipt.get("root") != str(self.root) or not verify_receipt(receipt, self.key)
                    or receipt.get("before_sha256") != pending.get("before_sha256")
                    or receipt.get("next_sha256") != pending.get("next_sha256")
                    or receipt.get("checkpoint_id") != pending["next"]["receipt"]["id"]):
                raise Refused("The pending research projection has no verified local intent.")
        return state

    @staticmethod
    def _basis_data(data):
        return {key: data.get(key) for key in ("sources", "explicit_notation", "initialized_at")}

    def _verify_entry(self, entry):
        if entry is None:
            return
        try:
            data, receipt, basis = entry["data"], entry["receipt"], entry.get("basis")
            valid = (isinstance(data, dict) and receipt.get("kind") == "research_checkpoint"
                and receipt.get("root") == str(self.root) and verify_receipt(receipt, self.key)
                and receipt.get("data_sha256") == digest(encode(data))
                and receipt.get("public_sha256") == entry.get("public_sha256")
                and receipt.get("basis_id") == (basis.get("id") if basis else None))
            if basis is None:
                valid = valid and not data.get("initialized") and not data.get("sources") and not data.get("explicit_notation")
            else:
                valid = (valid and basis.get("kind") == "research_initialization_basis"
                    and basis.get("root") == str(self.root) and verify_receipt(basis, self.key)
                    and basis.get("sha256") == digest(encode(self._basis_data(data))))
            if not valid:
                raise ValueError("invalid checkpoint")
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise Refused("The local research checkpoint has no verified current-root basis.") from error

    def _save(self, state):
        raw = encode(state) + b"\n"
        if len(raw) > self.limit * 3:
            raise Refused("The private research checkpoint limit was reached.")
        replace_file(self.path, raw, durable=True)

    def _entry(self, data, public_sha256, basis):
        receipt = sign_receipt({"id": uuid.uuid4().hex, "project_key": self.key, "kind": "research_checkpoint",
            "root": str(self.root), "data_sha256": digest(encode(data)), "public_sha256": public_sha256,
            "basis_id": basis["id"] if basis else None})
        return {"data": copy.deepcopy(data), "basis": basis, "receipt": receipt, "public_sha256": public_sha256}

    def inspect(self):
        self._paths()
        if not self.path.exists():
            return {**self._public(), "entry": None, "pending": False, "conflict": False}
        with registry.FileLock(self.path.with_suffix(".lock"), timeout=3):
            state, public = self._state(), self._public()
            pending = state.get("pending")
            conflict = False
            if pending is not None:
                if not public["error"] and public["sha256"] == pending["next_sha256"]:
                    state.update(committed=pending["next"], pending=None)
                    self._save(state)
                elif public["sha256"] != pending["before_sha256"]:
                    conflict = True
            return {**public, "entry": copy.deepcopy(state.get("committed")),
                    "pending": state.get("pending") is not None, "conflict": conflict}

    def _archive(self, raw):
        if raw is None:
            return
        folder = self.folder / "received"
        if folder.is_symlink():
            raise Refused("Received research evidence cannot use a linked folder.")
        path = folder / (digest(raw) + ".record")
        if path.is_symlink():
            raise Refused("Received research evidence cannot use a linked file.")
        if path.exists():
            if read_plain(path, self.limit) != raw:
                raise Refused("A received research evidence copy changed.")
            return
        existing = list(folder.glob("*.record")) if folder.exists() else []
        if len(existing) >= 100 or sum(item.stat().st_size for item in existing) + len(raw) > self.limit * 4:
            raise Refused("The retained research reference limit was reached.")
        replace_file(path, raw, durable=True)

    def save(self, data, expected_sha256, writer, *, initializing=False, private_only=False, expected_revision=None):
        self._paths()
        with registry.FileLock(self.path.with_suffix(".lock"), timeout=3):
            state, public = self._state(), self._public()
            if data.get("project_key") != self.key or len(encode(data)) > self.limit:
                raise Refused("The local research checkpoint has invalid project data.")
            if public["sha256"] != expected_sha256:
                raise Conflict("The received research record changed; its bytes were preserved.")
            old = state.get("committed")
            if (old["receipt"]["data_sha256"] if old else None) != expected_revision:
                raise Conflict("The local research policy changed; review the current record before saving.")
            basis = old.get("basis") if old else None
            if initializing:
                basis = sign_receipt({"id": uuid.uuid4().hex, "project_key": self.key,
                    "kind": "research_initialization_basis", "root": str(self.root),
                    "sha256": digest(encode(self._basis_data(data)))})
            elif basis is not None and self._basis_data(data) != self._basis_data(old["data"]):
                raise Refused("Received content cannot replace the established initialization basis.")
            elif basis is None and (data.get("initialized") or data.get("sources") or data.get("explicit_notation")):
                raise Refused("Explicit initialization must establish the local research basis first.")
            if private_only:
                if old is not None and old["data"] == data:
                    return False
                entry = self._entry(data, old.get("public_sha256") if old else None, basis)
                state["committed"] = entry
                self._save(state)
                return False
            if public["error"] or state.get("pending") and public["sha256"] not in (
                    state["pending"]["before_sha256"], state["pending"]["next_sha256"]):
                raise Conflict("Resolve the received research record before replacing its projection.")
            if old is None or public["sha256"] != old.get("public_sha256"):
                self._archive(public["raw"])
            raw = (json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + "\n").encode()
            next_sha = digest(raw)
            entry = self._entry(data, next_sha, basis)
            intent = sign_receipt({"id": uuid.uuid4().hex, "project_key": self.key, "kind": "research_projection",
                "root": str(self.root), "before_sha256": expected_sha256, "next_sha256": next_sha,
                "checkpoint_id": entry["receipt"]["id"]})
            state["pending"] = {"before_sha256": expected_sha256, "next_sha256": next_sha, "next": entry, "receipt": intent}
            self._save(state)
            try:
                if self._public()["sha256"] != expected_sha256:
                    raise Conflict("The received research record changed before the local projection was saved.")
                writer(raw)
            except BaseException:
                after = self._public()
                if not after["error"] and after["sha256"] == expected_sha256:
                    state["pending"] = None
                    self._save(state)
                raise
            state.update(committed=entry, pending=None)
            self._save(state)
            return True
