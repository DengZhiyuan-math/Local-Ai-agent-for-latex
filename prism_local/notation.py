"""Complete mathematical identifiers in supported LaTeX and chat math spans.

This module classifies spelling, not permission or mathematical meaning. Unknown
commands and ambiguous decorations remain explicit gaps for the caller to resolve.
"""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right

from texutil import GREEK, group, strip_comments

MAX_NOTATIONS = 20000
MATH_ENVS = "equation|align|alignat|gather|multline|displaymath|math|eqnarray|flalign"
MATH_START = re.compile(r"(?<!\\)(\$\$?|\\\(|\\\[)|\\begin\{(" + MATH_ENVS + r")(\*?)\}|\\ensuremath\s*\{")
COMMAND = re.compile(r"\\([A-Za-z]+|.)", re.S)
STYLED = set("mathbb mathcal mathfrak mathscr mathrm mathsf mathtt mathit mathbf boldsymbol bm hat widehat tilde widetilde bar overline underline vec dot ddot check breve acute grave mathring".split())
OPERATORS = set("sum prod coprod int iint iiint iiiint oint oiint oiiint lim limsup liminf sup inf max min det gcd lcm sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh log ln exp ker dim hom Hom arg deg mod bmod pmod Pr partial nabla infty forall exists nexists in notin ni subset subseteq supset supseteq cup cap bigcup bigcap times cdot otimes oplus circ bullet pm mp le leq ge geq neq ne equiv sim simeq cong approx propto to mapsto rightarrow leftarrow Rightarrow Leftarrow Leftrightarrow iff implies ldot ldots cdots vdots ddots dots emptyset varnothing setminus wedge vee neg land lor perp parallel mid vert Vert lvert rvert lVert rVert langle rangle ast star dagger prime colon coloneqq triangleq".split())
LAYOUT = set("left right middle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr limits nolimits displaystyle textstyle scriptstyle scriptscriptstyle quad qquad thinspace medspace thickspace enspace hfill hfil nonumber notag allowbreak protect relax mathstrut".split())
ARGUMENTS = set("label tag ref eqref cite text textrm textbf textit textnormal mbox hbox hspace vspace color phantom hphantom vphantom rule".split())
TEXT_ARGUMENTS = set("tag text textrm textbf textit textnormal mbox hbox".split())
STRUCTURAL = set("frac dfrac tfrac sqrt binom dbinom tbinom substack begin end underbrace overbrace textcolor smash".split())
IDENTIFIER_COMMANDS = set(GREEK) | {"ell", "hbar", "imath", "jmath", "wp", "Re", "Im"}
INNER_ENVS = set(MATH_ENVS.split("|")) | set("array matrix pmatrix bmatrix Bmatrix vmatrix Vmatrix smallmatrix cases aligned alignedat gathered split".split())
ENV_OPTIONS = {"array", "aligned", "alignedat", "gathered"}
ENV_ARGUMENTS = {"array": 1, "alignat": 1, "alignedat": 1}
OPTIONAL_LAYOUT = {"color", "textcolor", "rule", "smash"}

# Unicode spellings with an unambiguous corresponding mathematical command.
# epsilon/phi variants use their mathematical glyphs, not texutil's plain-text map.
UNICODE_IDENTIFIERS = {value: "\\" + key for key, value in GREEK.items()}
UNICODE_IDENTIFIERS.update({"ε": r"\varepsilon", "ϵ": r"\epsilon", "φ": r"\varphi", "ϕ": r"\phi",
                            "ℓ": r"\ell", "ℏ": r"\hbar"})
UNICODE_DECORATIONS = set("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎⁺⁻⁼⁽⁾″‴⁗")


def _canonical(notation: str) -> str:
    """Normalize equivalent spelling without equating different styled identifiers."""
    value = "".join(UNICODE_IDENTIFIERS.get(c, c) for c in notation)
    value = re.sub(r"\s+", "", value).replace("′", "'")
    value = re.sub(r"\^\{((?:\\prime)+)\}|\^\\prime(?![A-Za-z])",
                   lambda m: "'" * (m.group(1).count(r"\prime") if m.group(1) else 1), value)
    # Single-token scripts do not need braces. Nested scripts keep their grouping.
    value = re.sub(r"([_^])\{([^{}]|\\[A-Za-z]+)\}", r"\1\2", value)
    script = r"(?:\{[^{}]*\}|\\[A-Za-z]+|[A-Za-z0-9+*\-])"
    for _ in range(2):
        value = re.sub(r"([_^]" + script + r")('+)", r"\2\1", value)
    return re.sub(r"(\^" + script + r")(_" + script + r")", r"\2\1", value)


