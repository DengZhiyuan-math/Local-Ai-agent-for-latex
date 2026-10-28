"""Read-only provenance for existing builds; never compiles or changes source files.

An unavailable recorder is an unsupported result, never a reason to rebuild. Only
local inputs used by the compiler belong to the result's dependency set.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

SCHEMA = "prism-build-result/v1"
MAX_FILE = 4_000_000
MAX_BYTES = 32_000_000
MAX_FILES = 512
MAX_CANDIDATES = 16_384
# Only engine-written feedback is exempt from local input capture. Bibliography,
# index, glossary and nomenclature outputs require tool inputs absent from TeX FLS.
TEX_FEEDBACK = {".aux", ".toc", ".lof", ".lot", ".loa", ".lol", ".out", ".nav", ".snm", ".vrb"}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _plain(root, name, limit=MAX_FILE):
    path = root / name
    if (not name or Path(name).is_absolute() or Path(name).as_posix() != name
            or "\\" in name or ".." in Path(name).parts
            or any(p.is_symlink() for p in (path, *path.parents))
            or path.resolve(strict=True).is_relative_to(root) is False):
        raise ValueError("unsafe local input")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= limit:
            raise ValueError("unsupported local input")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(raw) > limit or fields(before) != fields(after) or fields(path.stat()) != fields(after):
        raise ValueError("input changed while reading")
    return _sha(raw), fields(after)


def _paths(root, outdir):
    """Bounded pre-build candidates; only actual recorder inputs are compared later."""
    found, total = {}, 0
    for directory, children, names in os.walk(root, followlinks=False):
        children[:] = sorted(n for n in children if n not in {".git", ".prism-local"}
                             and (Path(directory) / n).relative_to(root).as_posix() != outdir)
        for name in sorted(names, key=lambda n: (Path(n).suffix not in {".tex", ".sty", ".cls"}, n)):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if rel == "prism.json":
                continue
            # Unrelated unsupported files must not revoke a valid compiler input set.
            # A skipped file can never be accepted later: every recorded input still
            # needs a complete, unchanged pre-build capture in finish().
            if len(found) >= MAX_CANDIDATES:
                return found
            try:
                if path.stat().st_size + total > MAX_BYTES:
                    continue
                sha, identity = _plain(root, rel)
            except (OSError, ValueError, TypeError):
                continue
            total += identity[2]
            found[rel] = (sha, identity)
    return found


def _recorder(cfg, kind):
    suffix = ".prism-deps.mk" if kind == "tectonic" else ".fls"
    return cfg.root / cfg.outdir / (Path(cfg.main).stem + suffix)


def begin(cfg):
    attempt = {"id": uuid4().hex, "before": None, "error": None}
    try:
        if cfg.shell_escape or cfg.error or cfg.outdir != "build":
            raise ValueError("this project layout has no supported dependency proof")
        if any(p.is_symlink() for p in (cfg.root, *cfg.root.parents)):
            raise ValueError("linked project")
        for kind in ("tectonic", "builtin"):
            recorder = _recorder(cfg, kind)
            if recorder.is_symlink() or any(p.is_symlink() for p in recorder.parents):
                raise ValueError("linked build output")
            recorder.unlink(missing_ok=True)
        attempt["before"] = _paths(cfg.root, cfg.outdir)
    except (OSError, ValueError, TypeError) as exc:
        attempt["error"] = str(exc)
    return attempt


def _make_words(text):
    """Tectonic's Makefile rules use backslash quoting and line continuations."""
    text = text.replace("\\\r\n", "").replace("\\\n", "")
    # Ambiguous make variables or multiple rules are not guessed.
    if "$" in text or "#" in text or text.count(": ") != 1:
        raise ValueError("unsupported dependency rules")
    inputs = text.split(": ", 1)[1]
    tokens, token, escape = [], "", False
    for char in inputs:
        if escape:
            token += char
            escape = False
        elif char == "\\":
            escape = True
        elif char.isspace():
            if token:
                tokens.append(token)
                token = ""
        else:
            token += char
    if escape:
        raise ValueError("incomplete dependency escape")
    if token:
        tokens.append(token)
    return tokens


def _system_input(path):
    # Only known distribution installation trees are historical environment. Unknown
    # external local files are unsupported instead of being silently omitted.
    value = path.as_posix().lower()
    return (value.startswith(("/usr/local/texlive/", "/usr/share/texlive/", "/usr/share/texmf/",
                              "/opt/texlive/", "/library/tex/", "/opt/homebrew/share/texmf"))
            or "/miktex/" in value and ("/program files/" in value or "/appdata/" in value))


