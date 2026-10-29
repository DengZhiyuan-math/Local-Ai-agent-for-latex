"""Conservative interpretation of ordinary, original human notation instructions.

This module recognizes intent and complete subjects, not authority. The editor
validates identifiers, proposal versions and durable evidence before adoption.
Unrecognized prose never becomes permission.
"""
from __future__ import annotations

import re

from notation import _canonical

MATH = r"\$([^$\n]+)\$"
TARGET = r"(?:\$[^$\n]+\$|`[^`\n]+`|第[一二三四五六七八九十\d]+项|(?:item|proposal)\s+\d+|[\\\w{}_^'+*\-]+)"
ACTION_START = r"(?:and\s+)?(?:i\s+)?(?:(?:approve|reject|use|adopt|reopen|keep)\b|(?:don't|do not)\s+(?:use|adopt|approve)\b|同意|拒绝|不同意|用|采用|重新|保持|不要|不采用|(?:最终)?不用|最终不采用)"
UNCERTAIN = re.compile(r"\b(?:if|unless|maybe|perhaps|whether|might|either|or)\b|[?？]|如果|也许|可能|或者|是否|看情况", re.I)
REPORTED = re.compile(r"\b(?:document|attachment|model|tool|paper|quote|author)\s+(?:says?|said|reads?|wrote)\b|(?:文档|附件|模型|工具|引文|引用|作者)(?:说|写|指出|中说)", re.I)
BASE = r"(?:\\[A-Za-z]+|[^\W\d_])"
INDEX = r"(?:-?\d+|\\[A-Za-z]+|[^\W\d_])"
BARE_IDENTIFIER = BASE + r"(?:'|[_^](?:\{[^{}\n]+\}|\\[A-Za-z]+|[A-Za-z0-9+*\-]))*"
NAMED_NOTATION = r"(?P<notation>\$[^$\n]+\$|" + BARE_IDENTIFIER + ")"
QUOTED_BLOCK = re.compile(r"(?m)^(?: {0,3}(?:`{3,}|~{3,}|>)|(?: {4}| {0,3}\t)[ \t]*\S)")
HTML_QUOTE = re.compile(r"<!--|</?(?:blockquote|pre|code|q|samp)\b", re.I)


def quoted_message(text):
    """Quoted/code messages do not express an author notation decision."""
    return "```" in text or bool(QUOTED_BLOCK.search(text) or HTML_QUOTE.search(text))


def _unwrapped(value):
    value = value.strip()
    return value[1:-1].strip() if value.startswith("$") and value.endswith("$") else value


def family_matches(canonical, rule):
    """Match a complete identifier against an explicit, finite subscript family."""
    if not isinstance(canonical, str) or not isinstance(rule, dict):
        return False
    base, indices = rule.get("base"), rule.get("indices")
    if (not isinstance(base, str) or re.fullmatch(BASE, base) is None
            or not isinstance(indices, list) or not 1 <= len(indices) <= 100
            or not all(isinstance(i, str) and re.fullmatch(INDEX, i) for i in indices)):
        return False
    return _canonical(canonical) in {_canonical(base + "_{" + index + "}") for index in indices}


def _family(notation, expression, variable, values):
    representative = re.fullmatch("(?P<base>" + BASE + r")_(?:\{(?P<braced>[A-Za-z])\}|(?P<index>[A-Za-z]))", _canonical(notation))
    indices = [part.strip() for part in values.split(",")]
    if (not representative or (representative["braced"] or representative["index"]) != variable
            or not 1 <= len(indices) <= 100 or not all(re.fullmatch(INDEX, part) for part in indices)):
        return None
    return {"kind": "family", "notation": notation.strip(), "definition": expression.strip(),
            "source_expression": expression.strip(), "scope": "project",
            "rule": {"base": representative["base"], "indices": list(dict.fromkeys(indices))}}


def _clauses(text):
    result, start, math, depth = [], 0, False, 0
    for at, char in enumerate(text):
        if char == "$" and (not at or text[at - 1] != "\\"):
            math = not math
        if math:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        scope_continuation = re.match(r"^(?:reject\b|拒绝|不同意|保持)", text[start:].lstrip(), re.I) and \
            re.match(r"^(?:and\s+)?(?:keep\b|保持|不要(?:再)?(?:引入|简称|缩写))", text[at + 1:].lstrip(), re.I)
        guarded = re.match(r"^(?:if\b|unless\b|如果)", text[start:].lstrip(), re.I)
        split = not depth and not guarded and (char in ";；\n" or (char in ",，" and not scope_continuation and
            re.match(ACTION_START, text[at + 1:].lstrip(), re.I)))
        if split:
            result.append(text[start:at].strip())
            start = at + 1
    result.append(text[start:].strip())
    return [re.sub(r"^and\s+", "", part, flags=re.I).strip().rstrip(" .!。！")
            for part in result if part.strip()]


