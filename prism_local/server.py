#!/usr/bin/env python3
"""prism-local: a local web studio for LaTeX projects.

Editor + PDF preview with SyncTeX + an AI agent panel, served from
127.0.0.1 with the Python standard library only.

    python3 prism_local/server.py [PROJECT_DIR] [--port 8765] [--no-browser]
                                  [--exit-when-idle] [--port-tries N] [--ready-file F]

Besides the builds (build.py) it runs only read-only `git status` / `git diff`
and, for the agent panel, the chosen AI backend (agent.py, backends.py): the
Claude Code or Codex CLI, or calls to an OpenAI-compatible API such as DeepSeek.
Optional per-project settings live in PROJECT_DIR/prism.json (see README).
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent import NO_WINDOW, AgentManager  # noqa: E402
from fsutil import EDITABLE_SUFFIXES, SKIP_DIRS, with_line_ends_of, write_bytes  # noqa: E402
from presence import Presence  # noqa: E402
from texutil import group, plain_text  # noqa: E402
import build  # noqa: E402
import httpbase  # noqa: E402
import registry  # noqa: E402

MAX_FILES = 3000
BUILD_LOCK = threading.Lock()
SAVE_LOCK = threading.Lock()

STANDARD_THEOREMS = ["theorem", "proposition", "lemma", "corollary", "definition",
                     "remark", "example"]


class Config:
    """Project settings: defaults, overridden by PROJECT_DIR/prism.json. A prism.json
    that cannot be used is reported (`error`) and the defaults apply instead."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        problems: list[str] = []
        cfg = self.root / "prism.json"
        self.stamp = cfg.stat().st_mtime_ns if cfg.is_file() else None
        data: dict = {}
        if self.stamp is not None:
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("it must hold a JSON object")
            except (OSError, ValueError) as e:
                data = {}
                problems.append(f"prism.json cannot be used ({e}); the defaults apply.")
        self.main = str(data.get("main") or self._guess_main())
        outdir = str(data.get("outdir") or "build").replace("\\", "/").strip("/") or "build"
        if Path(outdir).is_absolute() or ".." in Path(outdir).parts:
            problems.append("outdir must be a folder inside the project; build is used.")
            outdir = "build"
        self.outdir = outdir
        lists = {k: data.get(k) for k in ("files", "exclude")}
        for k, v in lists.items():
            if v is not None and not (isinstance(v, list) and all(isinstance(g, str) for g in v)):
                problems.append(f"{k} must be a list of patterns; it is ignored.")
                lists[k] = None
        self.files = lists["files"]                    # optional list of globs
        self.exclude = lists["exclude"] or []
        self.builder = str(data.get("builder") or "auto")       # auto (built-in), latexmk, tectonic
        self.engine = data.get("engine") or None                 # pdflatex, xelatex, lualatex
        if self.engine is not None and str(self.engine).lower() not in build.ENGINES:
            problems.append(f"unknown engine {self.engine!r} (use pdflatex, xelatex or "
                            "lualatex); it is chosen from the document instead.")
            self.engine = None
        self.shell_escape = data.get("shell_escape") is True
        # Commands of your own per mode: an argv list, or a shell string (see build.py).
        cmds = data.get("build") or {}
        if not isinstance(cmds, dict):
            problems.append("build must map modes to commands; it is ignored.")
            cmds = {}
        self.custom = {k: v for k, v in cmds.items() if isinstance(v, (str, list)) and v}
        self.modes = [m for m in ("draft", "strict", "check") if m != "check" or m in self.custom]
        self.error = " ".join(p if p.startswith("prism.json") else "prism.json: " + p
                              for p in problems) or None
        stem = Path(self.main).stem
        out = self.root / self.outdir
        self.pdf = out / f"{stem}.pdf"
        self.log = out / f"{stem}.log"
        self.synctex = out / f"{stem}.synctex.gz"

    def _guess_main(self) -> str:
        if (self.root / "main.tex").is_file():
            return "main.tex"
        cands = [p for p in sorted(self.root.glob("*.tex")) if not p.name.startswith("._")
                 and "\\documentclass" in p.read_text(encoding="utf-8", errors="replace")[:5000]]
        return cands[0].name if cands else "main.tex"

    def expand(self, cmd: list[str] | str) -> list[str] | str:
        sub = lambda a: str(a).replace("{main}", self.main).replace("{outdir}", self.outdir)  # noqa: E731
        return sub(cmd) if isinstance(cmd, str) else [sub(a) for a in cmd]

    def describe(self, mode: str) -> str:
        """What a build in `mode` runs, for the Compile menu."""
        cmd = self.custom.get(mode)
        if cmd:
            return cmd if isinstance(cmd, str) else " ".join(self.expand(cmd))
        how = "stops at the first error" if mode == "strict" else "continues after errors"
        engine = self.engine or "chosen from the document"
        if self.builder in ("latexmk", "tectonic"):
            return f"{self.builder}; engine {engine}; {how}"
        return (f"built-in: engine {engine}, then bibtex/biber and makeindex as needed, "
                f"rerun until stable; {how}")


