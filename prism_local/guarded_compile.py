"""API builds run on disposable inputs, behind an enforced OS write boundary.

The whole Build (including its log readers) runs inside the sandbox. Wrapping only
its commands would let an output symlink make the host read private state later.
No source is copied back. Custom commands return diagnostics only.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import build
import registry
from fsutil import SKIP_DIRS, is_metadata, replace_file
from projectconfig import Config

SANDBOX = Path("/usr/bin/sandbox-exec")
MAX_FILE = 50 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_FILES = 3000
MAX_RESULT = 8 * 1024 * 1024
GENERATED = build.CLEAN | {".synctex", ".fdb_latexmk"}
# A conservative publication restriction, not a complete TeX semantic analyser.
# Inspect every copied input: TeX/Lua can load code with a misleading extension.
PUBLICATION_DYNAMIC = re.compile(
    rb"\\(?:csname|catcode|mathcode|delcode|lccode|uccode|sfcode|char|mathchar|Uchar|Umathchar|"
    rb"chardef|mathchardef|scantokens|directlua|latelua|luaescapestring|luafunction|luabytecode|"
    rb"openout|openin|write|read|shipout|special|let|futurelet|def|gdef|edef|xdef|expandafter|"
    rb"afterassignment|aftergroup|toks|newtoks|every[a-z]+|pdfextension|pdfliteral|pdfobj|"
    rb"pdfximage|pdfrefximage|XeTeXpicfile|newcommand|renewcommand|providecommand|"
    rb"DeclareRobustCommand|newenvironment|renewenvironment|InputIfFileExists|inputiffileexists|"
    rb"import|subimport|includefrom|subincludefrom|RequirePackage|LoadClass)(?![A-Za-z])", re.I)
SCRIPT_SUFFIXES = {".lua", ".texlua", ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".sh",
                   ".bash", ".zsh", ".fish", ".pl", ".pm", ".rb", ".ps1", ".bat", ".cmd"}


def _private_paths() -> list[Path]:
    home = Path.home()
    paths = [registry.state_dir(), home / ".ssh", home / ".aws", home / ".codex",
             home / ".agents", home / ".config", home / ".prism-local"]
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "AWS_SHARED_CREDENTIALS_FILE"):
        if os.environ.get(name):
            paths.append(Path(os.environ[name]))
    return [p.expanduser().resolve() for p in paths]


def _profile(scratch: Path, *, denied=()) -> str:
    """Read tools/system resources, write only this disposable directory, no network."""
    code = Path(build.__file__).resolve().parent
    reads = [scratch, code, Path(sys.prefix), Path(sys.base_prefix),
             Path(sys.executable).resolve().parent, Path("/System"), Path("/Library"),
             Path("/usr"), Path("/opt"), Path("/private/etc"), Path("/etc"),
             Path.home() / "texlive", Path.home() / ".TinyTeX",
             Path.home() / "Library" / "TinyTeX", Path.home() / "Library" / "Fonts"]
    quote = lambda p: json.dumps(str(p), ensure_ascii=False)
    rules = ["(version 1)", "(deny default)", "(deny network*)",
             "(allow process-exec process-fork)", "(allow sysctl-read)",
             "(allow file-read-metadata)",
             # macOS dyld opens the root directory itself while finding its cache.
             '(allow file-read* (literal "/"))',
             "(allow file-read* " + " ".join(f"(subpath {quote(p)})" for p in reads) + ")",
             '(allow file-read* (literal "/dev/null") (literal "/dev/urandom") '
             '(literal "/dev/random"))',
             f"(allow file-write* (subpath {quote(scratch)})(literal \"/dev/null\"))"]
    secrets = [*_private_paths(), *denied]
    secrets += [code / name for name in (".git", ".codex", ".agents", ".aws")]
    rules.append("(deny file-read* " + " ".join(f"(subpath {quote(p)})" for p in secrets) + ")")
    return "\n".join(rules)


def _environment(scratch: Path) -> dict[str, str]:
    # Build's normal environment contains provider credentials. None reach a compiler.
    original = build.build_env()
    env = {k: v for k, v in original.items()
           if k in {"PATH", "LANG", "TZ"} or k.startswith("LC_")}
    cache = scratch / "tmp"
    cache.mkdir(exist_ok=True)
    env.update(TMPDIR=str(cache), TMP=str(cache), TEMP=str(cache),
               TEXMFVAR=str(cache / "texmf-var"), TEXMFCONFIG=str(cache / "texmf-config"),
               TEXMFCACHE=str(cache / "texmf-cache"))
    return env


_PROBE = r"""
import errno, pathlib, socket, sys
work, private, outside = map(pathlib.Path, sys.argv[1:])
(work / 'allowed').write_text('ok')
blocked = []
def network():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
for operation in (lambda: outside.write_text('bad'), lambda: private.read_bytes(),
                  network):
    try:
        operation()
    except OSError as error:
        blocked.append(error.errno in (errno.EPERM, errno.EACCES))
    else:
        blocked.append(False)
