"""A project's research baseline. Reading status never starts initialization.

The JSON record is authoritative; the page renders a report from it. Extraction is
deliberately conservative and reports its limits instead of inventing definitions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import uuid
from bisect import bisect_right
from pathlib import Path

from errors import Conflict, Refused
from fsutil import project_path, replace_file
from project import MAX_TEXT, SAVE_WAIT
from registry import project_key
from texutil import group, plain_text, strip_comments
from notation import MATH_START, MAX_NOTATIONS, _canonical, math_fragments, math_notations
from symbols import DEFINE_RE, NAME_RE, macro_definitions
from research_provenance import Provenance, read_plain

RECORD = "research/knowledge.json"
SCHEMA = 1
MAX_RECORD = 32 * 1024 * 1024
MAX_SOURCES = 300
MAX_INPUT = 40000
MAX_TOTAL_TEXT = 8 * 1024 * 1024
INCLUDE = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
SOURCE_COMMANDS = set("input include includeonly documentclass usepackage begin end newcommand renewcommand providecommand DeclareMathOperator DeclareRobustCommand DeclarePairedDelimiter def gdef edef xdef let futurelet csname endcsname expandafter noexpand catcode newcount newdimen newtoks newwrite openout closeout write read openin closein immediate directlua latelua luacode special".split())
SOURCE_COMMANDS.update("InputIfFileExists inputiffileexists import subimport includefrom subincludefrom RequirePackage LoadClass newenvironment renewenvironment mathcode char mathchar chardef mathchardef scantokens shipout".split())


def declared_identifier_commands(texts) -> set[str]:
    """Zero-argument source macros whose explicit body is one identifier.

    The existing macro reader drops arity, so check the declaration as well.
    An argument macro or an expression abbreviation is not guessed to be a name.
    """
    found = set()
    for text in texts:
        clean = strip_comments(text)
        bodies = dict(macro_definitions(clean))
        for match in DEFINE_RE.finditer(clean):
            name = NAME_RE.match(clean, match.end())
            if not name:
                continue
            key = name.group(1) or name.group(2)
            found.discard(key)       # a later parameter declaration invalidates the name
            at = name.end()
            while at < len(clean) and clean[at].isspace():
                at += 1
            if clean[at:at + 3] == "[0]":
                at += 3
                while at < len(clean) and clean[at].isspace():
                    at += 1
            if at >= len(clean) or clean[at] != "{" or key not in bodies:
                continue
            body = bodies[key]
            parsed = math_notations("$" + body + "$")
            if (not parsed["gaps"] and len(parsed["symbols"]) == 1
                    and parsed["symbols"][0]["canonical"] == _canonical(body)):
                found.add(key)
    return found


def _verify_manual(receipt: dict, key: str) -> bool:
    # Read the local verifier lazily: reading a baseline does not create host state.
    from research_edits import verify_receipt
    return bool(verify_receipt(receipt, key))


def _terms(text: str) -> list[dict]:
    """Only explicitly marked terms and quoted source definitions, never inferred ones."""
    text = strip_comments(text)
    found = []
    newlines = [m.start() for m in re.finditer("\n", text)]
    for match in re.finditer(r"\\(?:emph|textbf)\s*\{", text):
        term = group(text, match.end() - 1)
        if not term:
            continue
        name = plain_text(term[0])
        if not name or len(name) > 160 or "$" in term[0]:
            continue
        following = text[term[1]:]
        meaning = re.match(r"\s*(?:is defined as|is|means|denotes|:|：)\s+([^\n.!?。]{1,800})", following)
        found.append({"name": name, "meaning": plain_text(meaning.group(1)) if meaning else None,
                      "line": bisect_right(newlines, match.start() - 1) + 1})
    for match in re.finditer(r"\\begin\{definition\}\s*\[([^\]\n]{1,160})\]", text):
        end = text.find(r"\end{definition}", match.end())
        body = text[match.end():end] if end >= 0 else ""
        found.append({"name": plain_text(match.group(1)), "meaning": plain_text(body)[:1200] or None,
                      "line": bisect_right(newlines, match.start() - 1) + 1})
    return found


def _notation_definitions(text: str) -> list[dict]:
    """Explicit single-identifier definitions, with the original source sentence.

    This recognizes only "x denotes ...", "x is defined as ..." and
    "Let x be ...". A nearby equation or claim is not inferred to be a definition.
    """
    text = strip_comments(text)
    newlines = [m.start() for m in re.finditer("\n", text)]
    pattern = re.compile(
        r"(?P<let>\b[Ll]et\s+)?(?P<form>(?<![\\$])\$(?!\$)(?P<dollar>[^$\n]+)\$"
        r"|\\\((?P<paren>[^\n]*?)\\\))\s+"
        r"(?P<verb>denotes?|means|is\s+defined\s+as|be)\s+"
        r"(?P<meaning>[^\n.!?。]{1,800})(?:[.!?。]|$)")
    result = []
    for match in pattern.finditer(text):
        if match["verb"] == "be" and match["let"] is None:
            continue
        expression = match["dollar"] if match["dollar"] is not None else match["paren"]
        parsed = math_notations(match["form"])
        if parsed["gaps"] or len(parsed["symbols"]) != 1:
            continue
        canonical = parsed["symbols"][0]["canonical"]
        if _canonical(expression) != canonical:
            continue
        result.append({"canonical": canonical, "meaning": plain_text(match["meaning"]),
                       "definition": match[0], "line": bisect_right(newlines, match.start() - 1) + 1})
    return result


class ResearchKnowledge:
    def __init__(self, project):
        self.project = project

    def _path(self) -> Path:
        # Internal records may be JSON, but must never follow a linked record/folder.
        root = self.project.root
        path = project_path(root, RECORD)
        if path != root / RECORD or any(p.is_symlink() for p in (root / "research", root / RECORD)):
            raise Refused("The research record must be a regular file inside this project.")
        return path

    def _load(self) -> dict | None:
        if not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise Conflict("The project files are busy; research evidence could not be checked.")
        try:
            observation = Provenance(self.project.root, RECORD, MAX_RECORD).inspect()
            entry, received = observation["entry"], observation["received"]
            if entry is None and received is None and observation["raw"] is None and not observation["error"]:
                return None
            key = project_key(self.project.root.resolve())
            data = copy.deepcopy(entry["data"]) if entry else {
                "initialized": False, "schema": SCHEMA, "project_key": key, "sources": [], "explicit_notation": "",
                "coverage": {}, "terms": [], "symbols": [], "corrections": [], "discussion": {"mode": "markdown"},
                "manual_evidence": [], "decisions": [], "proposals": [], "unverified_symbols": [],
                "maintenance_notices": [], "incremental_gaps": []}
            if entry and data.get("initialized"):
                self._validate_record(data)
            malformed = bool(observation["error"])
            if received is not None and not self._received_shape(received):
                malformed = True
            foreign = bool(received and received.get("project_key") != key)
            if received is not None and not foreign and not malformed and self._merge_received(data, received):
                Provenance(self.project.root, RECORD, MAX_RECORD).save(data, observation["sha256"], None, private_only=True,
                    expected_revision=entry["receipt"]["data_sha256"] if entry else None)
                entry = Provenance(self.project.root, RECORD, MAX_RECORD).inspect()["entry"]
            local = bool(entry and observation["sha256"] == entry.get("public_sha256"))
            record_state = ("pending" if observation["pending"] else "malformed" if malformed else
                "missing" if observation["raw"] is None else "foreign" if foreign else "local" if local else "received")
            # Operational settings belong to the current project record, never a
            # restored policy checkpoint. Their own subsystem verifies its witness.
            discussion = copy.deepcopy(data.get("discussion", {}))
            discussion["mode"] = "markdown"
            discussion.pop("github", None)
            if received is not None and not foreign and not malformed:
                incoming = received["discussion"]
                discussion["mode"] = incoming.get("mode") if incoming.get("mode") in ("markdown", "github") else "markdown"
                if isinstance(incoming.get("github"), dict):
                    discussion["github"] = copy.deepcopy(incoming["github"])
                if isinstance(received.get("question"), str):
                    data["question"] = received["question"]
            data["discussion"] = discussion
            references, symbols = self._received_references(received)
            warning = "" if record_state == "local" else (
                "The project record is an unverified reference. Local notation evidence is retained; received text is preserved.")
            if malformed or observation["conflict"]:
                warning = "Resolve the received research record before writing. Local policy evidence remains available."
            data["provenance"] = {"record_state": record_state, "baseline_verified": bool(entry and entry.get("basis")),
                "received_sha256": observation["sha256"], "received_project_key": received.get("project_key") if received else None,
                "received_symbols": symbols if not local else [], "received_references": references if not local else {},
                "warning": warning, "project_record_pending": not local or observation["pending"],
                "write_blocked": malformed or observation["conflict"],
                "root": str(self.project.root.resolve()),
                "revision": entry["receipt"]["data_sha256"] if entry else None}
            return data
        finally:
            self.project.files_lock.release()

    @classmethod
    def _received_shape(cls, data):
        return (isinstance(data, dict) and data.get("schema") == SCHEMA and data.get("initialized") is True
            and isinstance(data.get("project_key"), str)
            and all(isinstance(data.get(key), list) for key in ("sources", "terms", "symbols", "corrections"))
            and isinstance(data.get("coverage"), dict) and isinstance(data.get("discussion"), dict)
            and cls._content_shape(data))

    @staticmethod
    def _content_shape(data):
        return (all(isinstance(source, dict) and isinstance(source.get("path"), str)
                and isinstance(source.get("text"), str) and isinstance(source.get("sha256"), str)
                and hashlib.sha256(source["text"].encode()).hexdigest() == source["sha256"] for source in data["sources"])
            and all(isinstance(item, dict) and isinstance(item.get("name"), str)
                    and isinstance(item.get("sources"), list) for item in data["terms"])
            and all(isinstance(item, dict) and isinstance(item.get("notation"), str)
                    and isinstance(item.get("canonical"), str) and isinstance(item.get("sources"), list)
                    for item in data["symbols"]))

    @staticmethod
    def _received_references(data):
        if not isinstance(data, dict):
            return {}, []
        references = {key: copy.deepcopy(data[key]) for key in
                      ("question", "terms", "coverage", "discussion", "proposals", "decisions", "manual_evidence") if key in data}
        references["sources"] = [{key: source.get(key) for key in ("path", "sha256")}
                                 for source in data.get("sources", []) if isinstance(source, dict)] if isinstance(data.get("sources"), list) else []
        symbols = []
        for symbol in data.get("symbols", []) if isinstance(data.get("symbols"), list) else []:
            if isinstance(symbol, dict) and isinstance(symbol.get("notation"), str):
                symbols.append({"notation": symbol["notation"], "canonical": _canonical(symbol["notation"]),
                                "status": "unverified", "sources": copy.deepcopy(symbol.get("sources", []))})
        return references, symbols

    @classmethod
    def _merge_received(cls, data, received):
        """Admit independently witnessed events, never a received baseline or mirror."""
        from research_decisions import verify_decision, verify_instruction
        before = copy.deepcopy(data)
        key = data["project_key"]
        decisions = data.get("decisions", [])
        proposals = data.get("proposals", [])
        candidates = []
        for proposal in received.get("proposals", []) if isinstance(received.get("proposals"), list) else []:
            for candidate in [proposal, *(proposal.get("history", []) if isinstance(proposal, dict)
                                          and isinstance(proposal.get("history"), list) else [])]:
                try:
                    candidate = copy.deepcopy(candidate)
                    candidate.update(history=[], status="pending")
                    if candidate.get("project_key") != key or candidate.get("content_hash") != cls._proposal_hash(candidate):
                        continue
                    cls._proposal_records({"project_key": key, "proposals": [candidate]})
                    candidates.append(candidate)
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
        for receipt in received.get("decisions", []) if isinstance(received.get("decisions"), list) else []:
            if not isinstance(receipt, dict) or any(item.get("id") == receipt.get("id") for item in decisions):
                continue
            candidate = next((item for item in candidates if verify_decision(receipt, key, item)), None)
            if not verify_instruction(receipt, key) and candidate is None:
                continue
            decisions.append(copy.deepcopy(receipt))
            if candidate is not None:
                current = next((item for item in proposals if item["id"] == candidate["id"]), None)
                if current is None:
                    proposals.append(candidate)
                elif candidate["revision"] > current["revision"] and candidate["canonical"] == current["canonical"]:
                    candidate["history"] = [*current.get("history", []),
                                             {k: copy.deepcopy(v) for k, v in current.items() if k != "history"}]
                    proposals[proposals.index(current)] = candidate
        evidence = data.get("manual_evidence", [])
        for receipt in received.get("manual_evidence", []) if isinstance(received.get("manual_evidence"), list) else []:
            if (isinstance(receipt, dict) and _verify_manual(receipt, key)
                    and not any(item.get("id") == receipt.get("id") for item in evidence)):
                evidence.append(copy.deepcopy(receipt))
        if decisions or "decisions" in data:
            data["decisions"] = decisions
        if proposals or "proposals" in data:
            data["proposals"] = proposals
        if evidence or "manual_evidence" in data:
            data["manual_evidence"] = evidence
        cls._proposal_records(data)
        return data != before

    def _validate_record(self, data):
        if (not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("initialized") is not True
                or data.get("project_key") != project_key(self.project.root)
                or not all(isinstance(data.get(k), list) for k in ("sources", "terms", "symbols", "corrections"))
                or not isinstance(data.get("coverage"), dict) or not isinstance(data.get("discussion"), dict)):
            raise Refused("The research record has an unsupported format or belongs to another project.")
        if not self._content_shape(data):
            raise Refused("The research record is damaged; its source evidence cannot be read reliably.")
        if any(key in data and not isinstance(data[key], list) for key in
               ("manual_evidence", "unverified_symbols", "maintenance_notices", "incremental_gaps", "decisions")):
            raise Refused("The incremental research records are damaged.")
        self._proposal_records(data)
        return data

    @staticmethod
    def _report(data: dict) -> dict:
        result = copy.deepcopy(data)
        for source in result["sources"]:
            source.pop("text", None)
        result["record_path"] = RECORD
        result.setdefault("proposals", [])
        result.setdefault("decisions", [])
        result["adoptions"] = list(ResearchKnowledge._adoptions(data).values())
        result["families"] = ResearchKnowledge._families(data)
        result["rejections"] = list(ResearchKnowledge._rejections(data).values())
        result["github_decisions"] = ResearchKnowledge._github_decisions(data)
        return result

    @staticmethod
    def _github_receipts(data, *, historical=False):
        from research_decisions import verify_decision, verify_instruction
        proposals = data.get("proposals", [])
        if historical:
            proposals = [version for proposal in proposals for version in [proposal, *proposal.get("history", [])]
                         if isinstance(version, dict)]
        return [receipt for receipt in data.get("decisions", [])
                if isinstance(receipt, dict) and receipt.get("channel") == "github_comment"
                and (verify_instruction(receipt, data["project_key"]) or any(
                    verify_decision(receipt, data["project_key"],
                        {**proposal, "status": receipt.get("decision")} if historical else proposal)
                    for proposal in proposals))]

    @classmethod
    def _github_decisions(cls, data):
        result = []
        for receipt in cls._github_receipts(data):
            source = receipt["source"]
            subjects = source["publication"]["subjects"]
            proposal_id = receipt.get("proposal_id", receipt.get("github_subject", {}).get("id"))
            subject = next((item for item in subjects if item["id"] == proposal_id), None)
            comment = source["comment"]
            result.append({"id": receipt["id"], "decision": receipt["decision"],
                "kind": receipt.get("kind", receipt["purpose"]), "channel": "github_comment",
                "proposal_id": subject.get("id") if subject else None,
                "revision": receipt.get("revision", subject.get("revision") if subject else None),
                "content_hash": receipt.get("content_hash", subject.get("content_hash") if subject else None),
                "subjects": copy.deepcopy(subjects), "human_text": receipt["human_text"],
                "author": copy.deepcopy(comment["user"]), "comment_url": comment["html_url"],
                "comment_id": comment["id"], "updated_at": comment["updated_at"],
                "body_sha256": comment["body_sha256"], "repository": copy.deepcopy(source["repository"]),
                "issue": copy.deepcopy(source["issue"]), "topic_id": source["topic_id"], "batch_id": source["batch_id"]})
        return result

    def status(self) -> dict:
        data = self._load()
        return self._report(data) if data else {"initialized": False}

    @staticmethod
    def _incremental_defaults(data: dict) -> None:
        for key in ("manual_evidence", "unverified_symbols", "maintenance_notices", "incremental_gaps"):
            value = data.setdefault(key, [])
            if not isinstance(value, list):
                raise Refused("The incremental research records are damaged.")

    @staticmethod
    def _excluded(data: dict) -> set[str]:
        return {_canonical(name) for correction in data["corrections"]
                if isinstance(correction, dict)
                for name in correction.get("excluded_symbols", []) if isinstance(name, str)}

    @staticmethod
    def _baseline_occurrences(data: dict) -> dict[str, dict]:
        texts = [source["text"] for source in data["sources"]]
        commands = declared_identifier_commands(texts)
        supplied = data.get("explicit_notation", "")
        result = {}
        material = [(s["text"], s["path"], s["sha256"], "baseline") for s in data["sources"]]
        if isinstance(supplied, str) and supplied.strip():
            # Initialization's notation field is explicitly authored notation.
            explicit_commands = re.findall(r"\\([A-Za-z]+)", supplied)
            commands |= set(explicit_commands)
            text = supplied if MATH_START.search(supplied) else "$" + supplied + "$"
            material.append((text, "author input", hashlib.sha256(supplied.encode()).hexdigest(), "human_input"))
        for text, path, digest, evidence in material:
            for item in math_notations(text, known_commands=commands)["symbols"]:
                source = {"path": path, "line": item["line"], "sha256": digest, "evidence": evidence}
                entry = result.setdefault(item["canonical"], {**item, "sources": []})
                if source not in entry["sources"]:
                    entry["sources"].append(source)
        return result

    @staticmethod
    def _manual_occurrences(data: dict) -> dict[str, dict]:
        result = {}
        for receipt in data.get("manual_evidence", []):
            if (not isinstance(receipt, dict) or not isinstance(receipt.get("path"), str)
                    or not isinstance(receipt.get("id"), str)
                    or not isinstance(receipt.get("after_sha256"), str)
                    or not isinstance(receipt.get("added"), list)
                    or not _verify_manual(receipt, data["project_key"])):
                continue
            for item in receipt.get("added", []):
                if not isinstance(item, dict) or not isinstance(item.get("notation"), str):
                    continue
                canonical = _canonical(item["notation"])
                if (item.get("canonical") != canonical or not isinstance(item.get("line"), int)
                        or isinstance(item["line"], bool) or item["line"] < 1):
                    continue
                source = {"path": receipt["path"], "line": item["line"],
                          "sha256": receipt["after_sha256"], "evidence": "human_edit",
                          "receipt_id": receipt["id"]}
                entry = result.setdefault(canonical, {**item, "sources": []})
                if source not in entry["sources"]:
                    entry["sources"].append(source)
        return result

    @classmethod
    def _approved_occurrences(cls, data: dict) -> dict[str, dict]:
        from research_decisions import verify_decision
        result = {}
        for proposal in data.get("proposals", []):
            if (proposal.get("status") != "approved"
                    or cls._expression_rejected(data, proposal.get("source_expression", ""))):
                continue
            receipt = next((r for r in data.get("decisions", [])
                            if verify_decision(r, data["project_key"], proposal)
                            and r.get("decision") == "approved"), None)
            if receipt:
                result[proposal["canonical"]] = {"notation": proposal["notation"],
                    "canonical": proposal["canonical"], "meaning": proposal["definition"],
                    "sources": [{**cls._decision_provenance(receipt, data.get("provenance", {}).get("root")),
                                 "proposal_id": proposal["id"]}]}
        for canonical, receipt in cls._adoptions(data).items():
            result[canonical] = {"notation": receipt["notation"], "canonical": canonical,
                "meaning": receipt["definition"], "sources": [cls._decision_provenance(
                    receipt, data.get("provenance", {}).get("root"))]}
        return result

    @staticmethod
    def _decision_provenance(receipt, root=None):
        source = {"path": "human chat", "line": 1, "evidence": "human_decision", "decision_id": receipt["id"],
                  "sha256": hashlib.sha256(receipt["human_text"].encode()).hexdigest()}
        if receipt.get("channel") == "github_comment":
            comment = receipt["source"]["comment"]
            source.update(path="GitHub @" + comment["user"]["login"], evidence="github_decision",
                          url=comment["html_url"], comment_id=comment["id"])
        confirmation = receipt.get("received_confirmation")
        if isinstance(confirmation, dict):
            from research_edits import verify_receipt
            question = confirmation.get("question")
            if (isinstance(question, dict) and question.get("kind") == "received_notation_question"
                    and question.get("root") == root and question.get("canonical") == receipt.get("canonical")
                    and confirmation.get("question_id") == question.get("id")
                    and confirmation.get("sources") == question.get("sources")
                    and verify_receipt(question, receipt["project_key"]) and question.get("sources")):
                received = question["sources"][0]
                source.update(path=received["path"], sha256=received["sha256"],
                              evidence="author_confirmation", question_id=question["id"])
                source.pop("line", None)
                source.update({key: received[key] for key in ("line", "json_pointer", "entry_sha256", "kind")
                               if key in received})
        return source

    @staticmethod
    def _ordered_decisions(data):
        # Project JSON order is not the order of human instructions. The signed
        # timestamp belongs to each editor-issued receipt.
        from research_edits import verify_receipt
        return sorted((r for r in data.get("decisions", []) if isinstance(r, dict)
                       and type(r.get("at")) in (int, float) and math.isfinite(r["at"])
                       and verify_receipt(r, data["project_key"])),
                      key=lambda r: (r["at"], str(r.get("id", ""))))

    @staticmethod
    def _instructions(data):
        from research_decisions import verify_instruction
        return [r for r in ResearchKnowledge._ordered_decisions(data) if verify_instruction(r, data["project_key"])]

    @classmethod
    def _rejections(cls, data):
        from research_decisions import verify_decision, verify_instruction
        result = {}
        versions = [v for p in data.get("proposals", []) for v in [p, *p.get("history", [])]]
        for receipt in cls._ordered_decisions(data):
            if verify_instruction(receipt, data["project_key"]):
                if receipt["kind"] in ("reopen", "adopt", "family"):
                    result.pop(receipt.get("target") if receipt["kind"] == "reopen" else receipt.get("canonical"), None)
                    if receipt["kind"] in ("adopt", "family") and receipt.get("source_expression"):
                        expression = cls._expression_key(receipt["source_expression"])
                        result = {k: r for k, r in result.items() if cls._expression_key(
                            r.get("action", r).get("source_expression", "")) != expression}
                elif receipt["kind"] == "refuse_expression":
                    result["expression:" + cls._expression_key(receipt["source_expression"])] = receipt
                elif receipt["kind"] == "refuse_name":
                    result[receipt["canonical"]] = receipt
                continue
            if not any(verify_decision(receipt, data["project_key"], {**v, "status": receipt.get("decision")}) for v in versions):
                continue
            if receipt["decision"] == "rejected":
                result[receipt["canonical"]] = receipt
            else:
                result.pop(receipt["canonical"], None)
        return result

    @classmethod
    def _adoptions(cls, data):
        rejected = cls._rejections(data)
        result = {}
        for receipt in cls._instructions(data):
            if receipt["kind"] == "adopt":
                result[receipt["canonical"]] = receipt
            elif receipt["kind"] in ("refuse_name", "reopen"):
                result.pop(receipt.get("target") if receipt["kind"] == "reopen" else receipt["canonical"], None)
            elif receipt["kind"] == "refuse_expression":
                expression = cls._expression_key(receipt["source_expression"])
                result = {name: adopted for name, adopted in result.items()
                          if cls._expression_key(adopted.get("source_expression", "")) != expression}
        return {name: r for name, r in result.items() if name not in rejected
                and not cls._expression_rejected(data, r.get("source_expression", ""))}

    @classmethod
    def _families(cls, data):
        return [r for r in cls._instructions(data) if r["kind"] == "family"
                and not cls._expression_rejected(data, r.get("source_expression", ""))]

    @classmethod
    def _family_names(cls, data):
        from research_language import family_matches
        names = set()
        withdrawn = {r.get("target") if r["kind"] == "reopen" else r["canonical"]: r
                     for r in cls._instructions(data) if r["kind"] in ("refuse_name", "reopen")}
        for receipt in cls._families(data):
            rule = receipt.get("rule", {})
            for index in rule.get("indices", []):
                name = _canonical(rule.get("base", "") + "_{" + index + "}")
                barrier = withdrawn.get(name)
                if family_matches(name, rule) and (barrier is None or
                        (receipt["at"], receipt["id"]) > (barrier["at"], barrier["id"])):
                    names.add(name)
        return names

    @staticmethod
    def _expression_key(text):
        return _canonical(text.strip().strip("$"))

    @classmethod
    def _action_subjects(cls, action, proposals):
        if action["kind"] in ("adopt", "family", "refuse_expression"):
            expression = cls._expression_key(action.get("source_expression", action.get("definition", "")))
            return [proposal for proposal in proposals if expression and expression in {
                cls._expression_key(proposal.get("source_expression", "")),
                cls._expression_key(proposal.get("definition", ""))}]
        return [proposal for proposal in proposals if proposal.get("canonical") == action.get("target")]

    @classmethod
    def _expression_rejected(cls, data, expression):
        key = cls._expression_key(expression)
        return bool(key and any(
            (r.get("kind") == "refuse_expression" or r.get("action", {}).get("rejection_scope") == "expression")
            and cls._expression_key(r.get("action", r).get("source_expression", "")) == key
            for r in cls._rejections(data).values()))

    @classmethod
    def _blocked_abbreviations(cls, data):
        from research_language import family_matches
        blocked = {name for name in cls._rejections(data) if not name.startswith("expression:")}
        for p in data.get("proposals", []):
            if cls._expression_rejected(data, p.get("source_expression", "")):
                blocked.add(p["canonical"])
        for r in cls._instructions(data):
            if not cls._expression_rejected(data, r.get("source_expression", "")):
                continue
            if r["kind"] == "adopt":
                blocked.add(r["canonical"])
            if r["kind"] == "family":
                rule = r["rule"]
                for index in rule["indices"]:
                    name = _canonical(rule["base"] + "_{" + index + "}")
                    if family_matches(name, rule):
                        blocked.add(name)
        return blocked

    def expression_rejected(self, expression):
        data = self._load()
        return bool(data and self._expression_rejected(data, expression))

    def check_expanded_expressions(self, text):
        """Refuse explicit new abbreviations of a literal expression the author rejected."""
        data = self._load()
        if not data:
            return
        from research_language import parse_actions
        interpreted = parse_actions(text, [])
        if interpreted and any(a.get("kind") in ("adopt", "family")
                and self._expression_rejected(data, a.get("source_expression", ""))
                for a in interpreted["actions"]):
            raise Refused("The author requires this expression to remain expanded; keep its original form.")
        for fragment in math_fragments(text, latex=False):
            for separator in ("=", r"\coloneqq", r"\equiv"):
                left, sep, right = fragment.partition(separator)
                if not sep:
                    continue
                for expression, name in ((right, left.rstrip(":")), (left, right)):
                    if not self._expression_rejected(data, expression):
                        continue
                    try:
                        self._proposal_notation(data, name)
                    except Refused:
                        continue
                    raise Refused("The author requires this expression to remain expanded; keep its original form.")
        # The same explicit definition may be written across separate math spans.
        for match in re.finditer(r"\$([^$]+)\$\s*(?:denotes?|means|stands for|表示|代表)\s*\$([^$]+)\$", text, re.I):
            if self._expression_rejected(data, match[2]):
                raise Refused("The author requires this expression to remain expanded; keep its original form.")

    def decide_reply(self, human_text: str, conversation: str) -> dict | None:
        """Interpret actual editor input and atomically save its explicit decisions."""
        return self._decide_reply(human_text, conversation)

    def decide_github_reply(self, subjects, source, *, guard=None):
        """Apply only the exact published subjects witnessed by the issue synchronizer."""
        from research_decisions import verify_github_source
        key = project_key(self.project.root)
        if not verify_github_source(source, key) or subjects != source["publication"]["subjects"]:
            raise Refused("The GitHub reply has no verified publication subject.")
        conversation = f"github:{source['repository']['id']}:{source['issue']['id']}:{source['comment']['id']}:{source['batch_id']}"
        result = self._decide_reply(source["comment"]["body"], conversation,
                                    github_subjects=subjects, github_source=source, guard=guard)
        if result and result.get("purpose") == "notation_decision":
            return {"decisions": [result], "clarifications": []}
        return result or {"decisions": [], "clarifications": []}

    def _decide_reply(self, human_text, conversation, *, github_subjects=None, github_source=None, guard=None):
        from research_decisions import get_subjects, make_decision, make_instruction, parse_reply, set_subject
        from research_decisions import _github_time, github_source_key, verify_decision
        from research_language import parse_actions
        if not isinstance(human_text, str) or len(human_text) > MAX_INPUT:
            raise Refused("The original human message must be text of at most 40000 characters.")
        key = project_key(self.project.root)
        subjects = get_subjects(key, conversation) if github_subjects is None else github_subjects
        parsed_text = human_text
        if github_source is not None:
            lines = human_text.splitlines(keepends=True)
            header = lines[0].rstrip("\r\n") if lines else ""
            batch_header = "prism-batch:" + github_source["batch_id"]
            if header.lstrip().startswith("prism-batch:") and header != batch_header:
                raise Refused("The GitHub reply names a different proposal batch.")
            if header == batch_header:
                parsed_text = "".join(lines[1:])
        if not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise Conflict("The project files are busy; the human decision was not saved.")
        receipts, clarifications = [], []
        try:
            if guard is not None:
                guard()
            data = self._load()
            if not data:
                return None
            if github_source is not None and any(github_source_key(receipt["source"]) == github_source_key(github_source)
                                                 for receipt in self._github_receipts(data, historical=True)):
                return {"decisions": [], "clarifications": []}
            proposals = data.get("proposals", [])
            current, stale = [], set()
            for subject in subjects:
                proposal = next((p for p in proposals if p["id"] == subject["id"]), None)
                if proposal:
                    same = all(proposal.get(k) == subject[k] for k in ("revision", "content_hash"))
                    if not same:
                        stale.add(proposal["canonical"])
                    displayed = next((v for v in [proposal, *proposal.get("history", [])]
                        if all(v.get(k) == subject[k] for k in ("revision", "content_hash"))), proposal)
                    current.append(displayed)
                else:
                    # Keep ordinal positions stable even if a stored item disappeared.
                    current.append({"canonical": "", "status": "pending", "needs_details": True})
                    stale.add("")
            interpreted = parse_actions(parsed_text, current if github_source is not None else current or (
                [] if re.search(r"第.+项|(?:item|proposal)\s+\d+", human_text, re.I) else proposals))
            plain = parse_reply(parsed_text)
            if plain:
                interpreted = None
            pending = [p for p in current if p["status"] == "pending"]
            if interpreted:
                actions, clarifications = interpreted["actions"], interpreted["clarifications"]
            elif plain and len(current) == len(pending) == 1 and not pending[0]["needs_details"]:
                actions = [{"kind": "approve" if plain == "approved" else "reject", "target": pending[0]["canonical"]}]
            elif plain and len(current) > 1:
                actions, clarifications = [], ["Name the items you approve or reject; the batch remains pending."]
            else:
                actions = []
            if github_source is not None:
                scoped = []
                pending_subjects = [p for p in current if p.get("status") == "pending" and not p.get("needs_details")]
                for action in actions:
                    target_subjects = pending_subjects
                    if action["kind"] in ("reopen", "replace"):
                        target_subjects = [p for p in current if p.get("status") in ("pending", "rejected")
                                           and not p.get("needs_details")]
                    elif action["kind"] == "reject":
                        target_subjects = [p for p in current if p.get("status") in ("pending", "approved")
                                           and not p.get("needs_details")]
                    matches = self._action_subjects(action, target_subjects)
                    if len(matches) != 1:
                        clarifications.append("Reply to one complete current proposal in this published batch.")
                        continue
                    if matches[0]["canonical"] in stale:
                        raise Refused("The proposal changed. Review its current version before deciding.")
                    scoped.append({**action, "github_subject": {key: matches[0][key]
                        for key in ("id", "revision", "content_hash")}})
                actions = scoped
            if not actions and not clarifications:
                if github_source is None:
                    set_subject(key, conversation, [])
                return None
            if any(a.get("target") in stale for a in actions):
                raise Refused("The proposal changed. Review its current version before deciding.")
            # Contradictory clauses for the same target cannot silently pick a winner.
            targets = [a.get("target") or a.get("notation") for a in actions]
            repeated = {x for x in targets if x and targets.count(x) > 1}
            if repeated:
                clarifications.append("Clarify the conflicting decisions for " + ", ".join(sorted(repeated)) + ".")
                actions = [a for a in actions if (a.get("target") or a.get("notation")) not in repeated]
            for action in actions:
                kind = action["kind"]
                proposal = next((p for p in proposals if p["canonical"] == action.get("target")), None)
                if kind == "reopen" and (proposal is None or proposal["needs_details"]):
                    if action["target"] not in self._rejections(data):
                        clarifications.append("Only a rejected notation name needs to be reopened.")
                        continue
                    receipt = make_instruction(key, action, human_text, conversation, source=github_source)
                    receipts.append(receipt)
                    data.setdefault("decisions", []).append(receipt)
                    continue
                if kind in ("approve", "reject", "replace", "reopen"):
                    if not proposal or proposal["needs_details"]:
                        clarifications.append("Review the complete current proposal before deciding.")
                        continue
                    if kind == "reopen":
                        if proposal["status"] != "rejected":
                            clarifications.append("Only a rejected proposal needs to be reopened.")
                            continue
                        receipt = make_instruction(key, {**action, "proposal_id": proposal["id"],
                            "revision": proposal["revision"], "content_hash": proposal["content_hash"]}, human_text, conversation,
                            source=github_source)
                        proposal["status"] = "pending"
                        receipts.append(receipt)
                        data.setdefault("decisions", []).append(receipt)
                        continue
                    if proposal["status"] == "rejected" and kind != "replace":
                        clarifications.append("This notation was rejected; explicitly reopen or replace it first.")
                        continue
                    if kind == "reject" and proposal["status"] == "approved" and github_source is not None:
                        previous = next((r for r in reversed(self._ordered_decisions(data))
                            if r.get("decision") == "approved" and verify_decision(r, key, proposal)), None)
                        if previous and previous.get("channel") == "github_comment":
                            before, after = previous["source"]["comment"], github_source["comment"]
                            # Editing the approval is not a new later refusal.
                            if after["id"] == before["id"] or (_github_time(after["created_at"]), after["id"]) <= (
                                    _github_time(before["created_at"]), before["id"]):
                                clarifications.append("Use a new GitHub comment after the approval to reject this notation.")
                                continue
                    if proposal["status"] != "pending" and kind not in ("replace", "reject"):
                        continue
                    decision = "approved" if kind == "approve" else "rejected"
                    decision_action = {**action, "kind": "approve" if kind == "approve" else "reject"}
                    decision_action.setdefault("rejection_scope", "name")
                    if decision_action.get("rejection_scope") == "expression":
                        decision_action["source_expression"] = proposal["source_expression"]
                        decision_action["definition"] = proposal["definition"]
                        if not proposal["source_expression"]:
                            clarifications.append("Specify the original expression that must remain expanded.")
                            continue
                    if proposal["status"] == "pending" or kind == "reject":
                        receipt = make_decision(key, proposal, decision, human_text, conversation,
                                                action=decision_action if interpreted or github_source is not None else None,
                                                source=github_source)
                        proposal["status"] = decision
                        receipts.append(receipt)
                        data.setdefault("decisions", []).append(receipt)
                    if kind != "replace":
                        continue
                    action = {**action, "kind": "adopt"}
                    kind = "adopt"
                if kind in ("adopt", "family"):
                    notation, canonical = self._proposal_notation(data, action.get("notation", ""))
                    if not action.get("definition") or not action.get("scope"):
                        clarifications.append("Specify the notation's intended expression and scope.")
                        continue
                    action = {**action, "notation": notation, "canonical": canonical}
                    # A direct instruction never creates an Agent pending proposal.
                    existing = next((p for p in proposals if p["canonical"] == canonical), None)
                    if (existing and kind == "adopt" and not existing["needs_details"]
                            and existing["candidate_kind"] == "identifier" and (github_source is None
                            or existing["id"] == action["github_subject"]["id"])):
                        existing["status"] = "pending"
                        receipt = make_decision(key, existing, "approved", human_text, conversation,
                            action={"kind": "approve", "target": canonical}, source=github_source)
                        existing["status"] = "approved"
                        receipts.append(receipt); data.setdefault("decisions", []).append(receipt)
                    receipt = make_instruction(key, action, human_text, conversation, source=github_source)
                elif kind == "refuse_expression":
                    action = {**action, "rejection_scope": "expression"}
                    receipt = make_instruction(key, action, human_text, conversation, source=github_source)
                elif kind == "refuse_name":
                    notation, canonical = self._proposal_notation(data, action.get("notation", ""))
                    action = {**action, "notation": notation, "canonical": canonical, "rejection_scope": "name"}
                    receipt = make_instruction(key, action, human_text, conversation, source=github_source)
                else:
                    continue
                receipts.append(receipt)
                data.setdefault("decisions", []).append(receipt)
            if receipts:
                self._refresh_data(data)
                if guard is not None:
                    guard()
                self._write(data)
        finally:
            self.project.files_lock.release()
        if receipts:
            self.project.on_change()
        if len(receipts) == 1 and not clarifications and receipts[0].get("purpose") == "notation_decision":
            return receipts[0]   # preserve the single-reply event contract
        return {"decisions": receipts, "clarifications": clarifications}

    @classmethod
    def _known(cls, data: dict, baseline=None, manual=None) -> set[str]:
        baseline = cls._baseline_occurrences(data) if baseline is None else baseline
        manual = cls._manual_occurrences(data) if manual is None else manual
        evidence = {**baseline, **manual, **cls._approved_occurrences(data)}
        return {_canonical(entry["notation"]) for entry in data["symbols"]
                if _canonical(entry["notation"]) in evidence
                and _canonical(entry["notation"]) not in cls._excluded(data)} | cls._family_names(data)

    @staticmethod
    def _commands(data: dict) -> set[str]:
        commands = declared_identifier_commands(source["text"] for source in data["sources"])
        for item in data["symbols"] + data.get("unverified_symbols", []):
            if isinstance(item, dict) and isinstance(item.get("notation"), str):
                match = re.match(r"\\([A-Za-z]+)", item["notation"])
                if match:
                    commands.add(match[1])
        for receipt in ResearchKnowledge._instructions(data):
            match = re.match(r"\\([A-Za-z]+)", receipt.get("notation", ""))
            if match:
                commands.add(match[1])
        return commands

    def classify(self, text: str, *, latex: bool = True) -> dict:
        """Classify identity only. A changed meaning does not create a new name."""
        data = self._load()
        parsed = math_notations(text, known_commands=self._commands(data) if data else (), latex=latex)
        known = self._known(data) - self._blocked_abbreviations(data) if data else set()
        return {"known": [item for item in parsed["symbols"] if item["canonical"] in known],
                "unknown": [item for item in parsed["symbols"] if item["canonical"] not in known],
                "gaps": parsed["gaps"]}

    def _manuscript(self) -> tuple[list[dict], list[dict]]:
        cfg = self.project.config
        if not cfg.named_main:
            return [], []
        sources, gaps, total, visited = [], [], 0, set()

        def visit(rel):
            nonlocal total
            try:
                path = self.project.resolve(rel)
            except (OSError, ValueError, RuntimeError) as error:
                gaps.append({"path": rel, "line": 0, "reason": str(error)})
                return
            rel = self.project.rel(path)
            if rel in visited:
                return
            visited.add(rel)
            if len(visited) > MAX_SOURCES:
                gaps.append({"path": rel, "line": 0, "reason": "The current manuscript source limit was reached."})
                return
            try:
                if path.stat().st_size > MAX_TEXT:
                    raise Refused("The current source exceeds the editor's text size limit.")
                raw = read_plain(path, MAX_TEXT)
                total += len(raw)
                if len(raw) > MAX_TEXT or total > MAX_TOTAL_TEXT:
                    raise Refused("The current manuscript text limit was reached.")
                text = raw.decode("utf-8")
            except (OSError, ValueError, UnicodeError) as error:
                gaps.append({"path": rel, "line": 0, "reason": str(error)})
                return
            sources.append({"path": rel, "sha256": hashlib.sha256(raw).hexdigest(), "text": text})
            for match in INCLUDE.finditer(strip_comments(text)):
                name = match.group(1).strip()
                child = self.project.resolve_tex_name(name)
                if child:
                    visit(child)
                else:
                    gaps.append({"path": name, "line": 0, "reason": "Included source was not found."})

        # Resolve before reading. The general document-order cache can scan paths
        # before checking exclusions, which is insufficient evidence for this record.
        visit(cfg.project_main)
        return sources, gaps

    @staticmethod
    def _notice(data: dict, kind: str, entry: dict) -> None:
        key = hashlib.sha256((kind + "\0" + entry["canonical"]).encode()).hexdigest()[:24]
        if any(n.get("id") == key for n in data["maintenance_notices"] if isinstance(n, dict)):
            return
        messages = {"baseline_added": "Added omitted notation from the initialization version.",
                    "decision_added": "Added notation after the author's verified chat decision.",
                    "manual_added": "Added notation from a verified manual edit.",
                    "unverified": "Notation has no verified adoption source; confirmation is required before AI use."}
        if kind == "decision_added" and any(source.get("evidence") == "github_decision" for source in entry["sources"]):
            messages[kind] = "Added notation after a designated human's verified GitHub decision."
        if kind == "decision_added" and any(source.get("evidence") == "author_confirmation" for source in entry["sources"]):
            messages[kind] = "Added notation after the author confirmed its adoption from received source text."
        data["maintenance_notices"].append({"id": key, "kind": kind, "notation": entry["notation"],
                                           "canonical": entry["canonical"], "sources": entry["sources"],
                                           "message": messages[kind]})

    @classmethod
    def _register(cls, data: dict, entries: dict, kind: str) -> None:
        existing = {_canonical(item["notation"]): item for item in data["symbols"]}
        excluded = cls._excluded(data)
        for canonical, item in entries.items():
            if canonical in excluded:
                continue
            if canonical in existing:
                # An unsupported, copied status field is not its own source evidence.
                entry = existing[canonical]
                entry["canonical"] = canonical
                for source in item["sources"]:
                    if source not in entry["sources"]:
                        entry["sources"].append(source)
                continue
            entry = {"notation": item["notation"], "canonical": canonical, "meaning": item.get("meaning"),
                     "status": "documented" if item.get("meaning") else "unresolved", "sources": item["sources"]}
            data["symbols"].append(entry)
            cls._notice(data, kind, entry)

    def _refresh_data(self, data: dict) -> None:
        self._incremental_defaults(data)
        baseline, manual = self._baseline_occurrences(data), self._manual_occurrences(data)
        approved = self._approved_occurrences(data)
        evidence, excluded = {**baseline, **manual, **approved}, self._excluded(data)
        data["symbols"] = [item for item in data["symbols"]
                           if _canonical(item["notation"]) in evidence
                           and _canonical(item["notation"]) not in excluded]
        self._register(data, baseline, "baseline_added")
        self._register(data, manual, "manual_added")
        self._register(data, approved, "decision_added")
        known, excluded = self._known(data, baseline, manual), self._excluded(data)
        sources, gaps = self._manuscript()
        commands = declared_identifier_commands(source["text"] for source in sources) | self._commands(data)
        unknown = {}
        for source in sources:
            parsed = math_notations(source["text"], known_commands=commands)
            gaps.extend({"path": source["path"], **item} for item in parsed["gaps"])
            for item in parsed["symbols"]:
                if item["canonical"] in known or item["canonical"] in excluded:
                    continue
                evidence = {"path": source["path"], "line": item["line"], "sha256": source["sha256"],
                            "evidence": "unknown"}
                entry = unknown.setdefault(item["canonical"], {"notation": item["notation"],
                    "canonical": item["canonical"], "status": "unverified", "sources": []})
                if evidence not in entry["sources"]:
                    entry["sources"].append(evidence)
        data["unverified_symbols"] = list(unknown.values())
        # Initialization already reports unread/excluded sources and parser gaps.
        # Repeating the same fact is not an incremental change or a reason to save.
        def gap_key(item):
            return json.dumps({"path": item.get("path"), "line": item.get("line", 0),
                               "reason": item.get("reason"), "notation": item.get("notation")},
                              ensure_ascii=False, sort_keys=True)
        covered = {gap_key(item) for kind in ("gaps", "unread", "excluded")
                   for item in data["coverage"].get(kind, []) if isinstance(item, dict)}
        data["incremental_gaps"] = [gap for gap in gaps if gap_key(gap) not in covered]
        for entry in data["unverified_symbols"]:
            self._notice(data, "unverified", entry)

    def refresh(self) -> dict:
        if not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise Conflict("The project files are busy; research maintenance will retry later.")
        changed = False
        try:
            data = self._load()
            if not data:
                return {"initialized": False}
            if self.project.turn.busy():
                return self._report(data)
            before = copy.deepcopy(data)
            self._refresh_data(data)
            changed = data != before
            if changed:
                self._write(data, passive=True)
        finally:
            self.project.files_lock.release()
        if changed:
            self.project.on_change()
        return self._report(data)

    def add_manual(self, receipt: dict) -> dict:
        """Register only the added notations in a locally verified save receipt."""
        if not isinstance(receipt, dict) or not _verify_manual(receipt, project_key(self.project.root)):
            raise Refused("The manual editing receipt is not verified for this project.")
        if not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise Conflict("The project files are busy; the manual edit was not registered.")
        changed = False
        try:
            data = self._load()
            if not data:
                return {"initialized": False}
            self._incremental_defaults(data)
            prior = next((r for r in data["manual_evidence"] if isinstance(r, dict)
                          and r.get("id") == receipt.get("id")), None)
            if prior is not None:
                if prior != receipt:
                    raise Refused("This manual editing receipt has conflicting contents.")
                return self._report(data)
            sources, _ = self._manuscript()
            source = next((s for s in sources if s["path"] == receipt.get("path")), None)
            if source is None:
                return self._report(data)       # editing a reference does not adopt its names
            if source["sha256"] != receipt.get("after_sha256"):
                raise Refused("The source changed after the verified manual edit.")
            parsed = math_notations(source["text"], known_commands=declared_identifier_commands([source["text"]])
                                    | self._commands(data))
            actual = {(item["canonical"], item["line"]) for item in parsed["symbols"]}
            if (not isinstance(receipt.get("added"), list)
                    or any(not isinstance(item, dict) or not isinstance(item.get("notation"), str)
                           or item.get("canonical") != _canonical(item["notation"])
                           or (item.get("canonical"), item.get("line")) not in actual
                           for item in receipt["added"])):
                raise Refused("The manual editing receipt does not match the saved notation.")
            if not receipt["added"]:
                return self._report(data)
            before = copy.deepcopy(data)
            data["manual_evidence"].append(copy.deepcopy(receipt))
            self._refresh_data(data)
            changed = data != before
            if changed:
                self._write(data)
        finally:
            self.project.files_lock.release()
        if changed:
            self.project.on_change()
        return self._report(data)

    @staticmethod
    def _proposal_hash(proposal: dict) -> str:
        fields = {key: proposal[key] for key in ("project_key", "canonical", "definition", "scope", "reason",
                  "source_expression", "needs_details", "candidate_kind")}
        fields["origin"] = {key: proposal["origin"][key] for key in ("actor", "channel", "path")
                            if key in proposal["origin"]}
        return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()

    @classmethod
    def _proposal_records(cls, data: dict, *, create: bool = False) -> list[dict]:
        proposals = data.get("proposals", [])
        if not isinstance(proposals, list):
            raise Refused("The notation proposal records are damaged.")
        ids, canonicals = set(), set()
        for proposal in proposals:
            if (not isinstance(proposal, dict) or not isinstance(proposal.get("id"), str)
                    or not proposal["id"] or proposal["id"] in ids
                    or proposal.get("project_key") != data["project_key"]
                    or type(proposal.get("revision")) is not int or proposal["revision"] < 1
                    or proposal.get("status") not in ("pending", "approved", "rejected")
                    or type(proposal.get("needs_details")) is not bool
                    or proposal.get("candidate_kind") not in ("identifier", "unresolved_fragment")
                    or not all(isinstance(proposal.get(key), str) for key in
                               ("notation", "canonical", "definition", "scope", "reason", "source_expression"))
                    or not proposal["canonical"] or proposal["canonical"] in canonicals
                    or proposal["canonical"] != _canonical(proposal["notation"])
                    or not isinstance(proposal.get("origin"), dict)
                    or proposal["origin"].get("actor") != "agent"
                    or not isinstance(proposal["origin"].get("channel"), str)
                    or not isinstance(proposal.get("history", []), list)
                    or any(type(proposal.get(key)) not in (int, float) or not proposal[key] > 0
                           for key in ("created_at", "updated_at"))
                    or proposal.get("content_hash") != cls._proposal_hash(proposal)):
                raise Refused("A notation proposal has damaged contents or an unsupported decision state.")
            ids.add(proposal["id"])
            canonicals.add(proposal["canonical"])
            if proposal["status"] != "pending":
                from research_decisions import verify_decision
                if not any(verify_decision(receipt, data["project_key"], proposal)
                           and receipt.get("decision") == proposal["status"]
                           for receipt in data.get("decisions", [])):
                    raise Refused("The notation decision has no verified human message evidence.")
        # Status is a display mirror. Replay signed decisions for the current
        # version, so changing it to pending cannot erase an author's refusal.
        from research_decisions import verify_decision, verify_instruction
        for proposal in proposals:
            for receipt in cls._ordered_decisions(data):
                if verify_decision(receipt, data["project_key"], {**proposal, "status": receipt.get("decision")}):
                    proposal["status"] = receipt["decision"]
                elif (verify_instruction(receipt, data["project_key"]) and receipt["kind"] == "reopen"
                        and receipt.get("proposal_id") == proposal["id"]
                        and receipt.get("revision") == proposal["revision"]
                        and receipt.get("content_hash") == proposal["content_hash"]):
                    proposal["status"] = "pending"
        if create:
            data["proposals"] = proposals
        return proposals

    def _proposal_origin(self, origin=None) -> dict:
        if origin is not None and not isinstance(origin, dict):
            raise Refused("The proposal origin must describe its Agent channel.")
        origin = origin or {}
        channel = origin.get("channel", "api")
        if not isinstance(channel, str) or not channel.strip() or len(channel) > 80:
            raise Refused("The proposal channel must be short text.")
        result = {"actor": "agent", "channel": channel.strip()}
        if origin.get("path") is not None:
            if not isinstance(origin["path"], str):
                raise Refused("The proposal source path must name a project file.")
            result["path"] = self.project.rel(self.project.resolve(origin["path"]))
        for key in ("job", "turn", "provider", "source_sha256"):
            if key in origin:
                value = origin[key]
                if type(value) not in (str, int) or len(str(value)) > 200:
                    raise Refused("Invalid proposal origin metadata.")
                result[key] = value
        return result

    @staticmethod
    def _bare_notation(notation: str) -> str:
        if not isinstance(notation, str) or not notation.strip() or len(notation) > 4096:
            raise Refused("A proposal must name one complete notation.")
        notation = notation.strip()
        for opening, closing in (("$$", "$$"), ("$", "$"), (r"\(", r"\)"), (r"\[", r"\]")):
            if notation.startswith(opening) and notation.endswith(closing) and len(notation) > len(opening + closing):
                return notation[len(opening):-len(closing)].strip()
        return notation

    @classmethod
    def _proposal_notation(cls, data: dict, notation: str) -> tuple[str, str]:
        notation = cls._bare_notation(notation)
        command = re.match(r"\\([A-Za-z]+)", notation)
        if command and command[1] in SOURCE_COMMANDS:
            raise Refused("LaTeX source commands are not proposed mathematical identifiers.")
        commands = cls._commands(data)
        if command:
            # Explicitly proposing an unknown identifier macro does not infer its
            # expansion or add it to the known library.
            commands.add(command[1])
        parsed = math_notations("$" + notation + "$", known_commands=commands)
        if (parsed["gaps"] or len(parsed["symbols"]) != 1
                or parsed["symbols"][0]["canonical"] != _canonical(notation)):
            raise Refused("A proposal must identify one complete symbol, not an operator, layout command or expression.")
        return notation, parsed["symbols"][0]["canonical"]

    def _store_proposal(self, data: dict, contents: dict, *, needs_details: bool,
                        candidate_kind: str = "identifier") -> tuple[dict, bool]:
        proposals = self._proposal_records(data, create=True)
        if contents["canonical"] in self._blocked_abbreviations(data):
            raise Refused("The author requires this expression to remain expanded; keep its original form.")
        current = next((proposal for proposal in proposals if proposal["canonical"] == contents["canonical"]), None)
        if current is not None and current["status"] == "rejected":
            return copy.deepcopy(current), False
        if current is not None and needs_details and not current["needs_details"]:
            return copy.deepcopy(current), False       # an intercepted draft cannot erase a complete proposal
        now = time.time()
        candidate = {**contents, "project_key": data["project_key"], "status": "pending",
                     "needs_details": needs_details, "candidate_kind": candidate_kind}
        candidate["content_hash"] = self._proposal_hash(candidate)
        if current is not None and candidate["content_hash"] == current["content_hash"]:
            return copy.deepcopy(current), False
        if current is None:
            candidate.update(id=uuid.uuid4().hex, revision=1, created_at=now, updated_at=now, history=[])
            proposals.append(candidate)
        else:
            previous = {key: copy.deepcopy(value) for key, value in current.items() if key != "history"}
            candidate.update(id=current["id"], revision=current["revision"] + 1,
                             created_at=current["created_at"], updated_at=now,
                             history=[*current.get("history", []), previous])
            proposals[proposals.index(current)] = candidate
        return copy.deepcopy(candidate), True

    def propose(self, notation: str, definition: str, scope: str, reason: str,
                source_expression: str = "", origin=None) -> dict:
        return self.propose_many([dict(notation=notation, definition=definition, scope=scope,
                                      reason=reason, source_expression=source_expression)], origin=origin)[0]

    def propose_many(self, items: list[dict], origin=None) -> list[dict]:
        """Save a displayed batch atomically; no candidate grants adoption authority."""
        if not isinstance(items, list) or not items or len(items) > 100 or not all(isinstance(x, dict) for x in items):
            raise Refused("A notation batch must contain between 1 and 100 proposals.")
        origin = self._proposal_origin(origin)
        if not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise Conflict("The project files are busy; the notation proposals were not saved.")
        changed, result = False, []
        try:
            data = self._load()
            if data is None:
                raise Refused("Initialize the project's research baseline before proposing notation.")
            for item in items:
                for field in ("definition", "scope", "reason", "source_expression"):
                    value = item.get(field, "")
                    if not isinstance(value, str) or len(value) > MAX_INPUT or (field != "source_expression" and not value.strip()):
                        raise Refused(f"The proposal's {field} must be readable text of at most {MAX_INPUT} characters.")
                notation, canonical = self._proposal_notation(data, item.get("notation", ""))
                if canonical in self._known(data):
                    raise Refused("Known notation does not need a proposal; its use may change without approval.")
                if (self._expression_rejected(data, item.get("source_expression", "")) or any(
                        r.get("action", {}).get("rejection_scope") == "expression"
                        and r["action"].get("definition") == item.get("definition")
                        for r in self._rejections(data).values())):
                    raise Refused("The author requires this expression to remain expanded; explicitly reopen or replace it first.")
                proposal, stored = self._store_proposal(data, {"notation": notation, "canonical": canonical,
                    **{field: item.get(field, "").strip() for field in ("definition", "scope", "reason", "source_expression")},
                    "origin": origin}, needs_details=False)
                result.append(proposal)
                changed |= stored
            if changed:
                self._write(data, explicit=True)
        finally:
            self.project.files_lock.release()
        if changed:
            self.project.on_change()
        return result

    def hold_notation(self, occurrences, *, channel: str, path=None,
                      source_expression: str = "", details: str = "") -> list[dict]:
        """Retain intercepted candidates without inventing an intended definition."""
        if (not isinstance(occurrences, list) or not all(isinstance(item, dict) for item in occurrences)
                or not isinstance(source_expression, str) or len(source_expression) > MAX_INPUT
                or not isinstance(details, str) or len(details) > MAX_INPUT):
            raise Refused("Invalid held notation candidates.")
        origin = self._proposal_origin({"channel": channel, "path": path})
        if not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise Conflict("The project files are busy; the notation candidates were not saved.")
        changed, result = False, []
        try:
            data = self._load()
            if data is None:
                raise Refused("Initialize the project's research baseline before recording notation candidates.")
            if self._expression_rejected(data, source_expression):
                raise Refused("The author requires this expression to remain expanded; keep its original form.")
            known, seen = self._known(data), set()
            for occurrence in occurrences:
                raw = occurrence.get("notation")
                if not isinstance(raw, str) or not raw.strip():
                    continue
                raw = self._bare_notation(raw)
                try:
                    notation, canonical = self._proposal_notation(data, raw)
                    targets = [(notation, canonical, "identifier")]
                except Refused:
                    parsed = math_notations("$" + raw + "$", known_commands=self._commands(data))
                    targets = [(item["notation"], item["canonical"], "identifier")
                               for item in parsed["symbols"] if item["canonical"] not in known]
                    if parsed["gaps"]:
                        targets.append((raw, _canonical(raw), "unresolved_fragment"))
                for notation, canonical, kind in targets:
                    if canonical in known or canonical in seen:
                        continue
                    seen.add(canonical)
                    proposal, stored = self._store_proposal(data, {"notation": notation, "canonical": canonical,
                        "definition": "Not recorded in a complete notation proposal; clarification is required.",
                        "scope": f"Manuscript file {origin['path']}" if "path" in origin else f"{origin['channel']} output",
                        "reason": details.strip() or "The generated draft contains notation without a verified adoption source.",
                        "source_expression": source_expression.strip(), "origin": origin},
                        needs_details=True, candidate_kind=kind)
                    result.append(proposal)
                    changed |= stored
            if changed:
                self._write(data)
        finally:
            self.project.files_lock.release()
        if changed:
            self.project.on_change()
        return result

    def _write(self, data: dict, *, initializing=False, passive=False, explicit=False) -> None:
        provenance = data.get("provenance", {})
        if provenance.get("write_blocked") and not passive:
            raise Refused("Resolve the received research record before saving local changes.")
        clean = copy.deepcopy(data)
        clean.pop("provenance", None)
        if clean.get("initialized"):
            self._validate_record(clean)
        raw = (json.dumps(clean, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + "\n").encode("utf-8")
        if len(raw) > MAX_RECORD:
            raise Refused("The research baseline is too large; reduce its input set before initializing.")
        if not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise Conflict("The project files are busy; the research change was not saved.")
        try:
            store = Provenance(self.project.root, RECORD, MAX_RECORD)
            observation = store.inspect()
            expected = provenance.get("received_sha256", observation["sha256"])
            received = observation.get("received") or {}
            old = observation["entry"]["data"] if observation.get("entry") else {}
            new_event = self._new_verified_event(clean, old, received)
            private_only = (not clean.get("initialized") or provenance.get("record_state") != "local" and not initializing
                            and (passive or not explicit and not new_event))
            changed = store.save(clean, expected, lambda value: replace_file(self._path(), value, durable=True),
                initializing=initializing, private_only=private_only,
                expected_revision=provenance.get("revision", observation["entry"]["receipt"]["data_sha256"]
                                                 if observation["entry"] else None))
            if changed:
                self.project.turn.server_wrote([RECORD])
            current = self._load()
            if current is not None:
                data["provenance"] = current["provenance"]
        finally:
            self.project.files_lock.release()

    @classmethod
    def _new_verified_event(cls, data, old, received):
        from research_decisions import verify_decision, verify_instruction
        from research_edits import verify_receipt
        def configuration_receipt(record):
            discussion = record.get("discussion")
            github = discussion.get("github") if isinstance(discussion, dict) else None
            return github.get("receipt") if isinstance(github, dict) else None
        key = data["project_key"]
        for field in ("decisions", "manual_evidence"):
            seen = {item.get("id") for source in (old, received)
                    for item in (source.get(field) if isinstance(source.get(field), list) else [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str)}
            for receipt in data.get(field, []):
                if not isinstance(receipt, dict) or receipt.get("id") in seen:
                    continue
                if field == "manual_evidence" and _verify_manual(receipt, key):
                    return True
                if verify_instruction(receipt, key) or any(verify_decision(receipt, key, proposal)
                                                         for proposal in data.get("proposals", [])):
                    return True
        receipt = configuration_receipt(data)
        seen = [configuration_receipt(source) for source in (old, received)]
        return bool(isinstance(receipt, dict) and receipt not in seen and verify_receipt(receipt, key))

    def initialize(self, question: str = "", notation: str = "") -> dict:
        if not isinstance(question, str) or not isinstance(notation, str) or max(len(question), len(notation)) > MAX_INPUT:
            raise Refused("The research question and notation must be text of at most 40000 characters each.")
        if not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise Conflict("The project files are busy; try initialization again.")
        try:
            existing = self._load()
            if existing and existing.get("provenance", {}).get("baseline_verified"):
                return self._report(existing)
            if existing and existing.get("provenance", {}).get("write_blocked"):
                raise Refused("Resolve the received research record before initializing it.")
            if self.project.turn.busy():
                raise Conflict("Wait until the current Agent Turn finishes before initializing research.")
            cfg = self.project.config
            coverage = {"main": cfg.named_main, "read": [], "excluded": [], "unread": [], "gaps": []}
            sources, symbol_map, term_map, visited = [], {}, {}, set()
            coverage["gaps"].append({"path": "", "line": 0, "reason": "Conservative source extraction: only explicit marked terms and supported mathematical spans are indexed; inspect the report for omissions."})

            def record(text: str, rel: str, evidence: str, digest: str) -> None:
                parsed = math_notations(text)
                coverage["gaps"].extend({"path": rel, **g} for g in parsed["gaps"])
                for item in parsed["symbols"]:
                    key = item["canonical"]
                    source = {"path": rel, "line": item["line"], "sha256": digest, "evidence": evidence}
                    if key not in symbol_map:
                        symbol_map[key] = {"notation": item["notation"], "canonical": key, "meaning": None,
                                           "status": "unresolved", "sources": []}
                    if source not in symbol_map[key]["sources"]:
                        symbol_map[key]["sources"].append(source)
                for item in _notation_definitions(text):
                    entry = symbol_map.get(item["canonical"])
                    if entry is None:
                        continue
                    source = {"path": rel, "line": item["line"], "sha256": digest,
                              "evidence": evidence, "definition": item["definition"], "meaning": item["meaning"]}
                    entry.setdefault("definition_sources", []).append(source)
                    if entry["meaning"] is None:
                        entry.update(meaning=item["meaning"], status="documented")
                    elif entry["meaning"] != item["meaning"]:
                        entry["status"] = "conflicting"
                        entry.setdefault("definitions", [entry["meaning"]])
                        if item["meaning"] not in entry["definitions"]:
                            entry["definitions"].append(item["meaning"])
                for item in _terms(text):
                    key = item["name"]
                    source = {"path": rel, "line": item["line"], "sha256": digest, "evidence": evidence}
                    entry = term_map.setdefault(key, {"name": key, "meaning": item["meaning"],
                                                    "status": "documented" if item["meaning"] else "unresolved", "sources": []})
                    if item["meaning"] and entry["meaning"] and item["meaning"] != entry["meaning"]:
                        entry["status"] = "conflicting"
                        entry.setdefault("definitions", [entry["meaning"]])
                        if item["meaning"] not in entry["definitions"]:
                            entry["definitions"].append(item["meaning"])
                    elif item["meaning"] and not entry["meaning"]:
                        entry["meaning"], entry["status"] = item["meaning"], "documented"
                    if source not in entry["sources"]:
                        entry["sources"].append(source)

            def visit(rel: str) -> None:
                try:
                    path = self.project.resolve(rel)
                except (ValueError, OSError, RuntimeError) as error:
                    coverage["excluded"].append({"path": rel, "reason": str(error)})
                    return
                rel = self.project.rel(path)
                if rel in visited:
                    return
                visited.add(rel)
                if len(visited) > MAX_SOURCES:
                    coverage["unread"].append({"path": rel, "reason": "The initialization source limit was reached."})
                    return
                try:
                    if path.stat().st_size > MAX_TEXT:
                        coverage["unread"].append({"path": rel, "reason": "The source exceeds the editor's text size limit."})
                        return
                    raw = read_plain(path, MAX_TEXT)
                    if len(raw) > MAX_TEXT:
                        coverage["unread"].append({"path": rel, "reason": "The source exceeds the editor's text size limit."})
                        return
                    text = raw.decode("utf-8")
                except (OSError, ValueError, UnicodeError) as error:
                    coverage["unread"].append({"path": rel, "reason": str(error)})
                    return
                digest = hashlib.sha256(raw).hexdigest()
                if sum(s["bytes"] for s in sources) + len(raw) > MAX_TOTAL_TEXT:
                    coverage["unread"].append({"path": rel, "reason": "The initialization total text limit was reached."})
                    return
                sources.append({"path": rel, "sha256": digest, "bytes": len(raw), "text": text})
                coverage["read"].append(rel)
                record(text, rel, "baseline", digest)
                for match in INCLUDE.finditer(strip_comments(text)):
                    name = match.group(1).strip()
                    # Reuse the editor's TeX name lookup, but check all resolved paths
                    # through Project.resolve before reading any content.
                    child = self.project.resolve_tex_name(name)
                    if child:
                        visit(child)
                    else:
                        requested = name if Path(name).suffix else name + ".tex"
                        try:
                            self.project.resolve(requested)
                        except (ValueError, OSError, RuntimeError) as error:
                            coverage["excluded"].append({"path": name, "reason": str(error)})
                        else:
                            coverage["unread"].append({"path": name, "reason": "Included source was not found."})

            if cfg.named_main:
                visit(cfg.project_main)
            excluded = {s["path"] for s in coverage["excluded"]}
            for rel in self.project.list_files():
                if (Path(rel).suffix in (".tex", ".sty", ".cls", ".bib")
                        and rel not in visited and rel not in excluded):
                    coverage["excluded"].append({"path": rel, "reason": "Not included in the current manuscript; not used as baseline evidence."})
            if cfg.error:
                coverage["gaps"].append({"path": "prism.json", "line": 0, "reason": cfg.error})
            if notation.strip():
                # This input is explicitly supplied notation, so a bare formula is valid.
                supplied = notation if MATH_START.search(notation) else "$" + notation + "$"
                record(supplied, "author input", "human_input", hashlib.sha256(notation.encode()).hexdigest())
            data = {"initialized": True, "schema": SCHEMA, "project_key": project_key(self.project.root),
                    "initialized_at": time.time(), "question": question, "explicit_notation": notation,
                    "discussion": {"mode": "markdown"}, "sources": sources, "coverage": coverage,
                    "terms": list(term_map.values()), "symbols": list(symbol_map.values()), "corrections": [],
                    "manual_evidence": [], "unverified_symbols": [], "maintenance_notices": [], "incremental_gaps": [],
                    "proposals": []}
            data["provenance"] = (copy.deepcopy(existing["provenance"]) if existing else {"received_sha256": None, "revision": None})
            from research_topics import retain_received_topics
            topic_references = retain_received_topics(self, existing)
            if topic_references:
                data["discussion"]["topic_references"] = topic_references
            if existing is not None:
                for field in ("manual_evidence", "decisions", "proposals", "corrections", "maintenance_notices"):
                    if field in existing:
                        data[field] = copy.deepcopy(existing[field])
                self._refresh_data(data)
            self._write(data, initializing=True)
        finally:
            self.project.files_lock.release()
        self.project.on_change()
        return self._report(data)

    def correct(self, term_updates: list[dict] | None = None, excluded_symbols: list[str] | None = None,
                notes: str = "", *, symbol_updates: list[dict] | None = None) -> dict:
        term_updates = [] if term_updates is None else term_updates
        symbol_updates = [] if symbol_updates is None else symbol_updates
        excluded_symbols = [] if excluded_symbols is None else excluded_symbols
        if (not isinstance(term_updates, list) or not isinstance(symbol_updates, list)
                or not isinstance(excluded_symbols, list)
                or not all(isinstance(n, str) for n in excluded_symbols)
                or not isinstance(notes, str) or len(notes) > MAX_INPUT):
            raise Refused("Invalid research report corrections.")
        if not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise Conflict("The project files are busy; try saving the corrections again.")
        try:
            if self.project.turn.busy():
                raise Conflict("Wait until the current Agent Turn finishes before correcting the report.")
            data = self._load()
            if not data:
                raise Refused("Initialize the project's research baseline first.")
            terms = {t["name"]: t for t in data["terms"]}
            for update in term_updates:
                if (not isinstance(update, dict) or update.get("name") not in terms
                        or not isinstance(update.get("meaning"), str) or len(update["meaning"]) > MAX_INPUT):
                    raise Refused("A correction must name an extracted term and provide its meaning as text.")
                terms[update["name"]].update(meaning=update["meaning"].strip() or None, status="corrected")
            symbols = {s["notation"]: s for s in data["symbols"]}
            for update in symbol_updates:
                if (not isinstance(update, dict) or not isinstance(update.get("notation"), str)
                        or update["notation"] not in symbols or set(update) - {"notation", "meaning"}
                        or not isinstance(update.get("meaning"), str) or len(update["meaning"]) > MAX_INPUT):
                    raise Refused("A correction must name an extracted notation and provide its meaning as text.")
                symbols[update["notation"]].update(meaning=update["meaning"].strip() or None, status="corrected")
            known = {s["notation"] for s in data["symbols"]}
            if any(n not in known for n in excluded_symbols):
                raise Refused("A symbol correction must name an extracted notation.")
            removed = [s for s in data["symbols"] if s["notation"] in excluded_symbols]
            data["symbols"] = [s for s in data["symbols"] if s["notation"] not in excluded_symbols]
            if not term_updates and not symbol_updates and not excluded_symbols and not notes.strip():
                return self._report(data)
            data["corrections"].append({"at": time.time(), "term_updates": term_updates,
                                        "symbol_updates": symbol_updates,
                                        "excluded_symbols": excluded_symbols, "removed_symbols": removed, "notes": notes})
            self._write(data, explicit=True)
        finally:
            self.project.files_lock.release()
        self.project.on_change()
        return self._report(data)
