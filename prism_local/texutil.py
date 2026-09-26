"""LaTeX source as plain text, for the outline and the Home page's titles."""
from __future__ import annotations

import re
from pathlib import Path

GREEK = {g: chr(c) for g, c in zip(
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho "
    "varsigma sigma tau upsilon phi chi psi omega".split(), range(0x3B1, 0x3CA))}
GREEK.update({"varepsilon": "ε", "vartheta": "ϑ", "varphi": "φ", "varrho": "ϱ", "varpi": "ϖ",
              "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
              "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω"})
SYMBOLS = {"infty": "∞", "sum": "Σ", "prod": "∏", "int": "∫", "oint": "∮", "partial": "∂",
           "nabla": "∇", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
           "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "cup": "∪",
           "cap": "∩", "times": "×", "cdot": "·", "to": "→", "rightarrow": "→", "mapsto": "↦",
           "leftarrow": "←", "Rightarrow": "⇒", "iff": "⇔", "approx": "≈", "sim": "∼",
           "simeq": "≃", "cong": "≅", "equiv": "≡", "otimes": "⊗", "oplus": "⊕", "ell": "ℓ",
           "langle": "⟨", "rangle": "⟩", "pm": "±", "ldots": "…", "dots": "…", "cdots": "⋯",
           "forall": "∀", "exists": "∃", "circ": "∘", "setminus": "∖", "emptyset": "∅",
           "varnothing": "∅", "hbar": "ℏ", "wedge": "∧", "vee": "∨", "perp": "⊥", "mid": "|",
           "lvert": "|", "rvert": "|", "lVert": "‖", "rVert": "‖", "Vert": "‖", "star": "⋆",
           "dagger": "†", "prime": "′", "S": "§", "ast": "∗", "backslash": "\\"}
BLACKBOARD = {"R": "ℝ", "C": "ℂ", "N": "ℕ", "Z": "ℤ", "Q": "ℚ", "H": "ℍ", "P": "ℙ", "E": "𝔼",
              "F": "𝔽", "T": "𝕋", "A": "𝔸", "K": "𝕂"}
UNWRAP = {"mathcal", "mathrm", "mathbf", "mathit", "mathsf", "mathtt", "mathfrak", "mathscr",
          "boldsymbol", "bm", "operatorname", "text", "textrm", "textbf", "textit", "textsf",
          "texttt", "textsc", "textup", "emph", "mbox", "hbox", "ensuremath", "underline",
          "overline", "widetilde", "tilde", "widehat", "hat", "bar", "vec", "dot", "ddot",
          "check", "breve", "acute", "grave", "mathring", "MakeUppercase", "uppercase"}
DROP = {"label", "footnote", "thanks", "index", "cite", "nocite", "hspace", "vspace", "phantom"}
IGNORE = {"limits", "nolimits", "left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr",
          "Bigl", "Bigr", "biggl", "biggr", "displaystyle", "textstyle", "scriptstyle", "protect",
          "quad", "qquad", "noindent", "nobreak", "relax", "newline", "linebreak", "allowbreak"}
ESCAPES = {"{": "{", "}": "}", "%": "%", "&": "&", "_": "_", "#": "#", "$": "$", "(": "", ")": "",
           "[": "", "]": "", ",": " ", ";": " ", ":": " ", "!": "", " ": " ", "\\": " ", "|": "‖"}
# The space after a command stays: for reading, "≤ ε" beats TeX's "≤ε".
CMD_RE = re.compile(r"\\(?:([A-Za-z]+)\*?|(.))", re.S)


def strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*", "", ln) for ln in text.splitlines())


def group(s: str, i: int) -> tuple[str, int] | None:
    """(contents, end) of the {...} group whose "{" is at s[i]."""
    if i >= len(s) or s[i] != "{":
        return None
    depth, j = 0, i
    while j < len(s):
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return None


def _arg(s: str, i: int) -> tuple[str, int]:
    """The argument that starts at s[i]: a {group}, a command, or one character."""
    while i < len(s) and s[i] in " \t\n":
        i += 1
    g = group(s, i)
    if g:
        return g
    m = CMD_RE.match(s, i) if i < len(s) and s[i] == "\\" else None
    if m:
        return m.group(0), m.end()
    return (s[i], i + 1) if i < len(s) else ("", i)


def _plain(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c != "\\":
            out.append(" " if c == "~" else "" if c in "{}$" else c)
            i += 1
            continue
        m = CMD_RE.match(s, i)
        if not m:
            break                                  # a lone backslash at the very end
        i = m.end()
        name = m.group(1)
        if name is None:
            out.append(ESCAPES.get(m.group(2), ""))
            if m.group(2) == "\\" and s.startswith("[", i) and "]" in s[i:]:
                i = s.index("]", i) + 1                # \\[2pt]
        elif name == "texorpdfstring":             # the PDF (plain text) version
            _, i = _arg(s, i)
            b, i = _arg(s, i)
            out.append(_plain(b))
        elif name in ("frac", "dfrac", "tfrac"):
            a, i = _arg(s, i)
            b, i = _arg(s, i)
            out.append(f"{_plain(a)}/{_plain(b)}")
        elif name == "mathbb":
            a, i = _arg(s, i)
            out.append("".join(BLACKBOARD.get(ch, ch) for ch in _plain(a)))
        elif name in DROP:
            _, i = _arg(s, i)
        elif name in UNWRAP:
            a, i = _arg(s, i)
            out.append(_plain(a))
        elif name not in IGNORE:                   # symbols; a macro of your own by its name
            out.append(SYMBOLS.get(name) or GREEK.get(name) or name)
    return "".join(out)


def plain_text(tex: str) -> str:
    """LaTeX source as readable text: "Estimates for $\\sum\\limits_{j}\\|f_j\\|$" becomes
    "Estimates for Σ_j‖f_j‖". Rough, for display only."""
    return re.sub(r"\s+", " ", _plain(tex)).strip()


def tex_title(path: Path) -> str | None:
    """The \\title{...} of a LaTeX file as plain text, roughly."""
    try:
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace")[:40000])
    except OSError:
        return None
    m = re.search(r"\\title\s*(?:\[[^\]]*\])?\s*\{", text)
    g = group(text, m.end() - 1) if m else None
    return (plain_text(g[0])[:240] or None) if g else None