CFG: Config = None  # type: ignore[assignment]
ROOT: Path = Path.cwd()


def set_root(root: Path) -> None:
    global CFG, ROOT
    CFG = Config(root)
    ROOT = CFG.root


def refresh_config() -> None:
    """Load prism.json again when it changed, so a new engine or outdir applies at once."""
    try:
        stamp = (ROOT / "prism.json").stat().st_mtime_ns
    except OSError:
        stamp = None
    if stamp != CFG.stamp:
        set_root(ROOT)


# ---------------------------------------------------------------- files

def _excluded(rel: str) -> bool:
    parts = rel.split("/")
    if any(p.startswith(".") or p in SKIP_DIRS for p in parts[:-1]):
        return True
    if parts[-1].startswith("._") or rel.startswith(CFG.outdir + "/"):
        return True
    return any(fnmatch.fnmatch(rel, g) for g in CFG.exclude)


def resolve(rel: str) -> Path:
    """Map a project-relative path to an editable file, or raise ValueError."""
    if not rel or rel.startswith("/") or "\\" in rel:
        raise ValueError("bad path")
    p = (ROOT / rel).resolve()
    if ROOT not in p.parents:
        raise ValueError("outside project")
    r = p.relative_to(ROOT).as_posix()
    if p.suffix not in EDITABLE_SUFFIXES or _excluded(r):
        raise ValueError("not an editable file")
    return p


def list_files() -> list[str]:
    seen: set[str] = set()
    if CFG.files:
        for g in CFG.files:
            for p in ROOT.glob(g):
                rel = p.relative_to(ROOT).as_posix()
                if p.is_file() and p.suffix in EDITABLE_SUFFIXES and not _excluded(rel):
                    seen.add(rel)
    else:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            reld = Path(dirpath).relative_to(ROOT).as_posix()
            reld = "" if reld == "." else reld + "/"
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                                 and d not in SKIP_DIRS and reld + d != CFG.outdir)
            for f in filenames:
                rel = reld + f
                if Path(f).suffix in EDITABLE_SUFFIXES and not _excluded(rel):
                    seen.add(rel)
            if len(seen) > MAX_FILES:
                break
    return sorted(seen, key=lambda s: (s != CFG.main, s.count("/") == 0, s))


