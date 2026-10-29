"""Version-bound editor input receipts for the controlled API boundary.

Page-private capabilities distinguish API file tools from the editor's verified
input path. Signatures also require a matching local editor witness: a readable
key alone cannot manufacture adoption. This is not isolation from arbitrary
localhost clients, browser automation, unrestricted CLIs, or project builds that
execute host code (custom commands, shell escape, Lua, or build scripts).
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import hashlib
import hmac
import json
import secrets
import threading
import time

from errors import Refused
from fsutil import replace_file
from notation import math_notations
import registry

KEY_FILE = "research-evidence.key"
WITNESS_FILE = "research-edit-receipts.json"
MAX_ADDITIONS = 20000


def _key(*, create=False):
    path = registry.state_dir() / KEY_FILE
    if not create and not path.exists():
        return None
    if path.is_symlink():
        raise Refused("The research evidence key must be a regular local state file.")
    with registry.FileLock(path.with_suffix(".lock"), timeout=3):
        try:
            value = path.read_bytes()
        except FileNotFoundError:
            if not create:
                return None
            value = secrets.token_bytes(32)
            replace_file(path, value, durable=True)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        if len(value) != 32:
            raise Refused("The research evidence key is damaged; no receipt was trusted.")
        return value


def _payload(receipt):
    return json.dumps({k: v for k, v in receipt.items() if k != "signature"},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fingerprint(receipt):
    return hashlib.sha256(_payload(receipt) + receipt["signature"].encode("ascii")).hexdigest()


def _witness(receipt, *, create=False) -> bool:
    # The project record remains the knowledge authority. This separate local
    # witness only establishes that this editor actually issued a receipt.
    path = registry.state_dir() / WITNESS_FILE
    if not create and not path.exists():
        return False
    if path.is_symlink():
        raise Refused("The research receipt witness must be a regular local state file.")
    with registry.FileLock(path.with_suffix(".lock"), timeout=3):
        data = registry.read_state(path, tries=1)
        if not data and create:
            data = {"version": 1, "receipts": {}}
        if data.get("version") != 1 or not isinstance(data.get("receipts"), dict):
            raise Refused("The research receipt witness is damaged; no receipt was trusted.")
        expected = _fingerprint(receipt)
        if not create:
            return data["receipts"].get(receipt["id"]) == expected
        if len(data["receipts"]) >= 100000:
            raise Refused("The local research receipt limit was reached.")
        data["receipts"][receipt["id"]] = expected
        registry.write_state(path, data)
        return True


def verify_receipt(receipt, project_key: str) -> bool:
    if (not isinstance(receipt, dict) or receipt.get("project_key") != project_key
            or not isinstance(receipt.get("signature"), str)):
        return False
    try:
        key = _key()
        return key is not None and hmac.compare_digest(
            receipt["signature"], hmac.new(key, _payload(receipt), hashlib.sha256).hexdigest()) \
            and _witness(receipt)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def sign_receipt(payload: dict) -> dict:
    """Seal one editor-issued event and durably witness its exact contents."""
    if (not isinstance(payload, dict) or "signature" in payload
            or not isinstance(payload.get("id"), str) or not payload["id"]
            or not isinstance(payload.get("project_key"), str) or not payload["project_key"]):
        raise Refused("A research receipt must identify its editor event and project.")
    receipt = copy.deepcopy(payload)
    receipt["signature"] = hmac.new(_key(create=True), _payload(receipt), hashlib.sha256).hexdigest()
    _witness(receipt, create=True)
    return receipt


def make_receipt(project_key: str, path: str, before: bytes, after: bytes):
    """Sign only identifiers newly introduced by this exact verified editor save."""
    from research import declared_identifier_commands
    old, new = before.decode("utf-8"), after.decode("utf-8")
    commands = declared_identifier_commands([old, new])
    existing = {s["canonical"] for s in math_notations(old, known_commands=commands)["symbols"]}
    added, seen = [], set()
    for item in math_notations(new, known_commands=commands)["symbols"]:
        if item["canonical"] not in existing and item["canonical"] not in seen:
            added.append(item)
            seen.add(item["canonical"])
    if not added:
        return None
    if len(added) > MAX_ADDITIONS:
        raise Refused("Too many new notations to verify in one editor save.")
    receipt = {"id": secrets.token_hex(16), "project_key": project_key, "path": path,
               "before_sha256": hashlib.sha256(before).hexdigest(),
               "after_sha256": hashlib.sha256(after).hexdigest(),
               "added": added, "at": time.time()}
    return sign_receipt(receipt)


class EditTokens:
    """Single-use page capabilities; no token is persisted or sent to the model."""
    def __init__(self, *, clock=time.monotonic, lifetime=600, limit=512):
        self.clock, self.lifetime, self.limit = clock, lifetime, limit
        self._tokens = OrderedDict()
        self._lock = threading.Lock()

    def issue(self, project_key: str, path: str, content: bytes) -> dict:
        digest = hashlib.sha256(content).hexdigest()
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._tokens[token] = (project_key, path, digest, now + self.lifetime)
            while self._tokens and (len(self._tokens) > self.limit
                    or next(iter(self._tokens.values()))[3] < now):
                self._tokens.popitem(last=False)
        return {"token": token, "before_sha256": digest}

    def consume(self, token, project_key: str, path: str, before: bytes, after: bytes):
        if not isinstance(token, str):
            return False, None
        with self._lock:
            evidence = self._tokens.pop(token, None)
        if (not evidence or evidence[:3] != (project_key, path, hashlib.sha256(before).hexdigest())
                or evidence[3] < self.clock()):
            return False, None
        return True, make_receipt(project_key, path, before, after)

    def discard(self, token) -> None:
        if isinstance(token, str):
            with self._lock:
                self._tokens.pop(token, None)