def _inputs(cfg, kind):
    recorder = _recorder(cfg, kind)
    relative = recorder.relative_to(cfg.root).as_posix()
    _plain(cfg.root, relative)
    text = recorder.read_text(encoding="utf-8")
    names = (_make_words(text) if kind == "tectonic" else
             [line[6:] for line in text.splitlines() if line.startswith("INPUT ")])
    found = set()
    for name in names:
        path = Path(name)
        path = path if path.is_absolute() else cfg.root / path
        # Do not resolve links before the local-input safety check.
        path = Path(os.path.abspath(path))
        if not path.is_relative_to(cfg.root):
            if kind != "tectonic" and _system_input(path):
                continue
            raise ValueError("external local dependency is unsupported")
        rel = path.relative_to(cfg.root).as_posix()
        if rel.startswith(cfg.outdir + "/"):
            if path.suffix in TEX_FEEDBACK:
                continue                     # compiler-generated feedback, not source
            raise ValueError("non-feedback output dependency has no reliable local source mapping")
        if rel == "prism.json" or ".git" in Path(rel).parts or ".prism-local" in Path(rel).parts:
            raise ValueError("unsupported local dependency")
        found.add(rel)
    if cfg.main not in found or not 1 <= len(found) <= MAX_FILES:
        raise ValueError("missing or excessive recorded inputs")
    return sorted(found)


def _directory(root):
    directory = root / ".prism-local" / "build-results"
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError("redirected build result")
    return directory


def _latest(root, main):
    return _directory(root) / (_sha(main.encode()) + ".json")


def finish(cfg, attempt, result):
    receipt = {"schema_version": SCHEMA, "status": "unknown", "id": attempt["id"],
               "main": cfg.main, "files": {}, "pdf_sha256": None,
               **{k: result.get(k) for k in ("exit", "cancelled", "timed_out", "pdf_updated",
                                             "engine", "builder", "diagnostics")}}
    try:
        diagnostics = result.get("diagnostics")
        if not isinstance(diagnostics, list) or any(not isinstance(d, dict) for d in diagnostics):
            raise ValueError("build diagnostics are unknown")
        if (type(result.get("exit")) is int and result["exit"] != 0
                or result.get("cancelled") is True or result.get("timed_out") is True
                or any(d.get("severity") == "error" for d in diagnostics)):
            receipt["status"] = "failed"
        else:
            if (type(result.get("exit")) is not int or result["exit"] != 0
                    or result.get("cancelled") is not False or result.get("timed_out") is not False
                    or attempt["error"] or not result.get("pdf_updated") or result.get("builder") not in {
                    "builtin", "latexmk", "tectonic"}):
                raise ValueError(attempt["error"] or "no complete supported build")
            names = _inputs(cfg, result["builder"])
            files = {}
            for name in names:
                value = _plain(cfg.root, name)
                if attempt["before"].get(name) != value:
                    raise ValueError("a compiler input changed during this build")
                files[name] = value[0]
            pdf, _ = _plain(cfg.root, cfg.pdf.relative_to(cfg.root).as_posix(), 700_000)
            receipt.update(status="complete", files=files, pdf_sha256=pdf)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        receipt["reason"] = str(exc)
    try:
        directory = _directory(cfg.root)
        directory.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()
        historical = directory / (attempt["id"] + ".json")
        historical.write_bytes(raw)
        temporary = directory / (attempt["id"] + ".tmp")
        temporary.write_bytes(raw)
        os.replace(temporary, _latest(cfg.root, cfg.main))
    except (OSError, ValueError):
        pass                         # failure to prove must never change the build itself


def read(cfg, *, building=False):
    status = {"schema_version": SCHEMA, "status": "building" if building else "missing",
              "main": cfg.main}
    if building:
        return status
    try:
        path = _latest(cfg.root, cfg.main)
        _plain(cfg.root, path.relative_to(cfg.root).as_posix(), 128_000)
        receipt = json.loads(path.read_bytes())
        if (not isinstance(receipt, dict)
                or receipt.get("schema_version") != SCHEMA or receipt.get("main") != cfg.main
                or not isinstance(receipt.get("id"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", receipt["id"])):
            raise ValueError("invalid build result")
        if receipt.get("status") == "complete":
            if (not isinstance(receipt.get("files"), dict) or cfg.main not in receipt["files"]
                    or not 1 <= len(receipt["files"]) <= MAX_FILES):
                raise ValueError("invalid dependency set")
            if any(_plain(cfg.root, name)[0] != sha for name, sha in receipt["files"].items()):
                return status | {"status": "source_changed"}
            if _plain(cfg.root, cfg.pdf.relative_to(cfg.root).as_posix(), 700_000)[0] != receipt["pdf_sha256"]:
                return status | {"status": "unknown"}
        return receipt
    except FileNotFoundError:
        return status
    except (OSError, ValueError, TypeError, KeyError):
        return status | {"status": "unknown"}