def git(*args: str, timeout: float = 10) -> subprocess.CompletedProcess:
    """Read-only git in the project. --no-optional-locks: the editor asks every two
    seconds, and must never hold .git/index.lock when a git command of yours starts."""
    return subprocess.run(["git", "--no-optional-locks", *args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          **NO_WINDOW)


_GIT_PREFIX: dict = {}


def git_prefix() -> str:
    """Where the project sits in its repository: "" at the top, "examples/minimal/" …"""
    if ROOT not in _GIT_PREFIX:
        try:
            _GIT_PREFIX[ROOT] = git("rev-parse", "--show-prefix").stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    return _GIT_PREFIX[ROOT]


def git_status() -> dict[str, str]:
    """{project-relative path: status} of the files git reports as changed."""
    try:
        out = git("status", "--porcelain", "-z", "-uall", "--", ".").stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    # -z: unquoted paths (any characters), relative to the repository's top, "XY path";
    # a rename or copy is followed by its old path.
    prefix, st, entries, i = git_prefix(), {}, out.split("\0"), 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue
        if "R" in e[:2] or "C" in e[:2]:
            i += 1
        if e[3:].startswith(prefix):
            st[e[3:][len(prefix):]] = e[:2].strip() or "M"
    return st


def mtime(p: Path) -> float:
    return p.stat().st_mtime_ns / 1e9


def save_file(rel: str, content: str, base_mtime: float | None, force: bool) -> tuple[dict, int]:
    """Write an editor buffer to disk, unless the file changed on disk since the editor
    loaded it (a conflict). The file keeps its line ends (the editor sends \\n)."""
    p = resolve(rel)
    with SAVE_LOCK:     # check-then-write must not interleave with another save
        if p.exists() and base_mtime is not None and abs(mtime(p) - base_mtime) > 1e-6 \
                and not force:
            return {"conflict": True, "mtime": mtime(p)}, 409
        write_bytes(p, with_line_ends_of(content, p).encode("utf-8"))
        return {"ok": True, "mtime": mtime(p)}, 200


# ---------------------------------------------------------------- symbols

LABEL_RE = re.compile(r"\\label\{([^}]+)\}")
BEGIN_RE = re.compile(r"\\begin\{([A-Za-z*]+)\}(?:\[([^\]]*)\])?")
# \section{…}, \section*{…}, \section[short]{long}; the title is read with its braces paired.
SECTION_RE = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*\{")
INPUT_RE = re.compile(r"\\(?:input|include)\{([^}]+)\}")
MACRO_RE = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand|DeclareMathOperator|DeclarePairedDelimiter)\*?"
    r"\s*\{?\\([A-Za-z]+)\}?")
BIBKEY_RE = re.compile(r"^\s*@(\w+)\s*\{\s*([^,\s]+)\s*,", re.M)
# \newtheorem{thm}{Theorem}[section], \newtheorem{lem}[thm]{Lemma}, llncs, mdframed, tcolorbox …
NEWTHEOREM_RE = re.compile(r"\\(?:newtheorem|spnewtheorem|newmdtheoremenv|newtcbtheorem)\*?\s*"
                           r"\{([^}]+)\}\s*(?:\[[^\]]*\]\s*)?(?:\{([^}]*)\})?")
# thmtools: \declaretheorem[name=Theorem, numberwithin=section]{thm}
DECLARETHEOREM_RE = re.compile(r"\\declaretheorem\*?\s*(?:\[([^\]]*)\])?\s*\{([^}]+)\}")


