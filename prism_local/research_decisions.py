"""Editor decision evidence, separate from model prompts and project mirrors.

The project record owns decisions. Signatures and the local editor witness prove
their origin; the private subject record only identifies what a plain reply is
answering. These helpers do not grant permission from project status fields.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import time
from datetime import datetime

from errors import Refused
from research_edits import sign_receipt, verify_receipt
from research_language import quoted_message
import registry

SUBJECT_FILE = "research-decision-subjects.json"

APPROVAL = re.compile(
    r"(?:yes(?:[, ]+(?:please|i approve(?: (?:it|this proposal|that notation))?|that's fine))?"
    r"|(?:i )?approve(?: (?:it|this proposal|that notation))?|approved|sure|ok(?:ay)?|go ahead"
    r"|(?:我)?同意(?:这个提案|采用这个记号)?|可以(?:[,，]按这个来)?|好的?|批准(?:这个提案)?)")
REJECTION = re.compile(
    r"(?:no(?:[, ]+(?:thanks|i reject(?: (?:it|this proposal|that notation))?))?"
    r"|(?:i )?reject(?: (?:it|this proposal|that notation))?|rejected"
    r"|(?:do not|don't) (?:adopt|use) (?:it|this notation|that notation)"
    r"|(?:我)?不同意(?:这个提案)?|(?:我)?拒绝(?:这个提案)?|不要采用|不采用(?:这个记号)?)")


def parse_reply(text):
    """Recognize a complete ordinary answer, never a quotation or a substring."""
    if not isinstance(text, str) or len(text) > 40000 or quoted_message(text):
        return None
    value = re.sub(r"\s+", " ", text.strip()).casefold().rstrip(" .!。！")
    if APPROVAL.fullmatch(value):
        return "approved"
    if REJECTION.fullmatch(value):
        return "rejected"
    return None


def _binding(proposal, project_key):
    if (not isinstance(proposal, dict) or proposal.get("project_key") != project_key
            or not isinstance(proposal.get("id"), str) or not proposal["id"]
            or type(proposal.get("revision")) is not int or proposal["revision"] < 1
            or not isinstance(proposal.get("content_hash"), str)
            or re.fullmatch(r"[0-9a-f]{64}", proposal["content_hash"]) is None
            or proposal.get("candidate_kind") != "identifier" or proposal.get("needs_details") is not False
            or not all(isinstance(proposal.get(key), str) and proposal[key].strip()
                       for key in ("notation", "canonical", "definition", "scope", "reason"))):
        return None
    return {"proposal_id": proposal["id"], "revision": proposal["revision"],
            "content_hash": proposal["content_hash"], "canonical": proposal["canonical"]}


def _decision_text(text, decision, binding, action=None):
    if action is None:
        return parse_reply(text) == decision
    return bool(isinstance(text, str) and text.strip() and len(text) <= 40000
        and isinstance(action, dict) and action.get("target") == binding["canonical"]
        and action.get("kind") == {"approved": "approve", "rejected": "reject"}.get(decision))


def _github_time(value):
    if not isinstance(value, str) or len(value) > 50:
        raise ValueError("Invalid GitHub time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("GitHub time needs its time zone")
    return parsed.timestamp()


def _github_source_data(source, project_key):
    try:
        repository, issue = source["repository"], source["issue"]
        publication, comment = source["publication"], source["comment"]
        user, subjects = comment["user"], publication["subjects"]
        roster = source["approver_roster"]
        accounts = roster["accounts"]
        full = repository["full_name"]
        identifiers = (repository["id"], issue["id"], issue["number"], publication["comment_id"], comment["id"], user["id"])
        if (not isinstance(project_key, str) or not project_key or source.get("project_key", project_key) != project_key
                or not all(type(value) is int and value > 0 for value in identifiers)
                or not isinstance(full, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", full) is None
                or not isinstance(source["topic_id"], str) or re.fullmatch(r"[0-9a-f]{24}", source["topic_id"]) is None
                or not isinstance(source["batch_id"], str) or re.fullmatch(r"[0-9a-f]{32}", source["batch_id"]) is None
                or not isinstance(source["configuration_id"], str) or not source["configuration_id"]
                or not isinstance(comment["body"], str) or not comment["body"].strip() or len(comment["body"]) > 40000
                or comment.get("body_sha256") != hashlib.sha256(comment["body"].encode()).hexdigest()
                or not isinstance(publication["body_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", publication["body_sha256"]) is None
                or user["type"] != "User" or not isinstance(user["login"], str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", user["login"]) is None
                or not isinstance(source["approvers"], list) or not 1 <= len(source["approvers"]) <= 100
                or not all(isinstance(login, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login) for login in source["approvers"])
                or user["login"].casefold() not in {login.casefold() for login in source["approvers"]}
                or not isinstance(accounts, list) or not 1 <= len(accounts) <= 100
                or not all(isinstance(account, dict) and type(account.get("id")) is int and account["id"] > 0
                    and isinstance(account.get("login"), str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", account["login"])
                    for account in accounts)
                or len({account["id"] for account in accounts}) != len(accounts)
                or len({account["login"].casefold() for account in accounts}) != len(accounts)
                or {account["login"].casefold() for account in accounts} != {login.casefold() for login in source["approvers"]}
                or roster.get("kind") != "github_approver_roster" or roster.get("configuration_id") != source["configuration_id"]
                or roster.get("repository_id") != repository["id"] or not verify_receipt(roster, project_key)
                or not any(account["id"] == user["id"] and account["login"].casefold() == user["login"].casefold()
                           for account in accounts)
                or comment["html_url"] != f"https://github.com/{full}/issues/{issue['number']}#issuecomment-{comment['id']}"
                or not isinstance(subjects, list) or not 1 <= len(subjects) <= 100
                or len({subject["id"] for subject in subjects}) != len(subjects)):
            return False
        for subject in subjects:
            if (not isinstance(subject, dict) or not isinstance(subject.get("id"), str) or not subject["id"]
                    or len(subject["id"]) > 128 or type(subject.get("revision")) is not int or subject["revision"] < 1
                    or not isinstance(subject.get("content_hash"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", subject["content_hash"]) is None):
                return False
        created, published, updated = (_github_time(value) for value in
            (comment["created_at"], publication["created_at"], comment["updated_at"]))
        return (created, comment["id"]) > (published, publication["comment_id"]) and updated >= created
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def make_github_source(project_key, source_data):
    """Witness API evidence after the issue synchronizer validates its private binding."""
    source = copy.deepcopy(source_data)
    if not isinstance(source, dict):
        raise Refused("A GitHub decision needs its actual comment and publication evidence.")
    allowed = ("repository", "issue", "topic_id", "batch_id", "publication", "comment", "approvers", "approver_roster", "configuration_id")
    if set(source) != set(allowed) or not isinstance(source.get("comment"), dict):
        raise Refused("A GitHub decision needs its actual comment and publication evidence.")
    comment = source["comment"]
    if not isinstance(comment.get("body"), str):
        raise Refused("A GitHub decision needs its original comment text.")
    digest = hashlib.sha256(comment["body"].encode()).hexdigest()
    if comment.get("body_sha256", digest) != digest:
        raise Refused("The GitHub comment version changed.")
    comment["body_sha256"] = digest
    if not _github_source_data(source, project_key):
        raise Refused("The GitHub reply does not identify a designated human and a published proposal.")
    return sign_receipt({"purpose": "github_notation_source", "id": secrets.token_hex(16),
                         "project_key": project_key, **source})


def verify_github_source(source, project_key):
    return bool(isinstance(source, dict) and source.get("purpose") == "github_notation_source"
                and _github_source_data(source, project_key) and verify_receipt(source, project_key))


def github_source_key(source):
    comment = source["comment"]
    value = [source["repository"]["id"], source["issue"]["id"], source["batch_id"],
             comment["id"], comment["updated_at"], comment["body_sha256"], source["publication"]["subjects"]]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _human_origin(project_key, human_text, conversation, source):
    if source is None:
        return {"channel": "editor_chat"}
    if not verify_github_source(source, project_key) or human_text != source["comment"]["body"]:
        raise Refused("The GitHub decision has no verified original comment evidence.")
    return {"channel": "github_comment", "source": copy.deepcopy(source)}


def _verified_human_origin(receipt, project_key):
    if receipt.get("channel") == "editor_chat":
        return True
    source = receipt.get("source")
    return bool(receipt.get("channel") == "github_comment" and verify_github_source(source, project_key)
                and receipt.get("human_text") == source["comment"]["body"])


def _github_subject(source, binding):
    return bool(isinstance(source, dict) and any(
        subject.get("id") == binding.get("proposal_id", binding.get("id"))
        and all(subject.get(key) == binding.get(key) for key in ("revision", "content_hash"))
        for subject in source.get("publication", {}).get("subjects", [])))


def make_instruction(project_key, action, human_text, conversation, *, source=None):
    """Sign an editor-parsed instruction, independently of Agent proposals."""
    if (not isinstance(action, dict) or action.get("kind") not in ("adopt", "family", "reopen", "refuse_expression", "refuse_name")
            or not isinstance(human_text, str) or not human_text.strip() or len(human_text) > 40000
            or not isinstance(project_key, str) or not project_key
            or not isinstance(conversation, str) or not conversation
            or source is not None and not _github_subject(source, action.get("github_subject", {}))):
        raise Refused("A human notation instruction needs its original editor message.")
    return sign_receipt({"purpose": "notation_instruction", "id": secrets.token_hex(16),
        "project_key": project_key, "actor": "human", **_human_origin(project_key, human_text, conversation, source),
        "human_text": human_text, "conversation": conversation, "at": time.time(),
        "decision": ("pending" if action["kind"] == "reopen" else
                     "rejected" if action["kind"] in ("refuse_name", "refuse_expression") else "approved"), **action})


def verify_instruction(receipt, project_key):
    return bool(isinstance(receipt, dict) and receipt.get("purpose") == "notation_instruction"
        and receipt.get("kind") in ("adopt", "family", "reopen", "refuse_expression", "refuse_name")
        and receipt.get("actor") == "human" and _verified_human_origin(receipt, project_key)
        and isinstance(receipt.get("human_text"), str) and receipt["human_text"].strip()
        and isinstance(receipt.get("conversation"), str) and receipt["conversation"]
        and (receipt.get("channel") != "github_comment" or _github_subject(receipt.get("source"), receipt.get("github_subject", {})))
        and verify_receipt(receipt, project_key))


def make_decision(project_key, proposal, decision, human_text, conversation, action=None, *, source=None):
    """Issue one decision after the editor has checked its conversational subject."""
    binding = _binding(proposal, project_key)
    if (not binding or (proposal.get("status") != "pending"
                       and not (proposal.get("status") == "approved" and decision == "rejected"))
            or decision not in ("approved", "rejected")
            or not _decision_text(human_text, decision, binding, action)
            or not isinstance(project_key, str) or not project_key
            or source is not None and not _github_subject(source, binding or {})
            or not isinstance(conversation, str) or not conversation or len(conversation) > 512):
        raise Refused("The human decision must answer one complete current notation proposal.")
    return sign_receipt({"purpose": "notation_decision", "id": secrets.token_hex(16),
        "project_key": project_key, **binding, "decision": decision,
        "actor": "human", **_human_origin(project_key, human_text, conversation, source), "human_text": human_text,
        "conversation": conversation, "at": time.time(), **({"action": action} if action else {})})


def verify_decision(receipt, project_key, proposal):
    binding = _binding(proposal, project_key)
    return bool(binding and isinstance(receipt, dict)
        and receipt.get("purpose") == "notation_decision"
        and receipt.get("actor") == "human" and _verified_human_origin(receipt, project_key)
        and receipt.get("decision") in ("approved", "rejected")
        and proposal.get("status") in ("pending", receipt["decision"])
        and all(receipt.get(key) == value for key, value in binding.items())
        and (receipt.get("channel") != "github_comment" or _github_subject(receipt.get("source"), binding))
        and _decision_text(receipt.get("human_text"), receipt["decision"], binding, receipt.get("action"))
        and isinstance(receipt.get("conversation"), str) and bool(receipt["conversation"])
        and type(receipt.get("at")) in (int, float) and receipt["at"] > 0
        and verify_receipt(receipt, project_key))


def _subject_key(project_key, conversation):
    if (not isinstance(project_key, str) or not project_key or len(project_key) > 512
            or not isinstance(conversation, str) or not conversation or len(conversation) > 512):
        raise Refused("A notation reply must identify its project and Conversation.")
    return hashlib.sha256(json.dumps([project_key, conversation], ensure_ascii=False).encode()).hexdigest()


def _subjects(path):
    data = registry.read_state(path, tries=1)
    if not data and not path.exists():
        return {"version": 1, "subjects": {}}
    if data.get("version") != 1 or not isinstance(data.get("subjects"), dict):
        raise Refused("The notation reply contexts are damaged; review the proposal before replying.")
    return data


def get_subjects(project_key, conversation):
    key = _subject_key(project_key, conversation)
    path = registry.state_dir() / SUBJECT_FILE
    if path.is_symlink():
        raise Refused("The notation reply contexts must be a regular local state file.")
    if not path.exists():
        return []
    with registry.FileLock(path.with_suffix(".lock"), timeout=3):
        entry = _subjects(path)["subjects"].get(key)
    if entry is None:
        return []
    if (not isinstance(entry, dict) or entry.get("project_key") != project_key
            or entry.get("conversation") != conversation):
        raise Refused("The notation reply context is damaged; review the proposals before replying.")
    items = entry.get("items", [entry])   # earlier single-proposal context
    if not isinstance(items, list) or len(items) > 100:
        raise Refused("The notation reply context is damaged; review the proposals before replying.")
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                or type(item.get("revision")) is not int or item["revision"] < 1
                or not isinstance(item.get("content_hash"), str)
                or re.fullmatch(r"[0-9a-f]{64}", item["content_hash"]) is None):
            raise Refused("The notation reply context is damaged; review the proposals before replying.")
    return items


def get_subject(project_key, conversation):
    items = get_subjects(project_key, conversation)
    return ({name: items[0][name] for name in ("id", "revision", "content_hash")}
            if len(items) == 1 and items[0].get("complete", True) else None)


def set_subject(project_key, conversation, proposals):
    """Remember displayed item identities; context itself grants no permission."""
    key = _subject_key(project_key, conversation)
    if not isinstance(proposals, list) or len(proposals) > 100:
        raise Refused("The editor must provide the proposals displayed in this Turn.")
    items = [{**{name: p[name] for name in ("id", "revision", "content_hash")},
              "complete": bool(_binding(p, project_key))}
             for p in proposals if isinstance(p, dict) and p.get("status") == "pending"]
    subject = {"project_key": project_key, "conversation": conversation, "items": items} if items else None
    path = registry.state_dir() / SUBJECT_FILE
    if path.is_symlink():
        raise Refused("The notation reply contexts must be a regular local state file.")
    with registry.FileLock(path.with_suffix(".lock"), timeout=3):
        data = _subjects(path)
        if data["subjects"].get(key) == subject:
            return
        if subject is None:
            data["subjects"].pop(key, None)
        else:
            if key not in data["subjects"] and len(data["subjects"]) >= 10000:
                raise Refused("The local notation reply context limit was reached.")
            data["subjects"][key] = subject
        registry.write_state(path, data)
