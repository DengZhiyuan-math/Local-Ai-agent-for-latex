"""Explicit proposal batches on an existing GitHub research topic.

The private topic journal owns publication and processing progress. Proposal
fields are literal candidate data; ordinary topic Markdown keeps its own guard.
"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import re
import uuid

from errors import Conflict, Refused
from project import SAVE_WAIT
import registry
from research_edits import sign_receipt, verify_receipt
from research_decisions import make_github_source, verify_decision
from research_access import ResearchAccess
from research_language import parse_actions


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def current(knowledge, data, subjects, *, pending=False):
    if (not isinstance(subjects, list) or not 1 <= len(subjects) <= 100
            or any(not isinstance(row, dict) or set(row) != {"id", "revision", "content_hash"} for row in subjects)
            or any(not isinstance(row["id"], str) or not row["id"] or len(row["id"]) > 128
                or type(row["revision"]) is not int or row["revision"] < 1
                or not isinstance(row["content_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["content_hash"])
                for row in subjects)
            or len({str(row["id"]) for row in subjects}) != len(subjects)):
        raise Refused("Select between one and 100 distinct current notation proposals.")
    proposals = knowledge._proposal_records(data)
    result = []
    for subject in subjects:
        proposal = next((p for p in proposals if p["id"] == subject["id"]), None)
        if (not proposal or any(proposal.get(key) != subject[key] for key in subject)
                or proposal["candidate_kind"] != "identifier" or proposal["needs_details"]
                or pending and proposal["status"] != "pending"):
            raise Refused("Review the complete current pending proposal before sharing or deciding it.")
        result.append(copy.deepcopy(proposal))
    return result


def body_for(config, topic_id, batch_id, proposals):
    fields = [{key: proposal[key] for key in ("id", "revision", "content_hash", "notation", "definition",
                                              "scope", "reason", "source_expression")} for proposal in proposals]
    literal = json.dumps(fields, ensure_ascii=False, indent=2)
    fence = "`" * max(3, 1 + max((len(x) for x in re.findall(r"`+", literal)), default=0))
    body = (f"<!-- prism-proposals:{config['project_id']}:{topic_id}:{batch_id} -->\n"
            "# Pending notation proposals — not approved\n\n"
            "These candidates are not adopted notation. Each item needs an explicit human decision.\n"
            f"Batch: `{batch_id}`\n\n"
            "Reply in this topic issue. If several batches are open, start your reply with:\n"
            f"`prism-batch:{batch_id}`\n\n"
            "Name the items you approve, reject or replace. An unrelated reply grants no permission.\n\n"
            f"{fence}json\n{literal}\n{fence}\n")
    if len(body.encode("utf-8")) > 1024 * 1024:
        raise Refused("This proposal batch is too large to publish safely.")
    return body


def batches(worker, config, topic_id, record):
    rows = record.get("proposal_batches", {})
    if not isinstance(rows, dict) or len(rows) > 1000:
        raise Refused("The GitHub proposal publication records are damaged.")
    for identity, batch in rows.items():
        if (not isinstance(batch, dict) or not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity)
                or not isinstance(batch.get("body"), str) or not isinstance(batch.get("request"), dict)):
            raise Refused("The GitHub proposal publication records are damaged.")
        request = batch.get("request", {})
        expected = {"kind": "github_proposal_binding", "id": identity, "project_key": worker._key,
                    "repository_id": config["repository"]["id"], "project_id": config["project_id"],
                    "topic_id": topic_id, "subjects": request.get("subjects"),
                    "proposal_snapshots_sha256": digest(json.dumps(batch.get("proposals"), sort_keys=True)),
                    "body_sha256": digest(batch.get("body", ""))}
        if (not verify_receipt(request, worker._key)
                or any(request.get(key) != value for key, value in expected.items())
                or not isinstance(request.get("configuration_id"), str) or not isinstance(request.get("roster_id"), str)
                or not isinstance(request.get("subjects"), list)
                or not isinstance(batch.get("proposals"), list)
                or not all(isinstance(proposal, dict) for proposal in batch["proposals"])
                or [{key: proposal.get(key) for key in ("id", "revision", "content_hash")}
                    for proposal in batch["proposals"]] != request["subjects"]
                or "intent" in batch and (not isinstance(batch["intent"], dict)
                    or type(batch["intent"].get("uncertain")) is not bool)):
            raise Refused("The GitHub proposal binding has no verified local author request.")
        publication = batch.get("publication")
        if publication is not None and (not isinstance(publication, dict)
                or type(publication.get("comment_id")) is not int or publication["comment_id"] < 1
                or publication.get("body_sha256") != request["body_sha256"]
                or not isinstance(publication.get("created_at"), str)
                or not isinstance(publication.get("html_url"), str)):
            raise Refused("The GitHub proposal publication identity is damaged.")
    return rows


def roster(worker, snapshot, state, repository):
    config = snapshot["config"]
    epoch = config["receipt"]["id"]
    rows = repository.setdefault("approver_rosters", {})
    if not isinstance(rows, dict):
        raise Refused("The GitHub approver identity records are damaged.")
    found = rows.get(epoch)
    if found is not None:
        if (not verify_receipt(found, worker._key) or found.get("kind") != "github_approver_roster"
                or found.get("configuration_id") != epoch or found.get("repository_id") != config["repository"]["id"]
                or not isinstance(found.get("accounts"), list)
                or sorted(row.get("login", "").casefold() for row in found["accounts"]) != config["approvers"]):
            raise Refused("Confirm the GitHub approver identities again before sharing proposals.")
        return found
    if not config["approvers"]:
        raise Refused("Designate at least one human GitHub account before sharing proposals.")
    accounts = []
    for login in config["approvers"]:
        user = worker._call(snapshot, "user", login)
        if (not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] < 1
                or user.get("type") != "User" or not isinstance(user.get("login"), str)
                or user["login"].casefold() != login):
            raise Refused("The designated GitHub account is not a verified human account identity.")
        accounts.append({"id": user["id"], "login": user["login"].casefold()})
    found = sign_receipt({"kind": "github_approver_roster", "id": uuid.uuid4().hex,
        "project_key": worker._key, "configuration_id": epoch,
        "repository_id": config["repository"]["id"], "accounts": accounts})
    worker._current(snapshot)
    rows[epoch] = found
    worker._write(state)
    return found


def share(worker, topic_id, subjects):
    if worker._closed or worker.project.root != worker._root or worker.project.turn.busy() or not worker.is_open():
        raise Conflict("Keep this project's page open and finish its current Turn before sharing proposals.")
    if not worker._lock.acquire(timeout=SAVE_WAIT):
        raise Conflict("The GitHub check is busy; retry sharing the proposal batch.")
    try:
        with registry.FileLock(worker._path().with_suffix(".lock"), timeout=1):
            snapshot = worker._snapshot()
            data = worker.knowledge._load()
            config = snapshot["config"]
            import research_topics
            topic = next((t for t in snapshot["topics"] if t["id"] == topic_id), None)
            if not topic:
                raise Refused("Choose an existing research topic from this project.")
            proposals = current(worker.knowledge, data, subjects, pending=True)
            state = worker._read()
            repository = state["repositories"].setdefault(str(config["repository"]["id"]), {"topics": {}})
            identities = roster(worker, snapshot, state, repository)
            record = repository["topics"].setdefault(topic_id, {})
            rows = batches(worker, config, topic_id, record)
            if not any(row["request"]["subjects"] == subjects and row["request"].get("configuration_id") == config["receipt"]["id"] for row in rows.values()):
                if not worker.project.files_lock.acquire(timeout=SAVE_WAIT):
                    raise Conflict("The project files are busy; the proposal batch was not saved.")
                try:
                    worker._current(snapshot)
                    current(worker.knowledge, worker.knowledge._load(), subjects, pending=True)
                    identity = uuid.uuid4().hex
                    body = body_for(config, topic_id, identity, proposals)
                    snapshots = [{key: copy.deepcopy(value) for key, value in proposal.items() if key != "history"}
                                 for proposal in proposals]
                    request = sign_receipt({"kind": "github_proposal_binding", "id": identity,
                        "project_key": worker._key, "repository_id": config["repository"]["id"],
                        "project_id": config["project_id"], "topic_id": topic_id,
                        "configuration_id": config["receipt"]["id"], "roster_id": identities["id"],
                        "proposal_snapshots_sha256": digest(json.dumps(snapshots, sort_keys=True)),
                        "subjects": copy.deepcopy(subjects), "body_sha256": digest(body)})
                    record.setdefault("proposal_batches", {})[identity] = {"request": request, "body": body, "proposals": snapshots}
                    worker._write(state)
                finally:
                    worker.project.files_lock.release()
    finally:
        worker._lock.release()
    worker._next, worker._local = 0.0, None
    worker.wake()


def stamp(row):
    value = row.get("created_at")
    if not isinstance(value, str):
        raise Refused("GitHub did not provide the comment's creation version.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Refused("GitHub returned an invalid comment version.") from error
    if parsed.tzinfo is None:
        raise Refused("GitHub returned an ambiguous comment version.")
    return parsed, row["id"]


def deliver(worker, snapshot, state, record, topic):
    """Publish immutable proposal bodies, independently of topic revisions."""
    if not record.get("issue"):
        return
    config, issue = snapshot["config"], record["issue"]
    full = config["repository"]["full_name"]
    data = worker.knowledge._load()
    for batch in batches(worker, config, topic["id"], record).values():
        if batch["request"]["configuration_id"] != config["receipt"]["id"]:
            continue
        try:
            current(worker.knowledge, data, batch["request"]["subjects"])
        except Refused:
            continue
        comments = list(record.get("comments", {}).values())
        matches = [row for row in comments if row["body"] == batch["body"]]
        if len(matches) > 1:
            raise Refused("Multiple GitHub comments claim this proposal batch; its replies are paused.")
        published = batch.get("publication")
        if published:
            found = next((row for row in matches if row["id"] == published["comment_id"]), None)
            if not found:
                raise Refused("The published proposal comment changed; its replies cannot approve notation.")
            stamp(found)
            continue
        if matches:
            result = matches[0]
        else:
            if batch.get("intent", {}).get("uncertain"):
                continue
            batch["intent"] = {"uncertain": True}
            worker._current(snapshot)
            worker._write(state)
            try:
                result = worker._comment(worker._call(snapshot, "create_comment", full, issue["number"], batch["body"]), full, issue["number"])
                if result["body"] != batch["body"]:
                    raise Refused("GitHub did not confirm the published proposal contents.")
                stamp(result)
            except Exception as error:
                batch["intent"]["uncertain"] = bool(getattr(error, "uncertain", True))
                worker._current(snapshot)
                worker._write(state)
                raise
            comments.append(result)
            worker._cache_comments(record, comments)
        stamp(result)
        batch["publication"] = {"comment_id": result["id"], "body_sha256": digest(result["body"]),
                                "created_at": result["created_at"], "html_url": result["html_url"]}
        batch.pop("intent", None)
        record["delivered_bodies"] = sorted(set(record.get("delivered_bodies", [])) | {digest(batch["body"])})
        worker._current(snapshot)
        worker._write(state)


def views(worker, config, repository, data):
    result = []
    import research_topics
    topics = {topic["id"]: topic for topic in research_topics._topics(worker.knowledge, data)}
    access = ResearchAccess(worker.knowledge, refresh=False)
    for topic_id, record in repository["topics"].items():
        for identity, batch in batches(worker, config, topic_id, record).items():
            try:
                if batch["request"]["configuration_id"] != config["receipt"]["id"]:
                    raise Refused("Confirm the current approver configuration and share this proposal again.")
                proposals = current(worker.knowledge, data, batch["request"]["subjects"])
                publication = "published" if batch.get("publication") else "unknown" if batch.get("intent", {}).get("uncertain") else "pending"
            except Refused:
                proposals, publication = [], "stale"
            row = {"id": identity, "topic_id": topic_id, "topic_title": access.review_auxiliary(topics.get(topic_id, {}).get("title", "Research topic")),
                   "subjects": copy.deepcopy(batch["request"]["subjects"]), "proposals": proposals,
                   "publication": publication}
            if record.get("issue"):
                row["issue_url"] = record["issue"]["html_url"]
            if batch.get("publication"):
                row["comment_url"] = batch["publication"]["html_url"]
            errors = [reply.get("clarifications", []) for reply in record.get("proposal_replies", {}).values()
                      if reply.get("batch_id") == identity]
            row["clarifications"] = list(dict.fromkeys(message for group in errors for message in group))
            if publication == "stale":
                row["error"] = "The proposal or approver configuration changed. Review its current version and share it again."
            result.append(row)
    return result


def resolved_at(worker, data, proposals):
    events = []
    for proposal in proposals:
        receipts = [row for row in data.get("decisions", [])
                    if row.get("decision") == proposal["status"] and verify_decision(row, worker._key, proposal)]
        if not receipts:
            return None
        latest = max(receipts, key=lambda row: row["at"])
        if latest.get("channel") == "github_comment":
            events.append(stamp(latest["source"]["comment"]))
        else:
            # A local clock does not order GitHub comments. Keep the historical
            # context for unqualified replies; a named subject stays usable.
            return None
    return max(events)


def action_bindings(worker, text, proposals):
    parsed = parse_actions(text, proposals)
    actions = []
    for action in (parsed or {}).get("actions", []):
        matches = worker.knowledge._action_subjects(action, proposals)
        if len(matches) != 1:
            continue
        proposal = matches[0]
        if proposal.get("status") != "pending" and not (
                proposal.get("status") == "rejected" and action["kind"] in ("reopen", "replace")
                or proposal.get("status") == "approved" and action["kind"] == "reject"):
            continue
        actions.append({"subject": {key: proposal[key] for key in ("id", "revision", "content_hash")}, "action": action})
    return actions


def ingest(worker, snapshot, state, record, topic):
    """Route actual API replies; persist shared decisions before journal acknowledgement."""
    config = snapshot["config"]
    rows = batches(worker, config, topic["id"], record)
    if not rows or not record.get("issue"):
        return False
    processed = record.setdefault("proposal_replies", {})
    if not isinstance(processed, dict):
        raise Refused("The GitHub proposal reply progress is damaged.")
    full, issue = config["repository"]["full_name"], record["issue"]
    published_ids = {row["publication"]["comment_id"] for row in rows.values() if row.get("publication")}
    repository = state["repositories"][str(config["repository"]["id"])]
    identities = repository.get("approver_rosters", {}).get(config["receipt"]["id"])
    if (not isinstance(identities, dict) or not verify_receipt(identities, worker._key)
            or identities.get("kind") != "github_approver_roster"
            or identities.get("configuration_id") != config["receipt"]["id"]
            or identities.get("repository_id") != config["repository"]["id"]):
        return False
    pins = {(row["id"], row["login"].casefold()) for row in identities["accounts"]}
    heads = record.setdefault("proposal_reply_heads", {})
    if not isinstance(heads, dict):
        raise Refused("The GitHub reply version progress is damaged.")
    comments = list(record.get("comments", {}).values())
    comments.sort(key=lambda row: stamp(row))
    changed = False
    for comment in comments:
        worker._comment(comment, full, issue["number"])
        version = digest(json.dumps({key: comment.get(key) for key in
            ("id", "body", "user", "created_at", "updated_at", "issue_url")}, sort_keys=True))
        if version in processed or comment["id"] in published_ids:
            continue
        if digest(comment["body"]) in record.get("delivered_bodies", []):
            continue
        result = {"result": "discussion"}
        user = comment["user"]
        head = heads.get(str(comment["id"]))
        updated = stamp({"created_at": comment.get("updated_at"), "id": comment["id"]})[0]
        if head and updated <= stamp({"created_at": head["updated_at"], "id": comment["id"]})[0]:
            result = {"result": "stale_version"}
        elif (user.get("type") == "User" and user["login"].casefold() in config["approvers"]
              and (user["id"], user["login"].casefold()) in pins and len(comment["body"]) <= 40000):
            first = comment["body"].splitlines()[0] if comment["body"].splitlines() else ""
            selected = re.fullmatch(r"prism-batch:([0-9a-f]{32})", first)
            uncertain = [batch for identity, batch in rows.items()
                         if batch["request"]["configuration_id"] == config["receipt"]["id"]
                         and batch.get("intent", {}).get("uncertain")
                         and (not selected or selected[1] == identity)]
            if uncertain:
                # Missing publication context is temporary. Do not permanently
                # label a genuine later reply as unrelated while a POST settles.
                continue
            candidates = []
            contexts = []
            data = worker.knowledge._load()
            for identity, batch in rows.items():
                if (batch["request"]["configuration_id"] != config["receipt"]["id"]
                        or batch["request"]["roster_id"] != identities["id"]):
                    continue
                publication = batch.get("publication")
                if not publication or stamp(comment) <= stamp({"created_at": publication["created_at"], "id": publication["comment_id"]}):
                    continue
                if selected and selected[1] != identity:
                    continue
                try:
                    proposals = current(worker.knowledge, data, batch["request"]["subjects"])
                except Refused:
                    # A later proposal revision does not erase what an older
                    # unqualified reply could have answered on GitHub.
                    contexts.append((identity, batch, batch["proposals"], False))
                    continue
                contexts.append((identity, batch, proposals, True))
                if not any(proposal["status"] == "pending" for proposal in proposals):
                    continue
                candidates.append((identity, batch))
            outcomes = {}
            if not selected:
                for identity, batch, proposals, applicable in contexts:
                    actions = action_bindings(worker, comment["body"], proposals)
                    if actions:
                        key = json.dumps(actions, sort_keys=True)
                        outcomes.setdefault(key, []).append((identity, batch, applicable))
                if outcomes:
                    matches = next(iter(outcomes.values())) if len(outcomes) == 1 else []
                    candidates = [(identity, batch) for identity, batch, applicable in matches if applicable]
                    candidates.sort(key=lambda item: (stamp({"created_at": item[1]["publication"]["created_at"],
                        "id": item[1]["publication"]["comment_id"]}), item[0]))
                    candidates = candidates[:1]
                    earlier_context = len(outcomes) != 1 or not candidates
                else:
                    earlier_context = any(not applicable or not any(p["status"] == "pending" for p in proposals)
                        and (resolved_at(worker, data, proposals) is None or stamp(comment) <= resolved_at(worker, data, proposals))
                        for _, _, proposals, applicable in contexts)
            else:
                candidates = [(identity, batch) for identity, batch, proposals, applicable in contexts
                    if applicable and (any(p["status"] == "pending" for p in proposals)
                        or action_bindings(worker, comment["body"].partition("\n")[2], proposals))]
                earlier_context = False
            if len(candidates) == 1 and not earlier_context:
                identity, batch = candidates[0]
                if not worker.project.files_lock.acquire(timeout=SAVE_WAIT):
                    raise Conflict("The project files are busy; the GitHub reply will retry.")
                try:
                    worker._current(snapshot)
                    subjects = batch["request"]["subjects"]
                    current(worker.knowledge, worker.knowledge._load(), subjects)
                    source = make_github_source(worker._key, {
                        "repository": {key: config["repository"][key] for key in ("id", "full_name")},
                        "issue": {key: issue[key] for key in ("id", "number")},
                        "topic_id": topic["id"], "batch_id": identity,
                        "publication": {**{key: batch["publication"][key] for key in
                            ("comment_id", "body_sha256", "created_at")}, "subjects": copy.deepcopy(subjects)},
                        "comment": copy.deepcopy(comment), "approvers": copy.deepcopy(config["approvers"]),
                        "approver_roster": copy.deepcopy(identities),
                        "configuration_id": config["receipt"]["id"]})
                    decision = worker.knowledge.decide_github_reply(subjects, source,
                        guard=lambda: worker._current(snapshot))
                    result = {"batch_id": identity, "result": "decided" if decision["decisions"] else "discussion",
                              "decisions": [row["id"] for row in decision["decisions"]],
                              "clarifications": decision["clarifications"]}
                    processed[version] = result
                    heads[str(comment["id"])] = {"updated_at": comment["updated_at"], "version": version}
                    worker._write(state)
                    if decision["decisions"]:
                        return True
                finally:
                    worker.project.files_lock.release()
            elif len(candidates) > 1 or earlier_context:
                result = {"result": "ambiguous", "clarifications": ["Identify the proposal batch before deciding its notation."]}
        if version not in processed:
            processed[version] = result
            if not head or updated > stamp({"created_at": head["updated_at"], "id": comment["id"]})[0]:
                heads[str(comment["id"])] = {"updated_at": comment["updated_at"], "version": version}
            changed = True
    if changed:
        worker._current(snapshot)
        worker._write(state)
    return False