def _math_ranges(text: str, at: int, limit: int):
    """Math body offsets within one text argument or a complete source."""
    while start := MATH_START.search(text, at, limit):
        opening, env = start.group(1), start.group(2)
        if start[0].startswith(r"\ensuremath"):
            argument = group(text, start.end() - 1)
            if argument and argument[1] > limit:
                argument = None
            yield start.end(), argument[1] - 1 if argument else limit, argument is not None, "ensuremath"
            if not argument:
                break
            at = argument[1]
            continue
        if env:
            closing = re.escape(r"\end{" + env + start.group(3) + "}")
        else:
            token = {"$": "$", "$$": "$$", "\\(": "\\)", "\\[": "\\]"}[opening]
            closing = r"(?<!\\)" + re.escape(token)
        closer, cursor, end = re.compile(closing), start.end(), None
        while cursor < limit:
            end = closer.match(text, cursor, limit)
            if end:
                break
            if text[cursor] == "{":
                argument = group(text, cursor)
                if not argument or argument[1] > limit:
                    break
                cursor = argument[1]
                continue
            command = COMMAND.match(text, cursor)
            cursor = command.end() if command else cursor + 1
        yield start.end(), end.start() if end else limit, end is not None, env
        if not end:
            break
        at = end.end()


def _math_spans(text: str, *, latex: bool = True):
    if latex:
        text = strip_comments(text)
    newlines = [m.start() for m in re.finditer("\n", text)]
    for start, end, closed, env in _math_ranges(text, 0, len(text)):
        yield text[start:end], bisect_right(newlines, start - 1) + 1, closed, env


def math_fragments(text: str, *, latex: bool = True) -> list[str]:
    """Supported math bodies, retaining an unclosed tail for conservative checking."""
    return [body for body, _line, _closed, _env in _math_spans(text, latex=latex)]


def _argument(body: str, at: int, end: int):
    """(content, after, content_start, content_end), for one TeX argument."""
    while at < end and body[at].isspace():
        at += 1
    if at >= end:
        return None
    arg = group(body, at)
    if arg and arg[1] <= end:
        return arg[0], arg[1], at + 1, arg[1] - 1
    if body[at] in "{}_^":
        return None
    cmd = COMMAND.match(body, at)
    after = cmd.end() if cmd else at + 1
    return (body[at:after], after, at, after) if after <= end else None


def _optional_argument(body: str, at: int, end: int):
    """Return (after, complete) for one known layout option, or None if absent."""
    while at < end and body[at].isspace():
        at += 1
    if at >= end or body[at] != "[":
        return None
    i, depth = at + 1, 1
    while i < end:
        if body[i] == "\\":
            i += 2
            continue
        if body[i] == "{":
            arg = group(body, i)
            if arg and arg[1] <= end:
                i = arg[1]
                continue
        if body[i] == "[":
            depth += 1
        elif body[i] == "]":
            depth -= 1
            if depth == 0:
                return i + 1, True
        i += 1
    return end, False


def _environment_layout(body: str, at: int, end: int, env: str):
    if env in ENV_OPTIONS:
        option = _optional_argument(body, at, end)
        if option:
            at, complete = option
            if not complete:
                return end, "Unclosed mathematical environment layout option."
    for _ in range(ENV_ARGUMENTS.get(env, 0)):
        arg = _argument(body, at, end)
        if not arg or body[arg[2] - 1:arg[2]] != "{":
            return end, "Unresolved mathematical environment layout argument."
        at = arg[1]
    return at, None


def _label(value: str, known: set[str]) -> bool:
    """Simple identifier labels; compound indices need context, not a guessed name."""
    value = re.sub(r"\s+", "", value)
    if not value:
        return False
    if value in ("+", "-", "*", r"\ast", r"\star") or value.isalnum():
        return True
    decorated = re.fullmatch(r"(\\[A-Za-z]+|[^\W\d_])['′]*(?:_(\{[^{}]+\}|\\[A-Za-z]+|[A-Za-z0-9+*\-]))?['′]*", value)
    if decorated:
        base, label = decorated.groups()
        valid_base = not base.startswith("\\") or base[1:] in IDENTIFIER_COMMANDS | known
        if valid_base and (label is None or _label(label.strip("{}"), known)):
            return True
    command = COMMAND.fullmatch(value)
    if command:
        return command.group(1) in IDENTIFIER_COMMANDS | known
    styled = re.fullmatch(r"\\([A-Za-z]+)\{([^{}]+)\}", value)
    return bool(styled and styled.group(1) in STYLED and _label(styled.group(2), known))


