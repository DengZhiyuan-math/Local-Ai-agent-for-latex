"""Building a LaTeX project so that it works for any document, on any machine.

The built-in builder needs only the TeX engines of MiKTeX or TeX Live: no Perl, no
latexmk. One build

1. picks the engine: "engine" in prism.json, else a `% !TEX program = …` line at the
   top of the main file, else the packages the preamble loads (fontspec, xeCJK, ctex,
   unicode-math … need XeLaTeX; luacode, luatexja … need LuaLaTeX), else pdflatex;
2. runs it, runs bibtex or biber and makeindex (index, glossaries, nomenclature) when
   their input changed, and runs the engine again until the files it reads back
   (.aux, .toc, .bbl, …) stop changing, at most MAX_PASSES times;
3. if a file left by an earlier build (a truncated .aux, say) breaks the first pass,
   deletes those files and starts again, once.

Every tool runs with paths relative to the project, so folders with spaces or with
Chinese names work. Programs written in Perl (biber, latexmk) cannot enter a folder
whose name the Windows code page cannot spell; they run through an ASCII junction.

"builder" in prism.json can choose latexmk or Tectonic instead, and "build" can give
commands of your own. Every kind of build gets the same time limit, stop button and
error report.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from fsutil import SKIP_DIRS
from proc import NO_WINDOW, kill_tree

MAX_PASSES = 5            # engine runs per build (at most 3 while the document has errors)
TIMEOUT = 600.0           # seconds for one whole build

ENGINES = {"pdflatex": "pdflatex", "pdftex": "pdflatex", "latex": "pdflatex",
           "pdflatex-dev": "pdflatex", "xelatex": "xelatex", "xetex": "xelatex",
           "xelatex-dev": "xelatex", "lualatex": "lualatex", "luatex": "lualatex",
           "lualatex-dev": "lualatex"}
LATEXMK_FLAG = {"pdflatex": "-pdf", "xelatex": "-pdfxe", "lualatex": "-pdflua"}
MISSING = {
    "biber": "biber was not found; biblatex needs it. Install it with your TeX distribution's "
             "package manager, or load biblatex with backend=bibtex.",
    "bibtex": "bibtex was not found. It comes with MiKTeX and TeX Live; check that their bin "
              "folder is on PATH, then restart Prism.",
    "makeindex": "makeindex was not found. It comes with MiKTeX and TeX Live; check that their "
                 "bin folder is on PATH, then restart Prism.",
}


class BuildError(Exception):
    """The build cannot go on; the message says why and what to do."""


class Stopped(Exception):
    """The build was stopped, or ran out of time."""


# ---------------------------------------------------------------- engine

MAGIC_RE = re.compile(r"^\s*%\s*!\s*tex\s+(?:ts-)?program\s*=\s*([\w-]+)", re.I | re.M)
LOAD_RE = re.compile(r"\\(?:usepackage|RequirePackage|documentclass|LoadClass)\s*"
                     r"(?:\[[^\]]*\])?\s*\{([^}]*)\}")
XETEX = {"fontspec", "unicode-math", "xecjk", "polyglossia", "mathspec", "xltxtra", "xunicode",
         "ctex", "ctexart", "ctexrep", "ctexbook", "ctexbeamer", "xecjkfntef", "zhspacing",
         "bidi", "xgreek"}
LUATEX = {"luacode", "luatexja", "luatexja-fontspec", "luatexja-preset", "ltjsarticle",
          "ltjsbook", "ltjsreport", "ltjarticle", "ltjbook", "luaotfload", "luatextra", "lua-ul",
          "luamplib", "luacolor", "selnolig", "luaquotes", "lua-visual-debug", "chickenize",
          "luatodonotes"}
SHELL_ESCAPE = ("minted", "svg", "gnuplottex")


def strip_comments(text: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", text)


def _loaded(tex: str) -> set[str]:
    return {n.strip() for m in LOAD_RE.finditer(tex) for n in m.group(1).split(",") if n.strip()}


def _main_text(root: Path, main: str) -> str | None:
    try:
        return (root / main).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def detect_engine(root: Path, main: str, configured: str | None = None) -> tuple[str, str]:
    """(engine, why): the TeX engine the document needs."""
    if configured:
        eng = ENGINES.get(str(configured).strip().lower())
        if eng:
            return eng, "prism.json"
    text = _main_text(root, main)
    if text is None:
        return "pdflatex", "default"
    m = MAGIC_RE.search("\n".join(text.splitlines()[:30]))
    if m and m.group(1).lower() in ENGINES:
        return ENGINES[m.group(1).lower()], "% !TEX program"
    pre = strip_comments(text.split("\\begin{document}", 1)[0])
    names = _loaded(pre)
    for n in list(names):                 # the project's own classes and packages
        for ext in (".cls", ".sty"):
            f = root / (n + ext)
            if f.is_file():
                names |= _loaded(strip_comments(f.read_text(encoding="utf-8", errors="replace")))
    lua = sorted(n for n in names if n.lower() in LUATEX)
    if lua or "\\directlua" in pre:
        return "lualatex", lua[0] if lua else "\\directlua"
    xe = sorted(n for n in names if n.lower() in XETEX)
    if xe:
        return "xelatex", xe[0]
    return "pdflatex", "default"


def needs_shell_escape(root: Path, main: str) -> str | None:
    """A package in the preamble that only works with shell escape, if any."""
    text = _main_text(root, main)
    if text is None:
        return None
    names = _loaded(strip_comments(text.split("\\begin{document}", 1)[0]))
    return next((n for n in SHELL_ESCAPE if n in names), None)


# ---------------------------------------------------------------- platform help

def ansi_safe(path: Path) -> bool:
    """Whether Windows programs that use the ANSI code page (Perl) can spell `path`."""
    if os.name != "nt":
        return True
    try:
        str(path).encode("mbcs", "strict")
        return True
    except UnicodeEncodeError:
        return False


def ascii_alias(root: Path) -> Path:
    """`root`, or a junction to it that the Windows code page can spell.

    Perl programs (biber, latexmk) fail in a folder such as D:\\论文 on a Windows whose
    code page cannot spell it. A junction under the temp folder gives them a plain
    name for the same folder. Only the junction is ever removed, never its target."""
    if ansi_safe(root):
        return root
    bases = [tempfile.gettempdir(), os.environ.get("PUBLIC", ""), r"C:\Users\Public"]
    base = next((Path(b) for b in bases if b and ansi_safe(Path(b)) and Path(b).is_dir()), None)
    if base is None:
        return root
    alias = base / "prism-local-links" / hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
    try:
        if alias.exists() and alias.resolve() == root.resolve():
            return alias
        if os.path.lexists(alias):
            os.rmdir(alias)                   # a junction to somewhere else: only the link goes
        alias.parent.mkdir(parents=True, exist_ok=True)
        import _winapi
        _winapi.CreateJunction(str(root), str(alias))
    except (OSError, ImportError, AttributeError):
        return root
    return alias if alias.exists() else root


def git_bash() -> str | None:
    """Git for Windows' bash. Plain `bash` on Windows is usually WSL's, which would run
    a build in Linux, far from MiKTeX and the Windows PATH."""
    cands = []
    git = shutil.which("git")
    if git:                                   # <Git>/cmd/git.exe -> <Git>/bin/bash.exe
        top = Path(git).resolve().parent.parent
        cands += [top / "bin" / "bash.exe", top / "usr" / "bin" / "bash.exe"]
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if os.environ.get(var):
            cands.append(Path(os.environ[var]) / "Git" / "bin" / "bash.exe")
    if os.environ.get("LOCALAPPDATA"):
        cands.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git" / "bin" / "bash.exe")
    return next((str(p) for p in cands if p.is_file()), None)


def shell_argv(cmd: str) -> list[str]:
    if os.name != "nt":
        return ["bash", "-c", cmd]
    bash = git_bash()
    if not bash:
        raise BuildError("prism.json gives this build as a shell command, which needs Git for "
                         "Windows (its bash). Install Git for Windows, or write the command as a "
                         "list: [\"program\", \"argument\", …].")
    return [bash, "-c", cmd]


def git_perl_dir() -> str | None:
    """Where Git for Windows keeps its perl.exe, if it is installed.

    latexmk is a Perl script. MiKTeX does not ship Perl, and on Windows Perl is rarely on
    PATH, yet Git for Windows brings one along. latexmk uses it when no other Perl is found.
    """
    if os.name != "nt" or shutil.which("perl"):
        return None
    cands = []
    git = shutil.which("git")
    if git:                                   # <Git>/cmd/git.exe -> <Git>/usr/bin
        cands.append(Path(git).resolve().parent.parent / "usr" / "bin")
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if os.environ.get(var):
            cands.append(Path(os.environ[var]) / "Git" / "usr" / "bin")
    if os.environ.get("LOCALAPPDATA"):
        cands.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Git" / "usr" / "bin")
    return next((str(d) for d in cands if (d / "perl.exe").is_file()), None)


# ---------------------------------------------------------------- running programs

class Runner:
    """Runs the programs of one build. `stop()` (from another thread) ends the running
    program and every later one; so does the build's time limit."""

    def __init__(self, timeout: float = TIMEOUT):
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.stopped = threading.Event()
        self.timed_out = False
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def run(self, argv: list[str], cwd: Path, env: dict | None = None) -> tuple[int, str]:
        with self.lock:
            if self.stopped.is_set():
                raise Stopped()
            # No stdin: a program that asks something gets end-of-file instead of waiting.
            proc = self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                **NO_WINDOW)
        try:
            out, _ = proc.communicate(timeout=max(1.0, self.deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            self.timed_out = True
            self.stopped.set()
            kill_tree(proc)
            out, _ = proc.communicate()
        finally:
            with self.lock:
                self.proc = None
        if self.stopped.is_set():
            raise Stopped()
        return proc.returncode, out.decode("utf-8", "replace")

    def stop(self) -> None:
        with self.lock:
            self.stopped.set()
            proc = self.proc
        if proc is not None:
            kill_tree(proc)


# ---------------------------------------------------------------- the output folder

# Files an engine writes and reads back on its next run. In a separate output folder
# every file counts except these; in the project folder itself only the FEEDBACK ones.
NOT_FEEDBACK = {".log", ".pdf", ".gz", ".synctex", ".fls", ".fdb_latexmk", ".blg", ".ilg",
                ".glg", ".alg", ".nlg", ".xdv", ".dvi", ".bcf", ".xml", ".idx", ".glo", ".acn",
                ".nlo", ".json", ".png", ".jpg", ".jpeg", ".eps", ".tmp", ".prism-tmp"}
FEEDBACK = {".aux", ".toc", ".lof", ".lot", ".loa", ".lol", ".out", ".bbl", ".ind", ".gls",
            ".acr", ".nls", ".nav", ".snm", ".vrb", ".thm", ".brf", ".ent", ".tdo", ".loe",
            ".cpt", ".xref", ".mw"}
CLEAN = FEEDBACK | {".blg", ".bcf", ".idx", ".ilg", ".glo", ".glg", ".acn", ".alg", ".nlo",
                    ".nlg", ".fls", ".fdb_latexmk", ".xdv", ".dvi", ".log"}


def _walk(top: Path):
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
        for f in filenames:
            yield Path(dirpath, f)


def feedback_state(out: Path, in_root: bool) -> dict[str, str]:
    state = {}
    for p in _walk(out):
        suf = p.suffix.lower()
        if (suf in FEEDBACK) if in_root else (suf not in NOT_FEEDBACK):
            sha = _file_sha(p)
            if sha:
                state[p.relative_to(out).as_posix()] = sha
    return state


def clean(out: Path, stem: str, in_root: bool) -> int:
    """Delete what earlier builds left in the output folder, except the PDF and its
    SyncTeX data. Only generated kinds of files are touched, never sources."""
    removed = 0
    for p in _walk(out):
        suf = p.suffix.lower()
        if (suf in CLEAN and not (in_root and suf == ".bbl")) or \
                p.name in (f"{stem}.ist", f"{stem}.run.xml", f"{stem}.prism-build.json"):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def mirror_dirs(root: Path, out: Path, outdir: str) -> None:
    """Create the output subfolders that \\include'd files write their .aux into
    (TeX Live's engines cannot create folders)."""
    if out == root:
        return
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
                       and (rel / d).as_posix() != outdir]
        if rel.parts and any(f.endswith(".tex") for f in filenames):
            (out / rel).mkdir(parents=True, exist_ok=True)


def _sha(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha1(data).hexdigest()


def _file_sha(p: Path) -> str | None:
    try:
        return _sha(p.read_bytes())
    except OSError:
        return None


def joined_log(text: str) -> list[str]:
    """The log's lines with TeX's hard wrapping (at 79 columns) undone."""
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        msg = lines[i]
        while len(lines[i]) >= 79 and i + 1 < len(lines):
            i += 1
            msg += lines[i]
        out.append(msg)
        i += 1
    return out


ERROR_LINE_RE = re.compile(r"^(?:! |(?:[A-Za-z]:)?[^:\n]*?\.\w+:\d+: )")


def broken_feedback(log_text: str, out: Path, root: Path) -> bool:
    """Whether the first error came from a file an earlier build left behind, such as
    an .aux cut short when that build was interrupted."""
    stack: list[str] = []
    for line in joined_log(log_text):
        if ERROR_LINE_RE.match(line) or line.startswith("Runaway argument"):
            if re.search(r"File ended while scanning use of \\[A-Za-z]*@", line):
                return True        # a macro only .aux files use, such as \@newl@bel
            cur = next((s for s in reversed(stack) if s), "")
            if not cur or Path(cur).suffix.lower() not in FEEDBACK:
                continue           # "Runaway argument?" comes before the message itself
            try:
                full = (Path(cur) if os.path.isabs(cur) else root / cur).resolve()
                return out.resolve() in full.parents
            except OSError:
                return False
        for tok in re.finditer(r"\(([^\s()]*)|\)", line):
            if tok.group(0) == ")":
                if stack:
                    stack.pop()
            else:
                stack.append(tok.group(1))
    return False


# ---------------------------------------------------------------- reading the results

DIAG_RE = re.compile(r"^(error|warning): (.+?):(\d+): (.*)$")                        # Tectonic
FLE_RE = re.compile(r"^((?:[A-Za-z]:)?[^:\n]*?\.(?:tex|sty|cls|ltx|dtx|tikz|bbl)):(\d+): (.*)$")
LOG_WARN_RE = re.compile(r"(LaTeX|Package \S+) Warning: (.*?)(?: on input line (\d+))?\.?$")


def project_file(root: Path, name: str, base: Path | None = None) -> str | None:
    """`name` (as a TeX program printed it) as a project-relative path, if it is a
    file of the project."""
    name = name.strip().replace("\\", "/")
    name = name[2:] if name.startswith("./") else name
    if not name:
        return None
    for cand in (name, name + ".tex"):
        p = Path(cand) if os.path.isabs(cand) else (base or root) / cand
        if p.is_file():
            try:
                return p.resolve().relative_to(root).as_posix()
            except (ValueError, OSError):   # outside the project (a TeX distribution file)
                return None
    return None


def parse_stdout(out: str, root: Path) -> list[dict]:
    diags, seen = [], set()
    for line in out.splitlines():
        m = DIAG_RE.match(line.strip())
        if m:
            sev, name, ln, msg = m.groups()
        else:
            m = FLE_RE.match(line.strip())
            if not m:
                continue
            sev, (name, ln, msg) = "error", m.groups()
        key = (sev, name, ln, msg)
        if key in seen:           # engines repeat errors on every rerun
            continue
        seen.add(key)
        diags.append({"severity": sev, "file": project_file(root, name),
                      "line": int(ln), "message": msg})
    return diags


def parse_log_warnings(log: Path, root: Path) -> list[dict]:
    """LaTeX warnings (undefined refs/citations, ...) from the main .log file.

    TeX's log records the current file by '(' path ... ')' nesting. We track a
    stack of the project files we recognise; this is a heuristic, good enough to
    attribute warnings to a source file.
    """
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    diags, seen, stack = [], set(), []
    for msg in joined_log(text):
        for tok in re.finditer(r"\(([^\s()]*)|\)", msg):
            if tok.group(0) == ")":
                if stack:
                    stack.pop()
            else:
                name = tok.group(1)
                # Unresolvable file names (e.g. main.bbl, package files) are kept
                # as "?name" so warnings inside them are not misattributed.
                rel = project_file(root, name) if name else None
                stack.append(rel or ("?" + name if "." in name or "/" in name else None))
        m = LOG_WARN_RE.search(msg)
        if m and "Rerun" not in msg:
            cur = next((s for s in reversed(stack) if s), None)
            cur = cur if cur and cur.endswith(".tex") and cur[0] != "?" else None
            ln = int(m.group(3)) if m.group(3) else None
            text_msg = m.group(2).strip()
            key = (cur, ln, text_msg)
            if key not in seen:
                seen.add(key)
                diags.append({"severity": "warning", "file": cur if ln else None,
                              "line": ln, "message": text_msg})
    return diags


def _diag(sev: str, message: str, file: str | None = None, line: int | None = None) -> dict:
    return {"severity": sev, "file": file, "line": line, "message": message}


def parse_bibtex(out: str, root: Path, cwd: Path) -> list[dict]:
    diags = []
    for line in out.splitlines():
        m = re.match(r"(.*)---line (\d+) of file (.+)$", line)
        if m:
            diags.append(_diag("error", "BibTeX: " + m.group(1).strip(),
                               project_file(root, m.group(3), cwd), int(m.group(2))))
        elif line.startswith("Warning--"):
            diags.append(_diag("warning", "BibTeX: " + line[9:].strip()))
        elif re.match(r"I couldn't open|I found no|Illegal, another|Sorry---", line):
            diags.append(_diag("error", "BibTeX: " + line.strip()))
    return diags


def parse_biber(out: str) -> list[dict]:
    return [_diag("error" if m.group(1) == "ERROR" else "warning", "Biber: " + m.group(2).strip())
            for m in re.finditer(r"^(ERROR|WARN) - (.*)$", out, re.M)]


# ---------------------------------------------------------------- one build

AUX_RE = re.compile(r"\\(citation|bibdata|bibstyle|@input)\{([^}]*)\}")


def _read_aux(out: Path, rel: str, seen: set) -> tuple[set, list, list]:
    """\\citation keys, \\bibdata and \\bibstyle of an .aux and the ones it \\@input's."""
    if rel in seen:
        return set(), [], []
    seen.add(rel)
    try:
        text = (out / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set(), [], []
    cites, data, style = set(), [], []
    for kind, arg in AUX_RE.findall(text):
        if kind == "citation":
            cites.update(k.strip() for k in arg.split(",") if k.strip())
        elif kind == "bibdata":
            data += [d.strip() for d in arg.split(",") if d.strip()]
        elif kind == "bibstyle":
            style.append(arg.strip())
        else:
            c, d, s = _read_aux(out, arg.strip(), seen)
            cites |= c
            data += d
            style += s
    return cites, data, style


class Build:
    """One build of a project. `run()` does it; `stop()` may be called from another thread."""

    def __init__(self, root: Path, main: str, outdir: str, mode: str = "draft", *,
                 builder: str = "auto", engine: str | None = None,
                 command: list[str] | str | None = None, clean: bool = False,
                 shell_escape: bool = False, timeout: float = TIMEOUT):
        self.root, self.main, self.outdir, self.mode = root, main, outdir, mode
        self.out = (root / outdir).resolve()
        self.stem = Path(main).stem
        self.strict = mode == "strict"
        self.want_builder, self.want_engine, self.command = builder, engine, command
        self.clean, self.shell_escape = clean, shell_escape
        self.runner = Runner(timeout)
        self.parts: list[str] = []          # what the Output panel shows
        self.last = ""                      # the output of the last engine pass
        self.diags: list[dict] = []
        self.steps: list[str] = []
        self.notes: list[str] = []
        self.passes = 0
        self.engine: str | None = None
        self.engine_reason = ""
        self.builder = ""

    def stop(self) -> None:
        self.runner.stop()

    # ------------------------------------------------------------ public
    def run(self) -> dict:
        rc = 0
        try:
            self.engine, self.engine_reason = detect_engine(self.root, self.main, self.want_engine)
            self.builder = self._choose_builder()
            self.out.mkdir(parents=True, exist_ok=True)
            if self.clean:
                n = clean(self.out, self.stem, self.out == self.root)
                self.notes.append(f"Recompiling from scratch: removed {n} files earlier builds left.")
            rc = self._builtin() if self.builder == "builtin" else self._command()
        except BuildError as e:
            rc = 127
            self.parts.append(str(e))
            self.diags.append(_diag("error", str(e)))
        except Stopped:
            rc = -1
        stopped = self.runner.stopped.is_set()
        if stopped:
            self.notes.append(f"The build took longer than {int(self.runner.timeout)} seconds and "
                              "was stopped." if self.runner.timed_out else "The build was stopped.")
            if self.runner.timed_out:          # where TeX was: often an endless loop
                tail = "\n".join(self._log_text().splitlines()[-40:])
                self.parts.append(f"=== the last lines of {self.stem}.log ===\n{tail}")
        head = "".join(f"prism-local: {n}\n" for n in self.notes)
        return {"exit": rc, "output": head + ("\n" if head else "") + "\n".join(self.parts),
                # A stopped build's half-written log is full of warnings that mean nothing.
                "diagnostics": [] if stopped else self._diagnostics(), "engine": self.engine,
                "engine_reason": self.engine_reason, "builder": self.builder,
                "passes": self.passes, "steps": self.steps,
                "cancelled": stopped and not self.runner.timed_out,
                "timed_out": self.runner.timed_out}

    # ------------------------------------------------------------ choosing
    def _choose_builder(self) -> str:
        if self.command:
            return "custom"
        want = str(self.want_builder or "auto").lower()
        if want in ("latexmk", "tectonic"):
            if not shutil.which(want):
                raise BuildError(f"prism.json asks for {want}, which was not found on PATH.")
            return want
        if want not in ("auto", "builtin"):
            raise BuildError(f"prism.json: unknown builder {self.want_builder!r} "
                             "(use \"auto\", \"latexmk\" or \"tectonic\").")
        if shutil.which(self.engine):
            return "builtin"
        if shutil.which("tectonic"):
            self.notes.append(f"{self.engine} was not found, so Tectonic builds this project.")
            return "tectonic"
        raise BuildError(f"{self.engine} was not found. Install MiKTeX (miktex.org) or TeX Live, "
                         "or add its bin folder to PATH, then restart Prism.")

    def _step(self, name: str, argv: list[str], cwd: Path, env: dict | None = None,
              keep: bool = True, tool: bool = False) -> tuple[int, str]:
        """Run one program. A missing tool (bibtex …) is reported and the build goes on;
        a missing engine ends it."""
        self.steps.append(name)
        try:
            rc, out = self.runner.run(argv, cwd, env)
        except (FileNotFoundError, NotADirectoryError):
            prog = Path(str(argv[0])).stem.lower()
            msg = MISSING.get(prog) or (f"{Path(str(argv[0])).name} was not found. Install it, "
                                        "or add its folder to PATH, then restart Prism.")
            if not tool:
                raise BuildError(msg) from None
            self.diags.append(_diag("error", msg))
            self.parts.append(f"=== {name} ===\n{msg}")
            return 127, msg
        if keep:
            self.parts.append(f"=== {name} (exit {rc}) ===\n{out}")
        return rc, out

    # ------------------------------------------------------------ built-in
    def _engine_argv(self) -> list[str]:
        argv = [shutil.which(self.engine) or self.engine, "-synctex=1",
                "-interaction=nonstopmode", "-file-line-error", f"-output-directory={self.outdir}"]
        if self.shell_escape:
            argv.append("-shell-escape")
        if self.strict:
            argv.append("-halt-on-error")
        return argv + [self.main]

    def _builtin(self) -> int:
        root, out, in_root = self.root, self.out, self.out == self.root
        mirror_dirs(root, out, self.outdir)
        state = self._load_state()
        argv, retried, rc = self._engine_argv(), False, 0
        while True:
            before = feedback_state(out, in_root)
            rc, self.last = self._step(f"{self.engine} (pass {self.passes + 1})", argv, root,
                                       keep=False)
            self.passes += 1
            log = self._log_text()
            if self.passes == 1 and rc != 0 and not retried and not self.clean \
                    and broken_feedback(log, out, root):
                clean(out, self.stem, in_root)
                state, retried, self.passes = {}, True, 0
                self.notes.append("A file left by an earlier build broke the first pass; removed "
                                  "what earlier builds left and started again.")
                continue
            if rc != 0 and self.strict:
                break
            self._tools(state, log)
            if feedback_state(out, in_root) == before:
                break
            if self.passes >= (MAX_PASSES if rc == 0 else 3):
                self.notes.append(f"Stopped after {self.passes} passes although the document "
                                  "was still changing; cross-references may be off.")
                break
        self._save_state(state)
        self.parts.insert(0, f"=== {self.engine}, pass {self.passes} of {self.passes} "
                             f"(exit {rc}) ===\n{self.last}")
        pkg = needs_shell_escape(root, self.main)
        if pkg and not self.shell_escape:
            self.diags.append(_diag("warning", f"The package {pkg} needs shell escape. Set "
                                    "\"shell_escape\": true in prism.json if you trust this "
                                    "document (it lets the document run programs)."))
        return rc

    def _tools(self, state: dict, log: str) -> None:
        """bibtex/biber and makeindex, each only when its input changed."""
        out, root = self.out, self.root
        rel_root = os.path.relpath(root, out)
        # bibtex, for every .aux that names databases (the main one, multibib, bibunits …)
        for aux in sorted(p for p in _walk(out) if p.suffix == ".aux"):
            try:
                if "\\bibdata{" not in aux.read_text(encoding="utf-8", errors="replace"):
                    continue
            except OSError:
                continue
            target = aux.relative_to(out).with_suffix("").as_posix()
            cites, data, style = _read_aux(out, aux.relative_to(out).as_posix(), set())
            bbl = out / f"{target}.bbl"
            if not cites:              # nothing cited: no bibliography rather than an empty one
                if bbl.exists() and out != root:
                    bbl.unlink()
                continue
            bibs = [_file_sha(root / (d if d.endswith(".bib") else d + ".bib")) for d in data]
            bsts = [_file_sha(root / (s + ".bst")) for s in style]
            sig = _sha(json.dumps([sorted(cites), data, style, bibs, bsts]))
            if state.get("bibtex:" + target) == sig and bbl.exists():
                continue
            # bibtex runs in the output folder; the project folder, relative to it, is
            # where the .bib and .bst files are (relative, so any folder name works)
            env = {**os.environ, **{v: rel_root + os.pathsep + os.environ.get(v, "")
                                    for v in ("BIBINPUTS", "BSTINPUTS")}}
            rc, text = self._step(f"bibtex {target}", ["bibtex", target], out, env, tool=True)
            found = parse_bibtex(text, root, out) if rc != 127 else []
            self.diags += found
            if rc <= 1:                # 1: warnings only
                state["bibtex:" + target] = sig
            elif rc != 127 and not any(d["severity"] == "error" for d in found):
                self.diags.append(_diag("error", f"bibtex {target} failed (exit {rc}); see Output."))
        # biber (biblatex)
        for bcf in sorted(out.glob("*.bcf")):
            try:
                text = bcf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            sources = re.findall(r"<bcf:datasource[^>]*>\s*([^<]+?)\s*</bcf:datasource>", text)
            sig = _sha(json.dumps([_sha(text), [_file_sha(root / s) for s in sources]]))
            if state.get("biber:" + bcf.stem) == sig and (out / f"{bcf.stem}.bbl").exists():
                continue
            rel_out = os.path.relpath(out, root)
            rc, btext = self._step(f"biber {bcf.stem}",
                                   ["biber", f"--input-directory={rel_out}",
                                    f"--output-directory={rel_out}", bcf.stem],
                                   ascii_alias(root), tool=True)
            found = parse_biber(btext) if rc != 127 else []
            self.diags += found
            if rc == 0:
                state["biber:" + bcf.stem] = sig
            elif rc != 127 and not any(d["severity"] == "error" for d in found):
                self.diags.append(_diag("error", f"biber failed (exit {rc}); see Output."))
        # makeindex: indexes, glossaries, nomenclature
        for key, argv, src, dst in self._index_jobs(log):
            sig = _file_sha(out / src)
            if sig is None or (state.get(key) == sig and (out / dst).exists()):
                continue
            rc, itext = self._step(" ".join(argv), argv, out, tool=True)
            if rc == 0:
                state[key] = sig
            elif rc != 127:
                self.diags.append(_diag("error", f"{' '.join(argv)} failed (exit {rc}); see Output."))
            self.diags += [_diag("warning", "makeindex: " + ln[3:].strip())
                           for ln in itext.splitlines() if ln.startswith("!! ")]

    def _index_jobs(self, log: str) -> list[tuple[str, list[str], str, str]]:
        out, stem = self.out, self.stem
        # imakeidx runs makeindex itself (with its own options) when shell escape allows it
        by_tex = set(re.findall(r"runsystem\(makeindex\b[^)]*?(\S+\.idx)",
                                "\n".join(joined_log(log))))
        jobs = [(f"index:{p.name}", ["makeindex", p.name], p.name, p.stem + ".ind")
                for p in sorted(out.glob("*.idx")) if p.name not in by_tex]
        try:
            aux = (out / f"{stem}.aux").read_text(encoding="utf-8", errors="replace")
        except OSError:
            aux = ""
        ist = re.search(r"\\@istfilename\{([^}]*)\}", aux)
        for typ, lg, dst, src in re.findall(r"\\@newglossary\{([^}]*)\}\{([^}]*)\}\{([^}]*)\}"
                                            r"\{([^}]*)\}", aux):
            if not ist:
                continue
            if ist.group(1).endswith(".xdy"):
                self.diags.append(_diag("warning", "This glossary uses xindy, which the built-in "
                                        "builder does not run. Use the makeindex style of "
                                        "glossaries, or \"builder\": \"latexmk\"."))
                continue
            jobs.append((f"glossary:{typ}", ["makeindex", "-s", ist.group(1), "-t", f"{stem}.{lg}",
                                             "-o", f"{stem}.{dst}", f"{stem}.{src}"],
                         f"{stem}.{src}", f"{stem}.{dst}"))
        if (out / f"{stem}.nlo").exists():
            jobs.append(("nomencl", ["makeindex", f"{stem}.nlo", "-s", "nomencl.ist", "-o",
                                     f"{stem}.nls"], f"{stem}.nlo", f"{stem}.nls"))
        return jobs

    # ------------------------------------------------------------ latexmk, Tectonic, own commands
    def _command(self) -> int:
        root, env, cwd = self.root, {**os.environ}, self.root
        if self.builder == "custom":
            argv = shell_argv(self.command) if isinstance(self.command, str) else list(self.command)
        elif self.builder == "tectonic":
            argv = [shutil.which("tectonic"), "-o", self.outdir, "--keep-logs",
                    "--keep-intermediates", "--synctex"]
            argv += ([] if self.strict else ["-Z", "continue-on-errors"]) + [self.main]
        else:                                           # latexmk
            argv = [shutil.which("latexmk"), LATEXMK_FLAG[self.engine], "-synctex=1",
                    "-interaction=nonstopmode", "-file-line-error", f"-outdir={self.outdir}",
                    "-halt-on-error" if self.strict else "-f"]
            argv += (["-shell-escape"] if self.shell_escape else []) + [self.main]
            cwd = ascii_alias(root)                     # Perl: a path the code page can spell
            if os.name == "nt" and not shutil.which("perl"):
                perl = git_perl_dir()
                if not perl:
                    raise BuildError("latexmk needs Perl, and none was found. Install Strawberry "
                                     "Perl (strawberryperl.com) or Git for Windows, or remove "
                                     "\"builder\": \"latexmk\" from prism.json.")
                env["PATH"] = env.get("PATH", "") + os.pathsep + perl   # appended: shadows nothing
        # bibtex runs inside the output folder under latexmk; let it find the project's files.
        for v in ("BIBINPUTS", "BSTINPUTS"):
            env[v] = os.pathsep.join([str(cwd), os.environ.get(v, "")])
        rc, text = self._step(Path(str(argv[0])).stem, argv, cwd, env)
        self.passes = 1
        self.diags += parse_stdout(text, root)
        return rc

    # ------------------------------------------------------------ bookkeeping
    def _log_text(self) -> str:
        try:
            return (self.out / f"{self.stem}.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _load_state(self) -> dict:
        try:
            data = json.loads((self.out / f"{self.stem}.prism-build.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        p = self.out / f"{self.stem}.prism-build.json"
        try:
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass

    def _diagnostics(self) -> list[dict]:
        diags = parse_stdout(self.last, self.root) if self.last else []
        diags += [d for d in self.diags if d not in diags]
        known = {d["message"] for d in diags}
        diags += [d for d in parse_log_warnings(self.out / f"{self.stem}.log", self.root)
                  if d["message"] not in known]
        return diags
