"""What the editor reads from LaTeX sources: labels, sections, theorem environments,
macros and \\input's of a .tex file, and the entries of a .bib file. Plain functions of
a file's text; Project.scan keeps them per file while the file stays the same."""
from __future__ import annotations

import re

from texutil import group, plain_text, strip_comments

STANDARD_THEOREMS = ["theorem", "proposition", "lemma", "corollary", "definition",
                     "remark", "example"]

LABEL_RE = re.compile(r"\\label\s*\{([^}]+)\}")
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


def _scan_tex(text: str) -> dict:
    """What a .tex, .sty or .cls file holds for document_order and symbols, line by line
    in the order symbols() takes it: its \\input's, theorem definitions, and its sections,
    \\begin's, macros and labels (events: (what, line, name, title))."""
    inputs, theorems, events = [], [], []
    for n, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw)
        inputs += [m.group(1).strip() for m in INPUT_RE.finditer(line)]
        for m in NEWTHEOREM_RE.finditer(line):
            theorems.append((m.group(1).strip(), (m.group(2) or m.group(1)).strip()))
        for m in DECLARETHEOREM_RE.finditer(line):
            name = m.group(2).strip()
            opt = re.search(r"(?:^|,)\s*(?:name|title)\s*=\s*\{?([^,}]+)", m.group(1) or "")
            theorems.append((name, opt.group(1).strip() if opt else name.capitalize()))
        m = SECTION_RE.search(line)
        if m:
            g = group(line, m.end() - 1)          # a title that runs on: this line's part
            events.append(("section", n, m.group(1), plain_text(g[0] if g else line[m.end():])))
        for m in BEGIN_RE.finditer(line):
            events.append(("begin", n, m.group(1), plain_text(m.group(2) or "")))
        for m in MACRO_RE.finditer(line):
            events.append(("macro", n, m.group(1), None))
        for m in LABEL_RE.finditer(line):
            events.append(("label", n, m.group(1), None))
    return {"inputs": inputs, "theorems": theorems, "events": events, "definitions": macro_definitions(text)}


DEFINE_RE = re.compile(r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|DeclareMathOperator"
                       r"|DeclarePairedDelimiter|def|gdef)(?![A-Za-z])(\*?)")
NAME_RE = re.compile(r"\s*(?:\{\s*\\([A-Za-z]+)\s*\}|\\([A-Za-z]+))")
MAX_BODY = 2000


def macro_definitions(text: str) -> list[tuple[str, str]]:
    """The macros a .tex or .sty file defines, as KaTeX takes them: (name, body), the
    arguments #1 … in the body. \\newcommand and its kin, \\def, \\DeclareMathOperator,
    \\DeclarePairedDelimiter; a macro with an optional argument is left out (KaTeX
    cannot take one), and so is a body that does not close."""
    text = strip_comments(text)
    out = []
    for m in DEFINE_RE.finditer(text):
        kind, star = m.group(1), m.group(2)
        n = NAME_RE.match(text, m.end())
        if not n:
            continue
        name, i = n.group(1) or n.group(2), n.end()
        while i < len(text) and text[i] in " \t\n":
            i += 1
        if kind in ("def", "gdef"):              # \def\name#1#2{…}
            p = re.match(r"(?:#\d)*", text[i:])
            i += p.end()
        else:
            a = re.match(r"\[\s*(\d)\s*\]\s*", text[i:])
            if a:
                i += a.end()
            if text.startswith("[", i):          # an optional argument with a default
                continue
        g = group(text, i)
        if not g or len(g[0]) > MAX_BODY:
            continue
        body = g[0].strip()
        if kind == "DeclareMathOperator":
            body = "\\operatorname" + star + "{" + body + "}"
        elif kind == "DeclarePairedDelimiter":
            j = g[1]
            while j < len(text) and text[j] in " \t\n":
                j += 1
            g2 = group(text, j)
            if not g2:
                continue
            body = "\\left" + body + " #1 \\right" + g2[0].strip()
        out.append((name, body))
    return out


def _scan_bib(text: str) -> list[tuple]:
    """The entries of a .bib file: (key, type, line)."""
    keys, line, at = [], 1, 0
    for m in BIBKEY_RE.finditer(text):
        line += text.count("\n", at, m.start())
        at = m.start()
        if m.group(1).lower() not in ("string", "preamble", "comment"):
            keys.append((m.group(2), m.group(1).lower(), line))
    return keys