def math_notations(text: str, *, known_commands=(), latex: bool = True) -> dict:
    """Return occurrences (notation/canonical/line) and explicit extraction gaps.

    ``known_commands`` contains verified zero-argument identifier macro names,
    with or without a leading backslash. Their existence does not authorize
    adoption; that decision and source evidence belong to the caller. Powers are ordinary
    operations, while simple label subscripts/superscripts form identifiers.
    """
    known = {str(name).removeprefix("\\") for name in known_commands}
    symbols, gaps = [], []
    limit_reached = False
    for body, base_line, closed, env in _math_spans(text, latex=latex):
        if not closed:
            gaps.append({"line": base_line, "reason": "Unclosed mathematical delimiter; this span was not registered."})
            continue
        newlines = [m.start() for m in re.finditer("\n", body)]

        def line_at(offset):
            return base_line + bisect_right(newlines, offset - 1)

        def gap(at, reason, notation=None):
            item = {"line": line_at(at), "reason": reason}
            if notation:
                item["notation"] = notation
            gaps.append(item)

        def scan(start, end, depth=0):
            nonlocal limit_reached
            if depth > 16:
                gap(start, "Nested mathematical arguments exceed the supported depth.")
                return
            i = start
            while i < end:
                if len(symbols) >= MAX_NOTATIONS:
                    if not limit_reached:
                        gap(i, "The notation occurrence limit was reached; the remaining mathematical input was not indexed.")
                    limit_reached = True
                    return
                begin, name = i, None
                if body[i] == "\\":
                    match = COMMAND.match(body, i)
                    if not match:
                        gap(i, "Incomplete mathematical command.")
                        return
                    name, i = match.group(1), match.end()
                    if name in ARGUMENTS:
                        star = i
                        while star < end and body[star].isspace():
                            star += 1
                        if star < end and body[star] == "*":
                            i = star + 1
                        if name in OPTIONAL_LAYOUT:
                            option = _optional_argument(body, i, end)
                            if option:
                                i, complete = option
                                if not complete:
                                    gap(begin, "Unclosed layout command option.")
                                    continue
                        for _ in range(2 if name == "rule" else 1):
                            arg = _argument(body, i, end)
                            i = arg[1] if arg else i
                            if arg and name in TEXT_ARGUMENTS:
                                for inner_start, inner_end, closed, inner_env in _math_ranges(body, arg[2], arg[3]):
                                    if not closed:
                                        gap(inner_start, "Unclosed mathematical delimiter inside text.")
                                        continue
                                    nested_start, error = (_environment_layout(body, inner_start, inner_end, inner_env)
                                                           if inner_env else (inner_start, None))
                                    if error:
                                        gap(inner_start, error)
                                    else:
                                        scan(nested_start, inner_end, depth + 1)
                        continue
                    if name in OPERATORS or name in LAYOUT or len(name) == 1 and not name.isalpha():
                        continue
                    if name in STRUCTURAL:
                        if name in ("textcolor", "smash"):
                            option = _optional_argument(body, i, end)
                            if option:
                                i, complete = option
                                if not complete:
                                    gap(begin, "Unclosed layout command option.")
                                    continue
                        if name in ("begin", "end", "textcolor"):
                            arg = _argument(body, i, end)
                            i = arg[1] if arg else i
                            if name in ("begin", "end") and arg:
                                env = arg[0]
                                plain_env = env[:-1] if env.endswith("*") and env[:-1] in MATH_ENVS.split("|") else env
                                if plain_env not in INNER_ENVS:
                                    if name == "begin":
                                        depth = 1
                                        tokens = re.compile(r"\\(begin|end)\{" + re.escape(env) + r"\}")
                                        for token in tokens.finditer(body, i, end):
                                            depth += 1 if token.group(1) == "begin" else -1
                                            if depth == 0:
                                                i = token.end()
                                                break
                                        else:
                                            i = end
                                    gap(begin, "An unknown mathematical environment needs context before registration.", body[begin:i])
                                elif name == "begin":
                                    i, error = _environment_layout(body, i, end, plain_env)
                                    if error:
                                        gap(begin, error)
                        continue
                    if name in ("overset", "underset", "stackrel"):
                        for _ in range(2):
                            arg = _argument(body, i, end)
                            i = arg[1] if arg else i
                        gap(begin, "A composed mathematical decoration needs context before registration.", body[begin:i])
                        continue
                    if name in STYLED or name == "operatorname":
                        arg = _argument(body, i, end)
                        if not arg:
                            gap(begin, f"Unresolved argument for \\{name}.")
                            continue
                        i = arg[1]
                        content = arg[0].strip()
                        operator = COMMAND.fullmatch(content)
                        if (content.isdecimal() or operator and operator.group(1) in OPERATORS
                                or name == "operatorname" and content in OPERATORS):
                            continue
                        if not _label(arg[0], known):
                            gap(begin, "A styled compound expression is not a single registered identifier.", body[begin:i])
                            continue
                        base = "\\" + name + "{" + arg[0] + "}"
                    elif name in IDENTIFIER_COMMANDS:
                        base = body[begin:i]
                    elif name in known:
                        base = body[begin:i]
                        after = i
                        while after < end and body[after].isspace():
                            after += 1
                        if after < end and body[after] == "{":
                            while (arg := _argument(body, i, end)) and body[arg[2] - 1:arg[2]] == "{":
                                i = arg[1]
                            gap(begin, "A declared macro followed by grouped arguments needs context before registration.", body[begin:i])
                            continue
                    else:
                        # An unknown macro's braced arguments may be prose. Do not
                        # silently turn those letters into independent identifiers.
                        while (arg := _argument(body, i, end)) and body[arg[2] - 1:arg[2]] == "{":
                            i = arg[1]
                        gap(begin, f"Unresolved mathematical command \\{name}; its expansion was not inferred.", body[begin:i])
                        continue
                elif body[i].isalpha() or body[i] in UNICODE_IDENTIFIERS:
                    i += 1
                    base = body[begin:i]
                else:
                    i += 1
                    continue
                sub, sup, primes, powers = "", "", "", []
                sup_seen = False
                invalid = False
                while i < end:
                    mark = i
                    while mark < end and body[mark].isspace():
                        mark += 1
                    if mark < end and body[mark] in "'′":
                        primes += body[mark]
                        i = mark + 1
                        continue
                    if mark < end and (body[mark] in UNICODE_DECORATIONS
                                       or unicodedata.category(body[mark]).startswith("M")):
                        # These are part of the visible name, never ignorable
                        # punctuation. Keep the complete spelling as a gap until
                        # its equivalence to a supported form is established.
                        invalid = True
                        i = mark + 1
                        continue
                    if mark >= end or body[mark] not in "_^":
                        break
                    kind = body[mark]
                    arg = _argument(body, mark + 1, end)
                    if not arg:
                        gap(mark, "Incomplete identifier decoration.", body[begin:mark + 1])
                        invalid = True
                        i = mark + 1
                        break
                    value, i, arg_start, arg_end = arg
                    value_canonical = _canonical(value)
                    if kind == "^":
                        if sup_seen:
                            invalid = True
                        sup_seen = True
                    if kind == "_":
                        if sub or not _label(value, known):
                            invalid = True
                        sub = body[mark:i]
                    elif re.fullmatch(r"(?:\\prime)+", value_canonical):
                        primes += "'" * value_canonical.count(r"\prime")
                    elif value_canonical in ("+", "-", "*", r"\ast", r"\star") or value.isupper() or value.startswith(r"\mathrm{"):
                        if sup or not _label(value, known):
                            invalid = True
                        sup = body[mark:i]
                    elif (not value.strip() or any(c in value for c in "=,;")
                          or re.fullmatch(r"\s*\([^()]+\)\s*", value)):
                        invalid = True
                    else:
                        # Numeric and algebraic powers do not introduce a new
                        # identifier. Their own identifiers are checked separately.
                        powers.append((arg_start, arg_end))
                if invalid:
                    gap(begin, "A compound or repeated identifier decoration needs context before registration.", body[begin:i])
                else:
                    spelling = base + primes + sub + sup
                    # Retain the author's exact complete spelling where no power
                    # needed to be separated from the identifier.
                    notation = body[begin:i].strip() if not powers else spelling
                    canonical = _canonical(spelling)
                    symbols.append({"notation": notation, "canonical": canonical, "line": line_at(begin)})
                for power_start, power_end in powers:
                    scan(power_start, power_end, depth + 1)

        start, layout_error = _environment_layout(body, 0, len(body), env) if env else (0, None)
        if layout_error:
            gap(0, layout_error)
        else:
            scan(start, len(body))
        if limit_reached:
            break
    return {"symbols": symbols, "gaps": gaps}