def strip_comment(line: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", line)


def document_order() -> list[str]:
    """Project .tex files in the order the main file \\input's them (depth first)."""
    order: list[str] = []

    def visit(rel: str) -> None:
        if rel in order:
            return
        order.append(rel)
        try:
            text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for line in text.splitlines():
            for m in INPUT_RE.finditer(strip_comment(line)):
                child = resolve_tex_name(m.group(1).strip())
                if child:
                    visit(child)

    visit(CFG.main)
    rest = [f for f in list_files() if f.endswith(".tex") and f not in order]
    return order + rest


def theorem_envs(files: list[str]) -> dict[str, str]:
    """Theorem-like environments and the names they print: {"thm": "Theorem", …}."""
    envs: dict[str, str] = {}
    for rel in files:
        if Path(rel).suffix not in (".tex", ".sty", ".cls"):
            continue
        for raw in (ROOT / rel).read_text(encoding="utf-8", errors="replace").splitlines():
            line = strip_comment(raw)
            for m in NEWTHEOREM_RE.finditer(line):
                envs.setdefault(m.group(1).strip(), (m.group(2) or m.group(1)).strip())
            for m in DECLARETHEOREM_RE.finditer(line):
                name = m.group(2).strip()
                opt = re.search(r"(?:^|,)\s*(?:name|title)\s*=\s*\{?([^,}]+)", m.group(1) or "")
                envs.setdefault(name, opt.group(1).strip() if opt else name.capitalize())
    return envs or {e: e.capitalize() for e in STANDARD_THEOREMS}


def symbols() -> dict:
    labels, outline, macros = [], [], []
    files = list_files()
    envs = theorem_envs(files)
    outline_envs = set(envs)
    for rel in document_order():
        if not (ROOT / rel).is_file():
            continue
        env = None
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        for n, raw in enumerate(text.splitlines(), 1):
            line = strip_comment(raw)
            m = SECTION_RE.search(line)
            if m:
                g = group(line, m.end() - 1)          # a title that runs on: this line's part
                outline.append({"file": rel, "line": n, "kind": m.group(1),
                                "title": plain_text(g[0] if g else line[m.end():])})
                env = m.group(1)
            for m in BEGIN_RE.finditer(line):
                if m.group(1) in outline_envs:
                    outline.append({"file": rel, "line": n, "kind": m.group(1),
                                    "title": plain_text(m.group(2) or "")})
                env = m.group(1)
            for m in MACRO_RE.finditer(line):
                macros.append({"name": m.group(1), "file": rel, "line": n})
            for m in LABEL_RE.finditer(line):
                labels.append({"label": m.group(1), "file": rel, "line": n,
                               "kind": env or ""})
    keys = []
    for rel in (f for f in files if f.endswith(".bib")):
        bib = ROOT / rel
        text = bib.read_text(encoding="utf-8", errors="replace")
        for m in BIBKEY_RE.finditer(text):
            if m.group(1).lower() not in ("string", "preamble", "comment"):
                keys.append({"key": m.group(2), "type": m.group(1).lower(),
                             "file": bib.relative_to(ROOT).as_posix(),
                             "line": text.count("\n", 0, m.start()) + 1})
    return {"labels": labels, "bibkeys": keys, "outline": outline, "macros": macros,
            "environments": list(envs), "env_titles": envs}


# ---------------------------------------------------------------- build

def resolve_tex_name(name: str) -> str | None:
    return build.project_file(ROOT, name)


RUNNING: dict = {"build": None}      # the build in progress, for /api/build/stop


def run_build(mode: str, clean: bool = False) -> dict:
    """One build (see build.py). Only one runs at a time."""
    if mode not in CFG.modes:
        return {"busy": False, "exit": 127, "mode": mode, "seconds": 0, "diagnostics": [],
                "output": f"There is no '{mode}' build.", "pdf_mtime": None}
    if not BUILD_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        t0 = time.time()
        before = mtime(CFG.pdf) if CFG.pdf.exists() else None
        cmd = CFG.custom.get(mode)
        b = build.Build(ROOT, CFG.main, CFG.outdir, mode, builder=CFG.builder, engine=CFG.engine,
                        command=CFG.expand(cmd) if cmd else None, clean=clean,
                        shell_escape=CFG.shell_escape)
        RUNNING["build"] = b
        r = b.run()
        if CFG.error:
            r["output"] = f"prism-local: {CFG.error}\n" + r["output"]
        pdf = mtime(CFG.pdf) if CFG.pdf.exists() else None
        return {**r, "busy": False, "mode": mode, "seconds": round(time.time() - t0, 1),
                "pdf_mtime": pdf, "pdf_updated": pdf is not None and pdf != before}
    finally:
        RUNNING["build"] = None
        BUILD_LOCK.release()


def stop_build() -> bool:
    b = RUNNING["build"]
    if b is not None:
        b.stop()
    return b is not None


# ---------------------------------------------------------------- synctex

SP_TO_BP = 72.0 / 72.27 / 65536.0     # scaled points -> PDF points
REC_RE = re.compile(r"^([\[(hvxkg$])(\d+),(\d+):(-?\d+),(-?\d+)(?::(-?\d+),(-?\d+),(-?\d+))?")


class SyncTex:
    """Minimal reader for .synctex.gz files (pdfTeX, XeTeX, LuaTeX, Tectonic)."""

    def __init__(self) -> None:
        self.stamp = None
        self.inputs: dict[int, str] = {}
        self.recs: list[tuple] = []   # (page, kind, file, line, x, y, w, h, d) in bp

    def load(self) -> bool:
        if not CFG.synctex.exists():
            return False
        st = CFG.synctex.stat().st_mtime_ns
        if st == self.stamp:
            return True
        inputs, recs, page, unit, mag = {}, [], 0, 1.0, 1.0
        with gzip.open(CFG.synctex, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                c = line[:1]
                if c == "I" and line.startswith("Input:"):
                    tag, _, path = line[6:].rstrip("\n").partition(":")
                    if path:
                        try:
                            # pdfTeX writes paths relative to the working directory.
                            pp = Path(path) if os.path.isabs(path) else ROOT / path
                            rel = pp.resolve().relative_to(ROOT).as_posix()
                            inputs[int(tag)] = rel
                        except ValueError:
                            pass
                elif c == "{":
                    page = int(line[1:])
                elif line.startswith("Unit:"):
                    unit = float(line[5:])
                elif line.startswith("Magnification:"):
                    mag = float(line[14:]) / 1000.0
                elif c in "[(hvxkg$":
                    m = REC_RE.match(line)
                    if not m:
                        continue
                    f = int(m.group(2))
                    if f not in inputs:
                        continue
                    s = unit * mag * SP_TO_BP
                    w, h, d = (int(m.group(i)) * s if m.group(i) else 0.0
                               for i in (6, 7, 8))
                    recs.append((page, m.group(1), inputs[f], int(m.group(3)),
                                 int(m.group(4)) * s, int(m.group(5)) * s, w, h, d))
        self.inputs, self.recs, self.stamp = inputs, recs, st
        return True

    # Paragraph line boxes ('(') carry the line where the paragraph *ended*;
    # the fine records (glyph runs x, kerns k, glue g, math $, ...) carry the
    # line they were typeset from. So boxes locate the visual line, and fine
    # records supply the source line.
    FINE = "xkg$h"

    def _line_box(self, page, x, y):
        """Smallest wide hbox on `page` whose vertical extent contains y."""
        best = None
        for r in self.recs:
            if r[0] == page and r[1] == "(" and r[6] > 0 \
                    and r[5] - r[7] - 1 <= y <= r[5] + r[8] + 1 \
                    and r[4] - 1 <= x <= r[4] + r[6] + 1:
                if best is None or r[6] > best[6]:   # widest = the text line
                    best = r
        return best

    def forward(self, rel: str, line: int) -> dict | None:
        cands = [r for r in self.recs if r[2] == rel and r[3] > 0
                 and r[1] in self.FINE]
        if not cands:
            cands = [r for r in self.recs if r[2] == rel and r[3] > 0]
        if not cands:
            return None
        after = [r for r in cands if r[3] >= line]
        target = min(r[3] for r in after) if after else max(r[3] for r in cands)
        first = min((r for r in cands if r[3] == target),
                    key=lambda r: (r[0], r[5], r[4]))
        box = self._line_box(first[0], first[4], first[5])
        if box:
            return {"page": first[0], "x": box[4], "y": box[5] - box[7],
                    "w": box[6], "h": max(box[7] + box[8], 8.0), "line": target}
        return {"page": first[0], "x": first[4], "y": first[5] - 9,
                "w": 60.0, "h": 12.0, "line": target}

    def inverse(self, page: int, x: float, y: float) -> dict | None:
        box = self._line_box(page, x, y)
        on_page = [r for r in self.recs if r[0] == page and r[3] > 0]
        if box:
            base = box[5]
            row = [r for r in on_page if r[1] in self.FINE
                   and box[4] - 1 <= r[4] <= box[4] + box[6] + 1
                   and base - box[7] - 1 <= r[5] <= base + box[8] + 1]
            if row:
                left = [r for r in row if r[4] <= x + 0.5]
                r = max(left, key=lambda r: r[4]) if left else min(row, key=lambda r: r[4])
                return {"file": r[2], "line": r[3]}
        if not on_page:
            return None
        r = min(on_page, key=lambda r: (r[4] - x) ** 2 + 4 * (r[5] - y) ** 2)
        return {"file": r[2], "line": r[3]}


SYNC = SyncTex()
SYNC_LOCK = threading.Lock()
AGENT = AgentManager(lambda: ROOT, lambda: list_files(), resolve)
PRESENCE = Presence()


# ---------------------------------------------------------------- http

class Handler(httpbase.Handler):
    """The editor's requests. The checks every request passes are in httpbase.py."""

    presence = PRESENCE
    pages = {"/": "index.html", "/index.html": "index.html", "/viewer": "viewer.html"}

    def get(self, path, q):
        refresh_config()
        try:
            return self._get(path, q)
        except UnicodeDecodeError:
            return self._err(415, "This file is not UTF-8 text; prism-local edits UTF-8 files only.")
        except (ValueError, KeyError) as e:
            return self._err(400, str(e))
        except FileNotFoundError:
            return self._err(404, "file not found")
        except OSError as e:                 # e.g. the file is locked by another program
            return self._err(500, f"{type(e).__name__}: {e}")

    def _get(self, path, q):
        if path == "/api/ping":
            return self._json({"app": "prism-local", "root": str(ROOT), "pid": os.getpid(),
                               "pages": PRESENCE.count()})
        if path == "/api/pdfstat":
            return self._json({"mtime": mtime(CFG.pdf) if CFG.pdf.exists() else None})
        if path == "/pdf":
            if not CFG.pdf.exists():
                return self._err(404, "no PDF yet")
            return self._send(200, CFG.pdf.read_bytes(), "application/pdf")
        if path == "/api/tree":
            st = git_status()
            files = [{"path": f, "git": st.get(f, ""), "mtime": mtime(ROOT / f)}
                     for f in list_files()]
            return self._json({"root": ROOT.name, "files": files, "order": document_order(),
                               "pdf_mtime": mtime(CFG.pdf) if CFG.pdf.exists() else None})
        if path == "/api/config":
            return self._json({"main": CFG.main, "outdir": CFG.outdir, "modes": CFG.modes,
                               "builder": CFG.builder, "engine": CFG.engine, "error": CFG.error,
                               "build": {m: CFG.describe(m) for m in CFG.modes}})
        if path == "/api/agent/events":
            job = AGENT.jobs.get(int(q["job"]))
            if not job:
                return self._err(404, "unknown job")
            evs, done = job.wait_events(int(q.get("after", 0)), 20.0)
            return self._json({"events": evs, "done": done})
        if path == "/api/agent/commands":
            r = AGENT.commands(q.get("provider") or None, refresh=q.get("refresh") == "1")
            return self._json(r, 502 if "error" in r else 200)
        if path == "/api/agent/account":
            b = AGENT.backend(q.get("provider") or None)
            if b is None or not hasattr(b, "account"):
                return self._json({"account": None})
            return self._json(b.account(ROOT, fresh=q.get("fresh") == "1"))
        if path == "/api/agent/info":
            return self._json(AGENT.info())
        if path == "/api/symbols":
            return self._json(symbols())
        if path == "/api/file":
            p = resolve(q.get("path", ""))
            return self._json({"path": q["path"], "content": p.read_text(encoding="utf-8"),
                               "mtime": mtime(p)})
        if path == "/api/diff":
            if q.get("path"):
                resolve(q["path"])
            # --relative: paths as the project sees them, and nothing from outside it
            return self._json({"diff": git("diff", "--no-color", "--relative", "--",
                                           q.get("path") or ".", timeout=20).stdout})
        if path == "/api/synctex/forward":
            with SYNC_LOCK:
                if not SYNC.load():
                    return self._err(404, "no synctex data; build first")
                r = SYNC.forward(q["file"], int(q["line"]))
            return self._json(r or {"error": "no match"}, 200 if r else 404)
        if path == "/api/synctex/inverse":
            with SYNC_LOCK:
                if not SYNC.load():
                    return self._err(404, "no synctex data; build first")
                r = SYNC.inverse(int(q["page"]), float(q["x"]), float(q["y"]))
            return self._json(r or {"error": "no match"}, 200 if r else 404)
        return self._err(404, "not found")

    def post(self, path, body):
        refresh_config()
        try:
            return self._post(path, body)
        except (ValueError, KeyError) as e:
            return self._err(400, str(e))
        except OSError as e:                 # e.g. the file is locked by another program
            return self._err(500, f"{type(e).__name__}: {e}")

    def _post(self, path, body):
        if path == "/api/file":
            r, code = save_file(body["path"], str(body["content"]), body.get("base_mtime"),
                                bool(body.get("force")))
            return self._json(r, code)
        if path == "/api/agent":
            scope = body.get("scope") or None
            if scope is not None:
                if not isinstance(scope, list) or not all(isinstance(f, str) for f in scope):
                    raise ValueError("scope must be a list of files")
                for f in scope:
                    resolve(f)          # an editable project file, or ValueError
            r = AGENT.start(body["prompt"], body.get("session_id") or None,
                            body.get("mode", "ask"), body.get("model") or None,
                            body.get("effort") or None, scope, body.get("provider") or None)
            return self._json(r, 409 if "error" in r else 200)
        if path == "/api/home":
            r = registry.ensure_server(None)
            return self._json(r, 502 if "error" in r else 200)
        if path == "/api/agent/usage":
            return self._json(AGENT.probe_rate(body.get("provider") or None))
        if path == "/api/agent/stop":
            return self._json(AGENT.stop(int(body["job"])))
        if path == "/api/agent/undo":
            return self._json(AGENT.undo(int(body["turn"])))
        if path == "/api/build":
            r = run_build(str(body.get("mode", "draft")), bool(body.get("clean")))
            return self._json(r, 409 if r.get("busy") else 200)
        if path == "/api/build/stop":
            return self._json({"ok": stop_build()})
        return self._err(404, "not found")


def is_scratch(root: Path) -> bool:
    """A project in the system's temp folder (a test, a throwaway copy)."""
    import tempfile
    tmp = Path(tempfile.gettempdir()).resolve()
    return root == tmp or tmp in root.parents


def main():
    httpbase.quiet_stdio()
    ap = argparse.ArgumentParser(description="prism-local: local LaTeX studio "
                                 "(editor, PDF + SyncTeX, AI agent panel)")
    ap.add_argument("project", nargs="?", type=Path, default=Path.cwd(),
                    help="LaTeX project directory (default: current directory)")
    ap.add_argument("--port", type=int, default=8765, help="port (0: any free port)")
    ap.add_argument("--port-tries", type=int, default=1,
                    help="if the port is taken, try this many ports upwards")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--exit-when-idle", action="store_true",
                    help="exit shortly after the last editor or PDF page is closed")
    ap.add_argument("--ready-file", type=Path,
                    help="once listening, write {pid, port, url, root} as JSON to this file")
    ap.add_argument("--idle-timings", help=argparse.SUPPRESS)   # "first,grace,stale" for tests
    ap.add_argument("--root", type=Path, help=argparse.SUPPRESS)   # backwards compatible
    a = ap.parse_args()
    set_root(a.root or a.project)
    if a.idle_timings:
        f, g, st = (float(x) for x in a.idle_timings.split(","))
        PRESENCE.first_wait, PRESENCE.grace, PRESENCE.stale = f, g, st
    if not (ROOT / CFG.main).is_file():
        print(f"prism-local: warning: main file {CFG.main} not found in {ROOT}", file=sys.stderr)
    try:
        srv = httpbase.listen(Handler, a.port, a.port_tries)
    except OSError as e:
        sys.exit(f"prism-local: {e}")
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    stop = "closes after the last page" if a.exit_when_idle else "Ctrl-C to stop"
    print(f"prism-local: {url}\n  project: {ROOT}\n  main:    {CFG.main}\n"
          f"  builds:  {', '.join(CFG.modes)}\n  {stop}", flush=True)
    if CFG.error:
        print(f"  warning: {CFG.error}", flush=True)
    if not is_scratch(ROOT):
        registry.safe_touch(ROOT)       # list the project on the Home page
    if a.ready_file:
        httpbase.write_ready_file(a.ready_file, {"app": "prism-local", "pid": os.getpid(),
                                                 "port": port, "url": url, "root": str(ROOT)})
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if a.exit_when_idle:
        busy = lambda: BUILD_LOCK.locked() or AGENT.busy()  # noqa: E731
        threading.Thread(target=httpbase.idle_watchdog, daemon=True,
                         args=(srv, PRESENCE, busy, "build or agent turn")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if a.ready_file:            # first, so that a new server can start at once
            httpbase.remove_ready_file(a.ready_file)
        stop_build()
        AGENT.shutdown()
        httpbase.log("stopped")


if __name__ == "__main__":
    sys.exit(main())