def _proposal(target, proposals):
    target = target.strip()
    order = re.fullmatch(r"(?:第([一二三四五六七八九十\d]+)项|(?:item|proposal)\s+(\d+))", target, re.I)
    if order:
        number = order[1] or order[2]
        number = "一二三四五六七八九十".find(number) + 1 if not number.isdigit() else int(number)
        return proposals[number - 1] if 0 < number <= len(proposals) else None
    if target[:1] in ("$", "`") and target[-1:] == target[:1]:
        target = target[1:-1]
    matches = [p for p in proposals if p.get("canonical") == _canonical(target)]
    return matches[0] if len(matches) == 1 else None


def _targeted(value, proposals):
    negative = re.fullmatch(r"(?:don't|do not)\s+(?:use|adopt|approve)\s+(?P<target>" + TARGET +
        r")(?:\s+after all)?", value, re.I)
    negative = negative or re.fullmatch(r"(?:最终)?(?:不用|不采用|不要采用|不要使用|不批准|不要批准)\s*(?P<target>" + TARGET + ")", value)
    if negative:
        target = negative["target"]
        proposal = _proposal(target, proposals)
        if proposal and not proposal.get("needs_details"):
            return {"kind": "reject", "target": proposal["canonical"], "rejection_scope": "name"}, None
        notation = target[1:-1].strip() if target[:1] in ("$", "`") and target[-1:] == target[:1] else target
        if re.fullmatch(BARE_IDENTIFIER, notation) is None:
            return None, "Specify the exact notation name not to adopt."
        return {"kind": "refuse_name", "target": _canonical(notation), "notation": notation}, None
    expression_rejection = re.fullmatch(r"(?:i\s+)?reject\s+(?P<target>" + TARGET +
        r")(?:\s+and\s+|\s*,\s*)(?:keep (?:the|its|this) (?:original )?expression expanded|"
        r"(?:do not|don't) abbreviate (?:it|the expression))", value, re.I)
    expression_rejection = expression_rejection or re.fullmatch(r"(?:拒绝|不同意)\s*(?P<target>" + TARGET +
        r")\s*[,，]?\s*(?:保持原式展开|保持原表达式展开|不要再引入任何简称)", value)
    if expression_rejection:
        proposal = _proposal(expression_rejection["target"], proposals)
        if not proposal or not proposal.get("source_expression", "").strip():
            return None, "Specify the expression to keep expanded."
        return {"kind": "reject", "target": proposal["canonical"], "rejection_scope": "expression",
                "source_expression": proposal["source_expression"]}, None
    for kind, pattern in (("approve", r"(?:i\s+)?approve\s+(?P<target>" + TARGET + ")"),
                          ("approve", r"(?:我)?(?:同意|批准|接受)\s*(?P<target>" + TARGET + ")"),
                          ("approve", r"(?P<target>" + TARGET + r")\s*(?:同意|批准|接受)"),
                          ("reject", r"(?:i\s+)?reject\s+(?P<target>" + TARGET + ")"),
                          ("reject", r"(?:我)?(?:拒绝|不同意|不采用)\s*(?P<target>" + TARGET + ")"),
                          ("reject", r"(?P<target>" + TARGET + r")\s*(?:拒绝|不同意|不采用)"),
                          ("reopen", r"reopen\s+(?P<target>" + TARGET + ")"),
                          ("reopen", r"(?:重新考虑|重新打开|重新讨论)\s*(?P<target>" + TARGET + ")")):
        match = re.fullmatch(pattern, value, re.I)
        if match:
            proposal = _proposal(match["target"], proposals)
            if kind == "reopen" and (proposal is None or proposal.get("needs_details")):
                target = match["target"]
                notation = target[1:-1].strip() if target[:1] in ("$", "`") and target[-1:] == target[:1] else target
                if re.fullmatch(BARE_IDENTIFIER, notation):
                    return {"kind": "reopen", "target": _canonical(notation)}, None
            if not proposal or proposal.get("needs_details"):
                return None, "Review the complete current proposal for " + match["target"] + "."
            action = {"kind": kind, "target": proposal["canonical"]}
            if kind == "reject":
                action["rejection_scope"] = "name"
            return action, None
    match = re.fullmatch(r"(?:use|adopt)\s+" + MATH + r"\s+instead of\s+(?P<target>" + TARGET + ")", value, re.I)
    if match:
        replacement, target = match[1], match["target"]
    else:
        match = re.fullmatch(r"(?P<target>" + TARGET + r")\s*(?:改用|换成|改为)\s*" + MATH, value)
        if not match:
            return None, None
        replacement, target = match[2], match["target"]
    proposal = _proposal(target, proposals)
    if not proposal or proposal.get("needs_details"):
        return None, "Review the complete current proposal for " + target + "."
    return {"kind": "replace", "target": proposal["canonical"], "notation": replacement.strip(),
            **{key: proposal.get(key, "") for key in ("definition", "scope", "source_expression")}}, None


