"""A project's settings: the defaults, overridden by its prism.json. The editor (what it
builds and shows) and the Home page (a card's title and thumbnail) both read them here, so
both name the same document and the same output folder.

    cfg = Config(root)              # prism.json as it is now; never raises
    cfg.main, cfg.outdir, cfg.pdf   # the document, its output folder, its PDF
    cfg.error                       # what in prism.json could not be used (the defaults apply)
"""
from __future__ import annotations

import json
from pathlib import Path

import build


def guess_main(root: Path) -> str | None:
    """The project's document when prism.json names none: main.tex, else the first .tex at
    the top with a \\documentclass (not one commented out). Files that cannot be read (a
    broken link, a cloud placeholder offline, a folder named x.tex) are passed over."""
    if (root / "main.tex").is_file():
        return "main.tex"
    for p in sorted(root.glob("*.tex")):
        if p.name.startswith("._"):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                if build.is_document(f.read(5000)):
                    return p.name
        except OSError:
            pass
    return None


def output_folder(value) -> tuple[str, str | None]:
    """prism.json's "outdir" as a folder inside the project, and why it was not usable."""
    outdir = str(value or "build").replace("\\", "/").strip("/") or "build"
    if Path(outdir).is_absolute() or ".." in Path(outdir).parts:
        return "build", "outdir must be a folder inside the project; build is used."
    return outdir, None


class Config:
    """Project settings: defaults, overridden by PROJECT_DIR/prism.json. A prism.json
    that cannot be used is reported (`error`) and the defaults apply instead. `main`: the
    document shown (one opened on its own), else the project's."""

    def __init__(self, root: Path, *, main: str | None = None):
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
        # The document prism.json names, or the one guessed; None: neither (no .tex yet).
        self.named_main: str | None = str(data.get("main") or "") or guess_main(self.root)
        self.project_main = self.named_main or "main.tex"
        self.main = main or self.project_main
        self.outdir, problem = output_folder(data.get("outdir"))
        if problem:
            problems.append(problem)
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
