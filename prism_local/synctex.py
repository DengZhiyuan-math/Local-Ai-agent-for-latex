"""SyncTeX: where a line of the sources is in the PDF, and which line is at a point of a page.

    records = INDEX.lookup(cfg.synctex, root)        # NoData: no file yet, or a damaged one
    records.forward("sections/intro.tex", 12)          # {"page", "x", "y", "w", "h", "line"}
    records.inverse(3, 100.0, 200.0, cfg.outdir, bib_files)   # {"file", "line"}

A minimal reader for .synctex.gz files (pdfTeX, XeTeX, LuaTeX, Tectonic). A file is read
once until it changes (its path, mtime and size); one being written (a click during a
build) or damaged is "build again", read once too. Records are never changed once read,
so queries need no lock.
"""
from __future__ import annotations

import gzip
import os
import re
import threading
import zlib
from pathlib import Path
from typing import Callable

SP_TO_BP = 72.0 / 72.27 / 65536.0     # scaled points -> PDF points
REC_RE = re.compile(r"^([\[(hvxkg$])(\d+),(\d+):(-?\d+),(-?\d+)(?::(-?\d+),(-?\d+),(-?\d+))?")
NO_DATA = "no synctex data; build first"


class NoData(Exception):
    """There is nothing to look up; the message says why and what to do."""


class Records:
    """One .synctex.gz, read: its records (page, kind, file, line, x, y, w, h, d) in bp,
    with project-relative files."""

    # Paragraph line boxes ('(') carry the line where the paragraph *ended*;
    # the fine records (glyph runs x, kerns k, glue g, math $, ...) carry the
    # line they were typeset from. So boxes locate the visual line, and fine
    # records supply the source line.
    FINE = "xkg$h"

    def __init__(self, root: Path, recs: list[tuple]):
        self.root = root
        self.recs = recs

    @classmethod
    def read(cls, synctex: Path, root: Path) -> Records:
        inputs, recs, page, unit, mag = {}, [], 0, 1.0, 1.0
        with gzip.open(synctex, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                c = line[:1]
                if c == "I" and line.startswith("Input:"):
                    tag, _, path = line[6:].rstrip("\n").partition(":")
                    if path:
                        try:
                            # pdfTeX writes paths relative to the working directory.
                            pp = Path(path) if os.path.isabs(path) else root / path
                            inputs[int(tag)] = pp.resolve().relative_to(root).as_posix()
                        except ValueError:
                            pass
                elif c in "{UM":
                    try:            # a damaged line is left out, not the whole file
                        if c == "{":
                            page = int(line[1:])
                        elif line.startswith("Unit:"):
                            unit = float(line[5:])
                        elif line.startswith("Magnification:"):
                            mag = float(line[14:]) / 1000.0
                    except ValueError:
                        continue
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
        return cls(root, recs)

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

    def inverse(self, page: int, x: float, y: float, outdir: str,
                bib_files: Callable[[], list[str]] = lambda: []) -> dict | None:
        """The source line typeset at (x, y) on `page`. Text that LaTeX wrote into a
        generated file in `outdir` and read back (the table of contents from .toc, the
        bibliography from .bbl) is not where you edit it: a click there goes to the entry
        of a reference in one of `bib_files`, or else to the nearest line of a source file."""
        hit = self._inverse(page, x, y)
        if hit and hit["file"].startswith(outdir + "/"):
            hit = (self._bib_entry(hit, bib_files) if hit["file"].endswith(".bbl") else None) \
                or self._inverse(page, x, y, lambda f: not f.startswith(outdir + "/"))
        return hit

    def _bib_entry(self, hit: dict, bib_files: Callable[[], list[str]]) -> dict | None:
        """The .bib entry of the \\bibitem whose text is at hit's line of a .bbl file."""
        try:
            lines = (self.root / hit["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        key = None
        for text in reversed(lines[:hit["line"]]):
            m = re.search(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}|\\entry\{([^}]+)\}", text)
            if m:
                key = m.group(1) or m.group(2)
                break
        if not key:
            return None
        entry = re.compile(r"@\w+\s*[{(]\s*" + re.escape(key.strip()) + r"\s*,")
        for rel in bib_files():
            try:
                text = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = entry.search(text)
            if m:
                return {"file": rel, "line": text.count("\n", 0, m.start()) + 1}
        return None

    def _inverse(self, page: int, x: float, y: float, keep=lambda f: True) -> dict | None:
        box = self._line_box(page, x, y)
        on_page = [r for r in self.recs if r[0] == page and r[3] > 0 and keep(r[2])]
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


class SyncIndex:
    """The last .synctex.gz read (one document shown at a time), for any number of threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamp = None
        self._records: Records | None = None
        self._bad: tuple | None = None          # (stamp, message) of a damaged file

    def lookup(self, synctex: Path, root: Path) -> Records:
        """The records of `synctex` (paths relative to `root`); NoData if there are none."""
        try:
            st = synctex.stat()
        except FileNotFoundError:
            raise NoData(NO_DATA) from None
        stamp = (synctex, root, st.st_mtime_ns, st.st_size)
        with self._lock:                        # one read at a time; queries need none
            if stamp == self._stamp:
                return self._records
            if self._bad and self._bad[0] == stamp:
                raise NoData(self._bad[1])
            try:
                records = Records.read(synctex, root)
            except (OSError, EOFError, zlib.error) as exc:
                self._bad = (stamp, f"the synctex data is incomplete or damaged ({exc}); build again")
                raise NoData(self._bad[1]) from None
            self._stamp, self._records, self._bad = stamp, records, None
            return records