sys.exit(0 if all(blocked) else 3)
"""


@functools.lru_cache(maxsize=1)
def _availability() -> tuple[bool, str]:
    if sys.platform != "darwin" or not SANDBOX.is_file():
        return False, "Guarded API compilation needs a verified macOS sandbox-exec runtime."
    try:
        with tempfile.TemporaryDirectory(prefix="prism-compile-probe-") as name:
            root = Path(name).resolve()
            work = root / "work"
            work.mkdir()
            private, outside = root / "private", root / "outside"
            private.write_text("private")
            profile = _profile(work, denied=[private])
            runner = build.Runner(timeout=10)
            rc, output = runner.run([str(SANDBOX), "-p", profile, sys.executable, "-I", "-B",
                                     "-c", _PROBE, str(work), str(private), str(outside)],
                                    work, _environment(work))
            if rc == 0 and (work / "allowed").read_text() == "ok" and not outside.exists():
                return True, "macOS sandbox write, private-read and network restrictions verified."
            detail = output.strip()[:300] or f"probe exit {rc}"
            return False, "Guarded API compilation is unavailable: " + detail
    except (OSError, ValueError, build.Stopped) as exc:
        return False, "Guarded API compilation is unavailable: " + str(exc)


def availability() -> dict:
    supported, reason = _availability()
    return {"supported": supported, "reason": reason}


def _read_plain(root: Path, rel: str, limit=MAX_FILE) -> bytes:
    path = root / rel
    if (not rel or Path(rel).as_posix() != rel or Path(rel).is_absolute() or ".." in Path(rel).parts
            or "\\" in rel or any(p.is_symlink() for p in (path, *path.parents))
            or not path.resolve().is_relative_to(root)):
        raise ValueError("A compiler file is not a plain project file: " + rel)
    if os.name == "nt":
        from fsutil import read_windows_file
        return read_windows_file(path, limit)[0]
    # A detached sandboxed child could still change the copy. Open each directory
    # without following links, so a rename after the check cannot redirect a host read.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = Path(rel).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= limit:
                raise ValueError("A compiler file is not a bounded regular file: " + rel)
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
    finally:
        os.close(directory)
    fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(raw) > limit or fields(before) != fields(after) or fields(current) != fields(after):
        raise ValueError("A compiler file changed while being read: " + rel)
    return raw


def _inputs(project, cfg, copy: Path | None = None) -> dict[str, str]:
    """Capture regular visible inputs only; never enter linked/hidden/state folders."""
    found, total = {}, 0
    state = registry.state_dir().resolve()
    for directory, children, names in os.walk(project.root, followlinks=False):
        top = Path(directory)
        children[:] = sorted(n for n in children
                             if not n.startswith(".") and n not in SKIP_DIRS and n.casefold() != "research"
                             and not is_metadata(Path(n)) and not (top / n).is_symlink()
                             and not (top / n).resolve().is_relative_to(state)
                             and (top / n).relative_to(project.root).as_posix() != cfg.outdir
                             and not project.excluded((top / n).relative_to(project.root).as_posix(), cfg))
        for name in sorted(names):
            path = top / name
            rel = path.relative_to(project.root).as_posix()
            if (name.startswith(".") or is_metadata(Path(rel)) or project.excluded(rel, cfg)
                    or path.is_symlink() or path.resolve().is_relative_to(state)
                    or Path(name).suffix.lower() in GENERATED
                    or name in {f"{Path(cfg.main).stem}.pdf", f"{Path(cfg.main).stem}.synctex.gz"}
                    or name.endswith(".prism-build.json")):
                continue
            raw = _read_plain(project.root, rel)
            total += len(raw)
            if len(found) >= MAX_FILES or total > MAX_TOTAL:
                raise ValueError("The guarded compiler input limit was exceeded.")
            found[rel] = hashlib.sha256(raw).hexdigest()
            if copy is not None:
                dst = copy / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(raw)
                dst.chmod(0o600 | (path.stat().st_mode & 0o111))
    if cfg.main not in found:
        raise ValueError("The main document is missing or is not a safe compiler input.")
    return found


_DRIVER = r"""
import json, os, pathlib, subprocess, sys
sys.path.insert(0, sys.argv[1])
import build
request, result = map(pathlib.Path, sys.argv[2:])
data = json.loads(request.read_text())
# Every compiler stays in the outer Runner's group. Its Stop also ends descendants.
build.start = lambda argv, **kwargs: subprocess.Popen(argv, **kwargs)
build.build_env = lambda: dict(os.environ)
b = build.Build(pathlib.Path(data.pop('root')), **data)
r = b.run()
result.write_text(json.dumps(r))
"""


def _mtime(path: Path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _publish(project, cfg, copy: Path) -> list[str]:
    """Only the selected PDF and log; source and dependency evidence never return."""
    out = project.root / cfg.outdir
    if any(p.is_symlink() for p in (out, *out.parents)) or not out.resolve().is_relative_to(project.root):
        raise ValueError("The build output folder is redirected; no artifacts were published.")
    pending = []
    for suffix in (".pdf", ".log"):
        name = Path(cfg.main).stem + suffix
        relative = (Path(cfg.outdir) / name).as_posix()
        source = copy / relative
        if not source.exists() and not source.is_symlink():
            continue
        raw = _read_plain(copy, relative)
        target = out / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError("A build output is redirected; no artifacts were published.")
        pending.append((target, raw))
    # Read/validate all candidates before the first atomic replacement.
    for target, raw in pending:
        replace_file(target, raw)
    return [p.relative_to(project.root).as_posix() for p, _ in pending]


def _check_output(cfg):
    parts = Path(cfg.outdir).parts
    if (is_metadata(Path(cfg.outdir)) or any(p.startswith(".") or p.casefold() == "research" for p in parts)
            or not (cfg.root / cfg.outdir).resolve().is_relative_to(cfg.root)):
        raise ValueError("The configured build output is not a safe artifact folder.")


def _publication_reason(cfg, copy, manifest):
    """Keep dynamic builds diagnostic-only even when all input hashes stay unchanged."""
    if cfg.custom.get("draft"):
        return "Custom build commands have no supported artifact publication proof."
    if cfg.shell_escape:
        return "Shell escape is enabled."
    if build.detect_engine(copy, cfg.main, cfg.engine)[0] == "lualatex":
        return "The LuaLaTeX engine can execute code that constructs mathematical output."
    for rel in manifest:
        path = copy / rel
        if path.suffix.lower() in SCRIPT_SUFFIXES or path.stat().st_mode & 0o111:
            return f"Script or executable input: {rel}."
        marker = PUBLICATION_DYNAMIC.search(_read_plain(copy, rel))
        if marker:
            return f"Dynamic TeX marker {marker.group().decode('ascii')} in {rel}."
    return None


def guarded_compile(project, *, clean=False, cancel=None) -> dict:
    """Compile the API's draft in an isolated copy, or fail closed with diagnostics."""
    if not project._build_lock.acquire(blocking=False):
        return {"busy": True}
    t0 = time.monotonic()
    runner = None
    selected = None
    finished = threading.Event()
    watcher = None
    result = {"busy": False, "mode": "draft", "main": None, "exit": 127,
              "output": "", "diagnostics": [], "cancelled": False, "timed_out": False,
              "pdf_mtime": None, "pdf_updated": False, "guarded": True, "published": []}
    try:
        cfg = project.config
        before = _mtime(cfg.pdf)
        result.update(main=cfg.main, pdf_mtime=before)
        support = availability()
        if not support["supported"]:
            result["output"] = support["reason"]
            result["guard_unavailable"] = True
            return result
        if cancel is not None and cancel.is_set():
            result.update(exit=-1, cancelled=True, output="The build was stopped.")
            return result
        with tempfile.TemporaryDirectory(prefix="prism-guarded-build-") as temporary:
            scratch = Path(temporary).resolve()
            copy = scratch / "project"
            copy.mkdir()
            with project.files_lock:
                shown = project.config
                main = project.stale_document() or shown.main
                cfg = Config(project.root, main=main)
                _check_output(cfg)
                selected = cfg.main
                before = _mtime(cfg.pdf)
                result.update(main=cfg.main, pdf_mtime=before)
                manifest = _inputs(project, cfg, copy)
            publication_reason = _publication_reason(cfg, copy, manifest)
            if publication_reason:
                result["publication_restricted"] = publication_reason
            command = cfg.custom.get("draft")
            request = {"root": str(copy), "main": cfg.main, "outdir": cfg.outdir, "mode": "draft",
                       "builder": cfg.builder, "engine": cfg.engine,
                       "command": cfg.expand(command) if command else None,
                       "clean": clean, "shell_escape": cfg.shell_escape}
            (scratch / "request.json").write_text(json.dumps(request))
            profile = _profile(scratch, denied=[project.root])
            runner = build.Runner()
            project._running = runner
            project._running_pdf = (cfg.pdf, before)
            if cancel is not None:
                def watch():
                    while not finished.wait(0.05):
                        if cancel.is_set():
                            runner.stop()
                            return
                watcher = threading.Thread(target=watch, name="guarded-compile-stop", daemon=True)
                watcher.start()
            try:
                rc, output = runner.run([str(SANDBOX), "-p", profile, sys.executable, "-I", "-B", "-c",
                                         _DRIVER, str(Path(build.__file__).resolve().parent),
                                         str(scratch / "request.json"), str(scratch / "result.json")],
                                        copy, _environment(scratch))
            except build.Stopped:
                result.update(exit=-1, cancelled=not runner.timed_out, timed_out=runner.timed_out,
                              output="The build timed out." if runner.timed_out else "The build was stopped.")
                return result
            if rc != 0:
                result.update(exit=rc, output=output or "The isolated build could not run.")
                return result
            payload = json.loads(_read_plain(scratch, "result.json", MAX_RESULT))
            if (not isinstance(payload, dict) or type(payload.get("exit")) is not int
                    or not isinstance(payload.get("output"), str)
                    or type(payload.get("cancelled")) is not bool
                    or type(payload.get("timed_out")) is not bool
                    or not isinstance(payload.get("diagnostics"), list)
                    or any(not isinstance(d, dict) for d in payload["diagnostics"])):
                raise ValueError("The isolated build returned an invalid result.")
            result.update({k: payload[k] for k in ("exit", "output", "diagnostics", "cancelled",
                                                  "timed_out", "engine", "engine_reason", "builder",
                                                  "passes", "steps") if k in payload})
            if cfg.error:
                result["output"] = f"prism-local: {cfg.error}\n" + result["output"]
            if publication_reason:
                result["output"] += ("\nGuarded compile returns diagnostics only: " + publication_reason
                                     + " PDF and log artifacts were not published.")
            if runner.stopped.is_set() or cancel is not None and cancel.is_set():
                result.update(exit=-1, cancelled=True)
            if result["exit"] != 0 or result["cancelled"] or result["timed_out"]:
                return result
            if publication_reason:
                return result
            # Never trust compiler-produced sources, even when its OS boundary held.
            copied = SimpleNamespace(root=copy, excluded=project.excluded)
            if _inputs(copied, cfg) != manifest:
                raise ValueError("Compiler inputs changed in the isolated copy; no artifacts were published.")
            with project.files_lock:
                if runner.stopped.is_set() or cancel is not None and cancel.is_set():
                    result.update(exit=-1, cancelled=True)
                    return result
                if _inputs(project, cfg) != manifest:
                    raise ValueError("Project inputs changed during compilation; no artifacts were published.")
                result["published"] = _publish(project, cfg, copy)
                if result["published"]:
                    project.turn.server_wrote(result["published"])
                result["pdf_mtime"] = _mtime(cfg.pdf)
                result["pdf_updated"] = any(p.endswith(".pdf") for p in result["published"])
            return result
    except (OSError, ValueError, TypeError) as exc:
        result.update(exit=126, output=result.get("output", "") + "\nGuarded compile: " + str(exc))
        return result
    finally:
        finished.set()
        if watcher is not None:
            watcher.join(timeout=1)
        project._running = project._running_pdf = None
        try:
            if selected is not None:
                project.show(selected)
        finally:
            project._build_lock.release()
            result["seconds"] = round(time.monotonic() - t0, 1)
