"""Ordinary author confirmation of exact, received source versions.

A question identifies an unverified occurrence. It grants no permission. A later
real editor message may adopt its name without claiming a verified manual edit.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets

from errors import Conflict, Refused
from fsutil import replace_file
from guarded_compile import _read_plain
from notation import math_notations
from project import MAX_TEXT, SAVE_WAIT
import registry
from research_decisions import get_subjects, make_instruction, parse_reply, verify_instruction
from research_edits import sign_receipt, verify_receipt
from research_language import parse_actions
from research import MAX_RECORD, RECORD
from research_provenance import Provenance, read_plain

MAX_STATE = 8 * 1024 * 1024
MAX_QUESTIONS = 1000
ORDINAL = re.compile(r"第[一二三四五六七八九十\d]+项|\b(?:item|proposal)\s+\d+\b", re.I)
REOPEN = re.compile(r"^\s*(?:reopen\b|重新(?:考虑|打开|讨论))", re.I)


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _path(knowledge):
    folder = registry.state_dir() / "research-confirmations"
    path = folder / (registry.project_key(knowledge.project.root) + ".json")
    if folder.is_symlink() or path.is_symlink():
        raise Refused("Received notation questions need regular private state files.")
    return path


def _read(knowledge):
    path = _path(knowledge)
    key = registry.project_key(knowledge.project.root)
    try:
        raw = _read_plain(registry.state_dir().resolve(), path.relative_to(registry.state_dir()).as_posix(), MAX_STATE)
        data = json.loads(raw)
    except FileNotFoundError:
        return {"schema": 1, "project_key": key, "questions": {}, "contexts": {}, "library_demands": {}}
    except (UnicodeError, json.JSONDecodeError) as error:
        raise Refused("The received notation question state is damaged.") from error
    if (not isinstance(data, dict) or data.get("schema") != 1 or data.get("project_key") != key
            or any(not isinstance(data.get(name), dict) for name in ("questions", "contexts"))
            or len(data["questions"]) > MAX_QUESTIONS):
        raise Refused("The received notation question state belongs to another project or is damaged.")
    if any(not isinstance(context, dict) or not isinstance(context.get("blocked"), list)
            or len(context["blocked"]) > MAX_QUESTIONS
            or any(not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity) for identity in context["blocked"])
            for context in data["contexts"].values()):
        raise Refused("The received notation reply contexts are damaged.")
    data.setdefault("library_demands", {})
    if not isinstance(data["library_demands"], dict):
        raise Refused("The received library question context is damaged.")
    return data


def _write(knowledge, data):
    raw = _json(data) + b"\n"
    if len(raw) > MAX_STATE:
        raise Refused("The received notation question state exceeds its limit.")
    path = _path(knowledge)
    if path.exists() and _read_plain(registry.state_dir().resolve(), path.relative_to(registry.state_dir()).as_posix(), MAX_STATE) == raw:
        return
    replace_file(path, raw, durable=True)


def _observation(knowledge, data):
    store = Provenance(knowledge.project.root, RECORD, MAX_RECORD)
    observed = store.inspect()
    if observed["sha256"] != data.get("provenance", {}).get("received_sha256"):
        raise Conflict("The received research record changed while its notation was checked.")
    return store, observed


def _library_rows(knowledge, data, raw, sha256, requested):
    received = json.loads(raw)
    rows = received.get("symbols", []) if isinstance(received, dict) else []
    result = {}
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict) or not isinstance(row.get("notation"), str):
            continue
        try:
            notation, canonical = knowledge._proposal_notation(data, row["notation"])
        except ValueError:
            continue
        if canonical not in requested:
            continue
        entry = result.setdefault(canonical, {"notation": notation, "canonical": canonical, "definition": "", "sources": []})
        entry["sources"].append({"kind": "received_library", "path": RECORD, "sha256": sha256,
            "json_pointer": f"/symbols/{index}", "entry_sha256": _digest(_json(row)), "evidence": "unverified"})
    return result


def _library_entries(knowledge, data, known, rejected, include_rejected):
    progress = _read(knowledge)
    demands = progress["library_demands"]
    if not demands or data.get("provenance", {}).get("write_blocked"):
        return []
    root = str(knowledge.project.root.resolve())
    for canonical, demand in demands.items():
        if (not isinstance(demand, dict) or demand.get("kind") != "received_library_reference"
                or demand.get("root") != root or demand.get("canonical") != canonical
                or not verify_receipt(demand, registry.project_key(knowledge.project.root))
                or not isinstance(demand.get("sources"), list)):
            raise Refused("The received library reference has no private source witness.")
    store, observed = _observation(knowledge, data)
    requested = set(demands) - known
    if not include_rejected:
        requested -= set(rejected)
    if data["provenance"]["record_state"] != "local":
        return list(_library_rows(knowledge, data, observed["raw"], observed["sha256"], requested).values()) if observed["raw"] is not None and not observed["error"] else []
    result = []
    for canonical in sorted(requested):
        demand = demands[canonical]
        sources = demand["sources"]
        valid = bool(sources)
        for source in sources:
            if (not isinstance(source, dict) or source.get("kind") != "received_library" or source.get("path") != RECORD
                    or not isinstance(source.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                    or not isinstance(source.get("json_pointer"), str) or not re.fullmatch(r"/symbols/(0|[1-9]\d*)", source["json_pointer"])):
                raise Refused("The retained received library source is damaged.")
            path = store.folder / "received" / (source["sha256"] + ".record")
            if path.parent.is_symlink() or path.is_symlink():
                raise Refused("A retained received library source cannot use a link.")
            try:
                raw = read_plain(path, MAX_RECORD)
                entry = json.loads(raw)["symbols"][int(source["json_pointer"].rsplit("/", 1)[1])]
                notation, actual = knowledge._proposal_notation(data, entry["notation"])
                valid &= _digest(raw) == source["sha256"] and _digest(_json(entry)) == source["entry_sha256"] and actual == canonical
            except (OSError, ValueError, TypeError, KeyError, IndexError):
                valid = False
        if valid:
            result.append({"notation": demand["notation"], "canonical": canonical, "definition": "", "sources": copy.deepcopy(sources)})
    return result


def _retain_library(knowledge, data, progress, entry):
    if not entry["sources"] or any(source.get("kind") != "received_library" for source in entry["sources"]):
        return
    old = progress["library_demands"].get(entry["canonical"])
    if old and old.get("sources") == entry["sources"] and verify_receipt(old, registry.project_key(knowledge.project.root)):
        return
    store, observed = _observation(knowledge, data)
    if observed["raw"] is None or any(source["sha256"] != observed["sha256"] for source in entry["sources"]):
        raise Conflict("The received library entry changed before its question was retained.")
    store._archive(observed["raw"])
    progress["library_demands"][entry["canonical"]] = sign_receipt({
        "kind": "received_library_reference", "id": secrets.token_hex(16),
        "project_key": registry.project_key(knowledge.project.root), "root": str(knowledge.project.root.resolve()),
        "notation": entry["notation"], "canonical": entry["canonical"], "sources": copy.deepcopy(entry["sources"])})


def _current(knowledge, *, include_rejected=False):
    data = knowledge._load()
    if not data or not data.get("initialized"):
        return data, []
    knowledge._refresh_data(data)
    from research import _notation_definitions
    cached, entries = {}, []
    known = knowledge._known(data) - knowledge._blocked_abbreviations(data)
    commands = knowledge._commands(data)
    rejected = knowledge._rejections(data)
    for item in data.get("unverified_symbols", []):
        canonical = item["canonical"]
        if canonical in known or not include_rejected and canonical in rejected:
            continue
        sources, meanings = [], set()
        for source in item.get("sources", []):
            path = source.get("path")
            if not isinstance(path, str):
                continue
            if path not in cached:
                try:
                    raw = _read_plain(knowledge.project.root, path, MAX_TEXT)
                    text = raw.decode("utf-8")
                    cached[path] = (raw, math_notations(text, known_commands=commands), _notation_definitions(text))
                except (OSError, ValueError):
                    cached[path] = None
            actual = cached[path]
            if actual is None:
                continue
            raw, parsed, definitions = actual
            for occurrence in parsed["symbols"]:
                if occurrence["canonical"] != canonical:
                    continue
                reference = {"path": path, "line": occurrence["line"], "sha256": _digest(raw), "evidence": "unverified"}
                if reference not in sources:
                    sources.append(reference)
            meanings.update(definition["meaning"] for definition in definitions if definition["canonical"] == canonical)
        if not sources:
            continue
        entry = {"notation": item["notation"], "canonical": canonical,
                 "definition": next(iter(meanings)) if len(meanings) == 1 else "",
                 "sources": sorted(sources, key=lambda row: (row["path"], row["line"], row["sha256"]))}
        entries.append(entry)
    manuscript_names = {entry["canonical"] for entry in entries}
    entries.extend(entry for entry in _library_entries(knowledge, data, known, rejected, include_rejected)
                   if entry["canonical"] not in manuscript_names)
    for entry in entries:
        reopening = next((receipt["id"] for receipt in reversed(knowledge._instructions(data))
            if receipt["kind"] == "reopen" and receipt.get("target") == entry["canonical"]
            and _received_instruction(receipt, knowledge, entry["canonical"])), None)
        if reopening:
            entry["reopened_by"] = reopening
        entry["binding_hash"] = _digest(_json(entry))
    return data, sorted(entries, key=lambda row: row["canonical"])


def received_names(knowledge, *, candidates=None):
    """Names already present in actual unverified inputs, including refused names."""
    if not knowledge.project.files_lock.acquire(timeout=SAVE_WAIT):
        raise Conflict("The project files are busy; received notation could not be checked.")
    try:
        if candidates:
            data, entries = _current(knowledge, include_rejected=True)
            provenance = (data or {}).get("provenance", {})
            if data and data.get("initialized") and not provenance.get("write_blocked") and provenance.get("record_state") != "local":
                requested = {name for name in candidates if isinstance(name, str)} - knowledge._known(data)
                requested -= {entry["canonical"] for entry in entries if any(source.get("kind") != "received_library" for source in entry["sources"])}
                store, observed = _observation(knowledge, data)
                rows = _library_rows(knowledge, data, observed["raw"], observed["sha256"], requested) if observed["raw"] is not None and not observed["error"] else {}
                if rows:
                    with registry.FileLock(_path(knowledge).with_suffix(".lock"), timeout=3):
                        progress = _read(knowledge)
                        for entry in rows.values():
                            _retain_library(knowledge, data, progress, entry)
                        _write(knowledge, progress)
        return {entry["canonical"] for entry in _current(knowledge, include_rejected=True)[1]}
    finally:
        knowledge.project.files_lock.release()


def _valid(question, knowledge):
    return bool(isinstance(question, dict) and question.get("kind") == "received_notation_question"
                and question.get("root") == str(knowledge.project.root.resolve())
                and isinstance(question.get("id"), str) and re.fullmatch(r"[0-9a-f]{32}", question["id"])
                and verify_receipt(question, registry.project_key(knowledge.project.root)))


def _answers(data, knowledge, identity):
    key = registry.project_key(knowledge.project.root)
    return [receipt for receipt in (data or {}).get("decisions", [])
            if isinstance(receipt, dict) and verify_instruction(receipt, key)
            and isinstance(receipt.get("received_confirmation"), dict)
            and receipt["received_confirmation"].get("question_id") == identity
            and _valid(receipt["received_confirmation"].get("question"), knowledge)]


def _received_instruction(receipt, knowledge, canonical):
    metadata = receipt.get("received_confirmation", {})
    question = metadata.get("question") if isinstance(metadata, dict) else None
    return bool(_valid(question, knowledge) and question.get("canonical") == canonical
                and metadata.get("question_id") == question["id"])


def _question(knowledge, progress, entry):
    question = next((row for row in progress["questions"].values()
        if _valid(row, knowledge) and row.get("binding_hash") == entry["binding_hash"]), None)
    if question is None:
        if len(progress["questions"]) >= MAX_QUESTIONS:
            raise Refused("The received notation question limit was reached.")
        question = sign_receipt({"kind": "received_notation_question", "id": secrets.token_hex(16),
            "project_key": registry.project_key(knowledge.project.root),
            "root": str(knowledge.project.root.resolve()), **entry})
        progress["questions"][question["id"]] = question
    return question


def _check_questions(knowledge, questions, *, include_rejected=False):
    latest = {row["canonical"]: row for row in _current(knowledge, include_rejected=include_rejected)[1]}
    for question in questions:
        if not _valid(question, knowledge) or latest.get(question["canonical"], {}).get("binding_hash") != question["binding_hash"]:
            raise Conflict("The received source changed before its decision was saved. Review it again.")


def _reopen_reply(knowledge, human_text, conversation, received_subjects, *, context_ambiguous=False):
    """An explicit named reopening removes a refusal, without adopting the name."""
    if ORDINAL.search(human_text) and not received_subjects and not context_ambiguous:
        return None  # An existing Agent batch may own this ordinal context.
    project = knowledge.project
    if not project.files_lock.acquire(timeout=SAVE_WAIT):
        raise Conflict("The project files are busy; the received refusal was not reopened.")
    receipts = []
    try:
        with registry.FileLock(_path(knowledge).with_suffix(".lock"), timeout=3):
            progress = _read(knowledge)
            if any(not _valid(progress["questions"].get(identity), knowledge) for identity in received_subjects):
                raise Conflict("The received question context changed. Refresh it before reopening the notation.")
            data, entries = _current(knowledge, include_rejected=True)
            if not data or not data.get("initialized"):
                return None
            subjects = [{**entry, "status": "rejected", "needs_details": False} for entry in entries]
            parsed = parse_actions(human_text, subjects)
            if parsed and ORDINAL.search(human_text):
                return {"decisions": [], "clarifications": ["Name the actual received notation to reopen; these source questions have no item numbers."]}
            actions = (parsed or {}).get("actions", [])
            if not actions or any(action["kind"] != "reopen" for action in actions):
                return None
            targets = [action["target"] for action in actions]
            if len(set(targets)) != len(targets):
                return {"decisions": [], "clarifications": ["Name each received notation to reopen once."]}
            instructions = knowledge._instructions(data)
            rejected = knowledge._rejections(data)
            selected = []
            for action in actions:
                canonical = action["target"]
                if not any(_received_instruction(receipt, knowledge, canonical) for receipt in instructions):
                    return None  # Keep the existing Agent-proposal reopening path.
                refusal = rejected.get(canonical)
                if refusal is None:
                    continue
                entry = next(entry for entry in entries if entry["canonical"] == canonical)
                question = _question(knowledge, progress, entry)
                selected.append(question)
                metadata = {"question_id": question["id"], "question": copy.deepcopy(question),
                            "sources": copy.deepcopy(question["sources"]), "reopens_decision_id": refusal["id"]}
                receipts.append(make_instruction(registry.project_key(project.root), {
                    "kind": "reopen", "target": canonical, "canonical": canonical,
                    "notation": question["notation"], "received_confirmation": metadata}, human_text, conversation))
            if not receipts:
                return {"decisions": [], "clarifications": ["This received notation is already pending a fresh confirmation. Reopening alone grants no adoption."]}
            _write(knowledge, progress)
            _check_questions(knowledge, selected, include_rejected=True)
            data.setdefault("decisions", []).extend(receipts)
            knowledge._refresh_data(data)
            _check_questions(knowledge, selected, include_rejected=True)
            knowledge._write(data)
    finally:
        project.files_lock.release()
    project.on_change()
    return {"decisions": receipts, "clarifications": []}


def questions(knowledge, state=None):
    """Return stable questions derived from actual current bytes, never mirror claims."""
    project = knowledge.project
    if not project.files_lock.acquire(timeout=SAVE_WAIT):
        raise Conflict("The project files are busy; received notation questions will refresh later.")
    try:
        data, entries = _current(knowledge)
        if not data or not data.get("initialized"):
            return []
        path = _path(knowledge)
        with registry.FileLock(path.with_suffix(".lock"), timeout=3):
            progress = _read(knowledge)
            result = []
            for entry in entries:
                _retain_library(knowledge, data, progress, entry)
                question = _question(knowledge, progress, entry)
                if _answers(data, knowledge, question["id"]):
                    continue
                result.append({key: copy.deepcopy(question[key]) for key in
                    ("id", "project_key", "notation", "canonical", "sources", "definition")})
                result[-1]["question"] = ("May I use received " + ("library notation " if any(source.get("kind") == "received_library" for source in question["sources"]) else "notation ")
                    + question["notation"] + "? Its original source is preserved. "
                    + ("Its meaning remains unresolved." if not question["definition"] else "Its documented meaning is retained."))
            _write(knowledge, progress)
            return result
    finally:
        project.files_lock.release()


def decide_reply(knowledge, human_text, conversation, received_subjects, *, context_ambiguous=False):
    """Consume only a real editor reply to privately issued, exact source questions."""
    if received_subjects is None:
        received_subjects = []
    if not isinstance(received_subjects, list):
        raise Refused("The received notation question context must be a list of issued IDs.")
    reopening = isinstance(human_text, str) and REOPEN.search(human_text)
    if not received_subjects and not context_ambiguous and not reopening:
        return None
    if (not isinstance(received_subjects, list) or len(received_subjects) > MAX_QUESTIONS
            or any(not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity) for identity in received_subjects)
            or len(set(received_subjects)) != len(received_subjects)
            or not isinstance(human_text, str) or len(human_text) > 40000
            or not isinstance(conversation, str) or not conversation):
        raise Refused("The received notation reply needs its displayed question IDs and original human message.")
    if reopening:
        return _reopen_reply(knowledge, human_text, conversation, received_subjects, context_ambiguous=context_ambiguous)
    project = knowledge.project
    if not project.files_lock.acquire(timeout=SAVE_WAIT):
        raise Conflict("The project files are busy; the author confirmation was not saved.")
    receipts, clarifications = [], []
    try:
        with registry.FileLock(_path(knowledge).with_suffix(".lock"), timeout=3):
            progress = _read(knowledge)
            displayed = [progress["questions"].get(identity) for identity in received_subjects]
            if any(not _valid(question, knowledge) for question in displayed):
                raise Conflict("The received notation question no longer belongs to this project. Refresh and review it before replying.")
            subjects = [{**question, "status": "pending", "needs_details": False} for question in displayed]
            parsed = parse_actions(human_text, subjects)
            plain = parse_reply(human_text)
            actions = (parsed or {}).get("actions", [])
            if parsed and ORDINAL.search(human_text) and all(action["kind"] in ("approve", "reject", "refuse_name") for action in actions):
                return {"decisions": [], "clarifications": ["Name the actual received notation you approve or reject; these source questions have no item numbers."]}
            if not plain and (not actions or any(action["kind"] not in ("approve", "reject", "refuse_name") for action in actions)):
                if received_subjects:
                    progress["contexts"][_digest(conversation.encode())] = {"blocked": received_subjects}
                    _write(knowledge, progress)
                return None
            data, current = _current(knowledge)
            if not data or not data.get("initialized"):
                raise Conflict("Initialize this project's baseline before confirming received notation.")
            if displayed and all(_answers(data, knowledge, question["id"]) for question in displayed):
                return {"decisions": [], "clarifications": []}
            context = progress["contexts"].get(_digest(conversation.encode()), {})
            ai_subjects = get_subjects(registry.project_key(project.root), conversation)
            active_ai = any(proposal.get("status") == "pending" and any(
                all(proposal.get(key) == subject.get(key) for key in ("id", "revision", "content_hash"))
                for subject in ai_subjects) for proposal in data.get("proposals", []))
            if plain and (context_ambiguous or active_ai or set(received_subjects) & set(context.get("blocked", []))):
                progress["contexts"][_digest(conversation.encode())] = {"blocked": received_subjects}
                _write(knowledge, progress)
                return {"decisions": [], "clarifications": ["Name the received notation or the Agent proposal you are deciding; this reply grants neither permission."]}
            if plain:
                if len(displayed) != 1 or len(current) > 1:
                    return {"decisions": [], "clarifications": ["Name the received notation you approve or reject; several questions are open."]}
                actions = [{"kind": "approve" if plain == "approved" else "reject", "target": displayed[0]["canonical"]}]
            targets = [action.get("target") for action in actions]
            if any(targets.count(target) > 1 for target in targets):
                return {"decisions": [], "clarifications": ["Clarify the conflicting received notation decisions before adoption."]}
            for action in actions:
                question = next((q for q in displayed if q["canonical"] == action.get("target")), None)
                if question is None:
                    continue
                if _answers(data, knowledge, question["id"]):
                    continue
                actual = next((row for row in current if row["canonical"] == question["canonical"]), None)
                if actual is None or actual["binding_hash"] != question["binding_hash"]:
                    raise Conflict("The received source or its notation changed. Refresh and review the current question before replying.")
                if action["kind"] == "approve" and (question["canonical"] in knowledge._rejections(data)
                        or question["canonical"] in knowledge._blocked_abbreviations(data)
                        or knowledge._expression_rejected(data, question["definition"])):
                    clarifications.append("This notation was rejected. Explicitly reopen or replace it before adopting it.")
                    continue
                metadata = {"question_id": question["id"], "question": copy.deepcopy(question),
                            "sources": copy.deepcopy(question["sources"])}
                receipt = make_instruction(registry.project_key(project.root), {
                    "kind": "adopt" if action["kind"] == "approve" else "refuse_name",
                    "notation": question["notation"], "canonical": question["canonical"],
                    "definition": question["definition"], "scope": "project", "source_expression": question["definition"],
                    "received_confirmation": metadata}, human_text, conversation)
                receipts.append(receipt)
                data.setdefault("decisions", []).append(receipt)
            if receipts:
                knowledge._refresh_data(data)
                _check_questions(knowledge, [receipt["received_confirmation"]["question"] for receipt in receipts])
                knowledge._write(data)
    finally:
        project.files_lock.release()
    if receipts:
        project.on_change()
    return {"decisions": receipts, "clarifications": clarifications}