def parse_actions(text, proposals):
    """Return explicit actions or missing details; unrelated text returns None."""
    if not isinstance(text, str) or len(text) > 40000 or not isinstance(proposals, list):
        return None
    if quoted_message(text) or REPORTED.search(text):
        return None
    text = re.sub(r'"[^"]*"|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』', "", text)
    actions, clarifications = [], []
    for value in _clauses(text.strip()):
        intent = re.match(ACTION_START, value, re.I) or (UNCERTAIN.search(value) and
            re.search(ACTION_START, value, re.I))
        if intent and UNCERTAIN.search(value):
            clarifications.append("Clarify this part of the notation instruction: " + value)
            continue
        expression_refusal = re.fullmatch(r"keep\s+" + MATH + r"\s+expanded", value, re.I)
        expression_refusal = expression_refusal or re.fullmatch(r"(?:do not|don't|never) abbreviate\s+" + MATH, value, re.I)
        expression_refusal = expression_refusal or re.fullmatch(r"保持\s*" + MATH +
            r"\s*展开(?:\s*[,，]\s*不要(?:再)?(?:简称|缩写))?", value)
        if expression_refusal:
            actions.append({"kind": "refuse_expression", "source_expression": expression_refusal[1].strip()})
            continue
        family = re.fullmatch(r"(?:use|adopt)\s+" + MATH + r"\s+(?:for|to denote)\s+" + MATH +
            r"\s+with\s+([A-Za-z])\s+in\s+\{([^{}]+)\}", value, re.I)
        family = family or re.fullmatch(r"(?:用|采用)\s*" + MATH + r"\s*(?:表示|代表)\s*" + MATH +
            r"\s*[,，]?\s*其中\s*([A-Za-z])\s*(?:属于|取自|取值为|∈)\s*\{([^{}]+)\}", value)
        if family:
            action = _family(*family.groups())
            if action:
                actions.append(action)
            else:
                clarifications.append("Specify a finite list of literal indices for the named family.")
            continue
        if intent and re.search(r"\bwith\s+[A-Za-z]\s+in\b|其中", value, re.I):
            clarifications.append("Specify a finite list of literal indices for the named family.")
            continue
        direct = re.fullmatch(r"(?:use|adopt)\s+" + NAMED_NOTATION + r"\s+(?:for|to denote)\s+(?P<meaning>.+)", value, re.I)
        direct = direct or re.fullmatch(r"(?:用|采用)\s*" + NAMED_NOTATION + r"\s*(?:表示|代表)\s*(?P<meaning>.+)", value)
        if direct:
            notation, expression = _unwrapped(direct["notation"]), _unwrapped(direct["meaning"])
            if not expression or re.search(r"\bbut\b|\band\s+(?:do not|don't|approve|reject|use)\b|但是|不过|但不要", expression, re.I):
                clarifications.append("Clarify the intended meaning and adoption instruction for " + notation + ".")
                continue
            actions.append({"kind": "adopt", "notation": notation, "definition": expression,
                            "source_expression": expression, "scope": "project"})
            continue
        action, missing = _targeted(value, proposals)
        if action:
            actions.append(action)
        if missing:
            clarifications.append(missing)
        elif intent and not action:
            clarifications.append("Clarify the notation, intended expression or current proposal: " + value)
    return {"actions": actions, "clarifications": clarifications} if actions or clarifications else None
