"""Editor-owned research topic documents and restart-safe pending saves.

Discussion statements are records, never notation decisions. The ordinary Turn
supplies a structured draft; only the editor materializes it after success.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
import uuid
from pathlib import Path

from errors import Conflict, Refused
from fsutil import project_path, replace_file
from project import SAVE_WAIT
import registry
from research import MAX_RECORD, RECORD
from research_edits import sign_receipt, verify_receipt
from research_provenance import Provenance, encode, read_plain

MAX_QUEUE = 4 * 1024 * 1024
MAX_TEXT = 40000
ID = re.compile(r"[0-9a-f]{24}")
SHA = re.compile(r"[0-9a-f]{64}")
DECISION = re.compile(r"^(?:yes[,，\s]+)?(?:we (?:decided|decide|will use)|i (?:decide|choose|confirm)|"
                      r"let's use|use\b|confirmed\b|(?:我|我们)?(?:决定|确定采用|确认采用|选择|确认)(?:了)?)", re.I)
UNCERTAIN = re.compile(r"\b(?:maybe|perhaps|might|could|whether|if|unless|until|provided|assuming|pending|"
                       r"undecided|unresolved)\b|\b(?:subject to|depending on|not yet|not decided|not ready|no decision)\b"
                       r"|也许|或许|是否|考虑|如果|除非|取决于|尚未|未决定|未确定|没有决定|还未", re.I)
FAILED = "Research note could not be saved; its pending update remains available for retry."
REISSUE = "Research note was not saved. Repeat its topic update because successful completion could not be recorded."


class _Stopped(Exception):
    pass


def _check_stop(job):
    if job is not None and job.cancel.is_set():
        raise _Stopped


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _normal(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _batch(job):
    if not getattr(job, "topic_batch", None):
        job.topic_batch = uuid.uuid4().hex
    return job.topic_batch


def _text(value, *, limit=MAX_TEXT):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise Refused("A research topic needs bounded, nonempty text.")
    return value.strip().replace("\r\n", "\n").replace("\r", "\n")


def _key(knowledge):
    return registry.project_key(knowledge.project.root)


def _queue_path(knowledge):
    folder = registry.state_dir() / "research-topic-outbox"
    path = folder / (_key(knowledge) + ".json")
    if folder.is_symlink() or path.is_symlink():
        raise Refused("Research pending saves must use regular local state files.")
    return path


def _read_queue(knowledge):
    path = _queue_path(knowledge)
    try:
        if path.stat().st_size > MAX_QUEUE:
            raise Refused("The research pending-save limit was reached.")
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": 1, "project_key": _key(knowledge), "drafts": []}
    except (UnicodeError, json.JSONDecodeError) as error:
        raise Refused("The pending research saves are damaged; nothing was changed.") from error
    if (not isinstance(data, dict) or data.get("schema") != 1 or data.get("project_key") != _key(knowledge)
            or not isinstance(data.get("drafts"), list) or len(data["drafts"]) > 100):
        raise Refused("The pending research saves are damaged; nothing was changed.")
    for draft in data["drafts"]:
        if (not isinstance(draft, dict) or not isinstance(draft.get("owner"), str)
                or type(draft.get("ready")) is not bool or type(draft.get("base_revision")) is not int):
            raise Refused("The pending research saves are damaged; nothing was changed.")
        _validate_topic(knowledge, draft)
    return data


def _write_queue(knowledge, queue):
    raw = (json.dumps(queue, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(raw) > MAX_QUEUE or len(queue["drafts"]) > 100:
        raise Refused("The research pending-save limit was reached.")
    replace_file(_queue_path(knowledge), raw, durable=True)


def _try_queue(knowledge, queue):
    try:
        _write_queue(knowledge, queue)
        return True
    except (OSError, ValueError):
        return False


def _forget(knowledge, queue, draft):
    queue["drafts"].remove(draft)
    _try_queue(knowledge, queue)


def _valid_entry(knowledge, entry):
    if (not isinstance(entry, dict) or entry.get("kind") not in ("idea", "decision", "question")
            or not isinstance(entry.get("id"), str) or not ID.fullmatch(entry["id"])):
        return False
    try:
        text = _text(entry.get("text"))
    except ValueError:
        return False
    receipt = entry.get("provenance")
    return bool(isinstance(receipt, dict) and receipt.get("kind") == "topic_statement"
                and receipt.get("entry_id") == entry["id"] and receipt.get("entry_kind") == entry["kind"]
                and receipt.get("text_sha256") == _digest(text.encode())
                and receipt.get("source") in ("human", "assistant")
                and verify_receipt(receipt, _key(knowledge)))


def _received_shape(topic):
    if (not isinstance(topic, dict) or not isinstance(topic.get("id"), str) or not ID.fullmatch(topic["id"])
            or topic.get("path") != f"research/topics/{topic['id']}.md"
            or type(topic.get("revision")) is not int or topic["revision"] < 1
            or not isinstance(topic.get("sha256"), str) or not SHA.fullmatch(topic["sha256"])
            or not isinstance(topic.get("entries"), list) or not 1 <= len(topic["entries"]) <= 1000
            or not isinstance(topic.get("related_topics"), list)
            or any(not isinstance(identity, str) or not ID.fullmatch(identity) for identity in topic["related_topics"])):
        raise Refused("The received topic identity or document binding is damaged.")
    title = _text(topic.get("title"), limit=160)
    if title != topic["title"] or "\n" in title:
        raise Refused("The received topic title is damaged.")
    for entry in topic["entries"]:
        source = entry.get("provenance") if isinstance(entry, dict) else None
        if (not isinstance(source, dict) or source.get("kind") != "topic_statement"
                or source.get("topic_id") != topic["id"] or source.get("topic_title") != title
                or source.get("entry_id") != entry.get("id") or source.get("entry_kind") != entry.get("kind")
                or entry.get("kind") not in ("idea", "decision", "question")
                or not isinstance(entry.get("id"), str) or not ID.fullmatch(entry["id"])
                or not isinstance(entry.get("text"), str) or len(entry["text"]) > MAX_TEXT
                or source.get("text_sha256") != _digest(entry["text"].encode())):
            raise Refused("The received topic statement binding is damaged.")


def _received_reference_shape(reference):
    if (not isinstance(reference, dict) or reference.get("kind") != "research_topic_reference"
            or reference.get("source") != "unverified_received_reference"
            or not isinstance(reference.get("topic_id"), str) or not ID.fullmatch(reference["topic_id"])
            or reference.get("path") != f"research/topics/{reference['topic_id']}.md"
            or type(reference.get("revision")) is not int or reference["revision"] < 1
            or any(not isinstance(reference.get(key), str) or not SHA.fullmatch(reference[key])
                   for key in ("record_sha256", "document_sha256", "topic_sha256"))):
        raise Refused("The relayed topic reference binding is damaged.")
    title = _text(reference.get("topic_title"), limit=160)
    if title != reference["topic_title"] or "\n" in title:
        raise Refused("The relayed topic reference title is damaged.")
    return {"id": reference["topic_id"], "title": title, "path": reference["path"],
            "revision": reference["revision"], "sha256": reference["document_sha256"]}


def _received_items(record):
    discussion = record.get("discussion", {})
    topics, references = discussion.get("topics", []), discussion.get("topic_references", [])
    if (not isinstance(topics, list) or not isinstance(references, list)
            or len(topics) + len(references) > 600):
        raise Refused("The received topic index is damaged.")
    result = []
    for topic in topics:
        _received_shape(topic)
        result.append((topic, topic, "topics"))
    seen_references = set()
    for reference in references:
        topic = _received_reference_shape(reference)
        if topic["id"] in seen_references:
            raise Refused("The received topic references have duplicate identities.")
        seen_references.add(topic["id"])
        continued = next((item for item in topics if item["id"] == topic["id"]), None)
        if continued:
            if continued.get("received_reference") != reference:
                raise Refused("The received topic and its reference have conflicting identities.")
            continue
        result.append((topic, reference, "topic_references"))
    return result


def _validated_locator(value, topic_id):
    repository = value.get("repository") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("topic_id") != topic_id
            or not isinstance(value.get("project_id"), str) or re.fullmatch(r"[0-9a-f]{32}", value["project_id"]) is None
            or not isinstance(repository, dict) or type(repository.get("id")) is not int or repository["id"] < 1
            or not isinstance(repository.get("full_name"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository["full_name"]) is None
            or repository.get("html_url") != "https://github.com/" + repository["full_name"]):
        raise Refused("The received GitHub publication locator is malformed; revalidate its issue before publishing.")
    return {"project_id": value["project_id"], "topic_id": topic_id,
            "repository": {key: repository[key] for key in ("id", "full_name", "html_url")}}


def _observed_locator(record, source, topic_id):
    inherited = source if source.get("kind") == "research_topic_reference" else source.get("received_reference", {})
    inherited = inherited.get("publication_locator") if isinstance(inherited, dict) else None
    discussion = record.get("discussion", {})
    config = discussion.get("github")
    if inherited is None:
        if config is None and discussion.get("mode") != "github":
            return None
        inherited = {"project_id": config.get("project_id") if isinstance(config, dict) else None,
                     "repository": config.get("repository") if isinstance(config, dict) else None,
                     "topic_id": topic_id}
    try:
        return _validated_locator(inherited, topic_id)
    except Refused:
        return {"invalid": True}


def _reference_bytes(knowledge, receipt):
    """A local observation binds retained bytes, never the foreign claims' authority."""
    if (not isinstance(receipt, dict) or receipt.get("kind") != "research_topic_reference"
            or receipt.get("root") != str(knowledge.project.root.resolve())
            or not verify_receipt(receipt, _key(knowledge))
            or any(not isinstance(receipt.get(key), str) or not SHA.fullmatch(receipt[key])
                   for key in ("record_sha256", "document_sha256", "topic_sha256"))):
        raise Refused("The received topic reference has no verified local observation.")
    store = Provenance(knowledge.project.root, RECORD, MAX_RECORD)
    folder = store.folder / "received"
    if folder.is_symlink():
        raise Refused("The retained topic reference cannot use a linked folder.")
    record_raw = read_plain(folder / (receipt["record_sha256"] + ".record"), MAX_RECORD)
    raw = read_plain(folder / (receipt["document_sha256"] + ".record"), MAX_QUEUE)
    if _digest(record_raw) != receipt["record_sha256"] or _digest(raw) != receipt["document_sha256"]:
        raise Refused("The retained received topic reference changed.")
    received = json.loads(record_raw)
    found = next(((topic, source) for topic, source, field in _received_items(received)
                  if topic["id"] == receipt.get("topic_id") and field == receipt.get("record_field", "topics")), None)
    topic, source = found if found else (None, None)
    if (topic is None or _digest(encode(source)) != receipt["topic_sha256"]
            or any(topic.get(key) != receipt.get(field) for key, field in
                   (("id", "topic_id"), ("title", "topic_title"), ("path", "path"),
                    ("revision", "revision"), ("sha256", "document_sha256")))):
        raise Refused("The retained received topic identity changed.")
    return raw


def retain_received_topics(knowledge, existing):
    """Called only by explicit initialization; retain descriptors without importing trust."""
    if not existing or not existing.get("provenance", {}).get("received_references"):
        return []
    store = Provenance(knowledge.project.root, RECORD, MAX_RECORD)
    observation = store.inspect()
    if observation["error"] or observation["sha256"] != existing["provenance"].get("received_sha256"):
        raise Conflict("The received research record changed before topic references were retained.")
    record = observation.get("received") or {}
    topics = _received_items(record)
    if len(topics) > 300:
        raise Refused("The received topic index is damaged.")
    checked, identities, titles, paths = [], set(), set(), set()
    for topic, source, field in topics:
        if topic["id"] in identities or _normal(topic["title"]) in titles or topic["path"] in paths:
            raise Refused("The received topic index has duplicate or conflicting bindings.")
        identities.add(topic["id"])
        titles.add(_normal(topic["title"]))
        paths.add(topic["path"])
        path = _path(knowledge, topic)
        raw = read_plain(path, MAX_QUEUE)
        if (_digest(raw) != topic["sha256"] or not raw.decode("utf-8").splitlines()
                or raw.decode("utf-8").splitlines()[0] != "# " + topic["title"]):
            raise Refused("The received topic document does not match its recorded version.")
        checked.append((topic, raw, source, field))
    references = []
    if checked:
        store._archive(observation["raw"])
    for topic, raw, source, field in checked:
        store._archive(raw)
        references.append(sign_receipt({"id": uuid.uuid4().hex, "kind": "research_topic_reference",
            "project_key": _key(knowledge), "root": str(knowledge.project.root.resolve()),
            "topic_id": topic["id"], "topic_title": topic["title"], "path": topic["path"],
            "revision": topic["revision"], "document_sha256": topic["sha256"],
            "record_sha256": observation["sha256"], "topic_sha256": _digest(encode(source)), "record_field": field,
            "received_project_key": record.get("project_key"), "source": "unverified_received_reference",
            "publication_locator": _observed_locator(record, source, topic["id"])}))
    return references


def _reference_topics(knowledge, data):
    references = data["discussion"].get("topic_references", [])
    if not isinstance(references, list) or len(references) > 300:
        raise Refused("The received topic references are damaged.")
    result = []
    for reference in references:
        _reference_bytes(knowledge, reference)
        result.append({"id": reference["topic_id"], "title": reference["topic_title"], "path": reference["path"],
                       "revision": reference["revision"], "sha256": reference["document_sha256"],
                       "entries": [], "related_topics": [], "received_reference": reference})
    if (len({topic["id"] for topic in result}) != len(result)
            or len({_normal(topic["title"]) for topic in result}) != len(result)):
        raise Refused("The received topic references have duplicate identities.")
    return result


def publication_reference(knowledge, topic):
    """An unverified namespace locator; the caller must revalidate the remote repository."""
    reference = topic.get("received_reference")
    if not reference:
        return None
    _reference_bytes(knowledge, reference)
    store = Provenance(knowledge.project.root, RECORD, MAX_RECORD)
    received = json.loads(read_plain(store.folder / "received" / (reference["record_sha256"] + ".record"), MAX_RECORD))
    if "publication_locator" in reference:
        locator = reference["publication_locator"]
    else:
        source = next(source for item, source, field in _received_items(received)
                      if item["id"] == topic["id"] and field == reference.get("record_field", "topics"))
        locator = _observed_locator(received, source, topic["id"])
    if locator is None:
        return None
    return {**_validated_locator(locator, topic["id"]), "record_sha256": reference["record_sha256"]}


def _validate_topic(knowledge, topic):
    if (not isinstance(topic.get("id"), str) or not ID.fullmatch(topic["id"])
            or topic.get("path") != f"research/topics/{topic['id']}.md"
            or not isinstance(topic.get("entries"), list) or not 1 <= len(topic["entries"]) <= 1000
            or not all(_valid_entry(knowledge, entry) for entry in topic["entries"])
            or any(entry["provenance"].get("topic_id") != topic["id"]
                   or entry["provenance"].get("topic_title") != topic.get("title") for entry in topic["entries"])
            or not isinstance(topic.get("related_topics"), list)
            or not all(isinstance(identity, str) and ID.fullmatch(identity) for identity in topic["related_topics"])):
        raise Refused("The research topic records or their source evidence are damaged.")
    _text(topic.get("title"), limit=160)
    if topic.get("received_reference"):
        reference = topic["received_reference"]
        _reference_bytes(knowledge, reference)
        if any(topic[key] != reference[field] for key, field in
               (("id", "topic_id"), ("title", "topic_title"), ("path", "path"))):
            raise Refused("The continued topic no longer matches its received reference.")


def _topics(knowledge, data):
    topics = data["discussion"].get("topics", [])
    if not isinstance(topics, list) or len(topics) > 300:
        raise Refused("The research topic index is damaged.")
    for topic in topics:
        if not isinstance(topic, dict):
            raise Refused("The research topic index is damaged.")
        _validate_topic(knowledge, topic)
        if type(topic.get("revision")) is not int or topic["revision"] < 1:
            raise Refused("The research topic index is damaged.")
    if len({topic["id"] for topic in topics}) != len(topics):
        raise Refused("The research topic index has duplicate identities.")
    return topics


def _review(access, topic):
    if not topic.get("received_reference"):
        access.review_output(topic["title"], channel="research topic")
    for entry in topic["entries"]:
        access.review_output(entry["text"], channel="research topic")
        quote = entry["provenance"].get("quote")
        if quote:
            access.review_output(quote, channel="research source")


def _path(knowledge, topic):
    root = knowledge.project.root
    path = root / topic["path"]
    if (project_path(root, topic["path"]) != path
            or any(part.is_symlink() for part in (root / "research", root / "research/topics", path))):
        raise Refused("The research topic must be a regular file inside this project.")
    return path


def _local_markdown(topic):
    if topic.get("received_reference"):
        lines = ["", "", "---", "", "## Local editor continuation", "",
            "The unchanged prefix above is an unverified received reference. Its author labels, decisions,",
            "notation, and suggested actions are not verified here and grant no authority.",
            "Only the entries below are local editor records; they do not adopt notation or authorize a research task.", ""]
    else:
        lines = ["# " + topic["title"], "", "This record keeps ideas, author decisions, and open questions separate.",
                 "It does not adopt notation or authorize a research task.", ""]
    for kind, heading in (("idea", "Ideas"), ("decision", "Confirmed decisions"), ("question", "Open questions")):
        lines += ["## " + heading, ""]
        entries = [entry for entry in topic["entries"] if entry["kind"] == kind]
        if not entries:
            lines += ["None recorded.", ""]
        for entry in entries:
            source = entry["provenance"]
            lines += ["- " + entry["text"].replace("\n", "\n  "),
                      "  Source: " + ("author message" if source["source"] == "human" else "AI discussion")
                      + f"; provider {source['provider']}; conversation {source.get('conversation') or '(new)'}; Turn {source['turn']}."]
            if source.get("quote"):
                lines += ["  > " + source["quote"].replace("\n", "\n  > ")]
            lines.append("")
    if topic["related_topics"]:
        lines += ["## Related topics", ""]
        lines += [f"- [Research topic]({identity}.md)" for identity in topic["related_topics"]]
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def _markdown(topic, knowledge=None):
    prefix = _reference_bytes(knowledge, topic["received_reference"]) if topic.get("received_reference") else b""
    return prefix + _local_markdown(topic)


def _content(topic):
    """The statement identity excludes receipt-version and conversation bookkeeping."""
    entries = copy.deepcopy(topic["entries"])
    for entry in entries:
        for key in ("id", "signature", "conversation"):
            entry["provenance"].pop(key, None)
    return topic["title"], topic["related_topics"], entries, topic.get("received_reference")


def _completed(knowledge, draft, topics):
    topic = next((topic for topic in topics if topic["id"] == draft["id"]), None)
    if (not topic or topic["revision"] != draft.get("next_revision", draft["base_revision"] + 1)
            or _content(topic) != _content(draft)):
        return False
    try:
        path = _path(knowledge, topic)
        return path.stat().st_size <= MAX_QUEUE and _digest(path.read_bytes()) == topic["sha256"]
    except (OSError, ValueError):
        return False


def stage(access, job, args):
    """Persist an unready draft, without publishing it as a saved document."""
    knowledge, project = access.knowledge, access.knowledge.project
    if not access.initialized:
        raise Refused("Initialize the research baseline before recording discussions.")
    if not isinstance(args, dict) or len(json.dumps(args, ensure_ascii=False)) > MAX_TEXT:
        raise Refused("The research topic update is too large or malformed.")
    if not project.files_lock.acquire(timeout=SAVE_WAIT):
        raise Conflict("The project files are busy; retry the research note later.")
    try:
        data, queue = knowledge._load(), _read_queue(knowledge)
        if not data or data["discussion"].get("mode") not in ("markdown", "github"):
            raise Refused("Project discussion records are not enabled.")
        topics = _topics(knowledge, data)
        references = _reference_topics(knowledge, data)
        known = {topic["id"]: topic for topic in [*references, *topics, *queue["drafts"]]}
        title = _text(args.get("title"), limit=160)
        if "\n" in title:
            raise Refused("A research topic title must fit on one line.")
        identity = args.get("topic_id")
        if identity is not None and (not isinstance(identity, str) or identity not in known):
            raise Refused("Choose an existing topic from this project's index.")
        matched = known.get(identity) if identity else next(
            (topic for topic in known.values() if _normal(topic["title"]) == _normal(title)), None)
        if matched and _normal(matched["title"]) != _normal(title):
            raise Refused("Use the existing topic title rather than silently renaming it.")
        identity = matched["id"] if matched else _digest((_key(knowledge) + "\n" + _normal(title)).encode())[:24]
        if identity not in known and len(known) >= 300:
            raise Refused("The research topic limit was reached.")
        prior = next((topic for topic in topics if topic["id"] == identity), None)
        draft = next((topic for topic in queue["drafts"] if topic["id"] == identity), None)
        base = copy.deepcopy(draft or prior or matched or {"id": identity, "title": title,
            "path": f"research/topics/{identity}.md", "entries": [], "related_topics": []})
        if base.get("received_reference") and data.get("provenance", {}).get("record_state") != "local":
            raise Conflict("The received research projection changed; revalidate it before continuing its topic.")
        if matched and matched.get("received_reference") and not prior and not draft:
            if _digest(read_plain(_path(knowledge, matched), MAX_QUEUE)) != matched["sha256"]:
                raise Conflict("The received topic document changed; its exact bytes were kept.")
        if draft:
            base["application_sha256"] = _digest(_markdown(draft, knowledge))
        entries = args.get("entries")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
            raise Refused("A research topic update needs between one and 100 statements.")
        if not base.get("received_reference"):
            access.review_output(title, channel="research topic")
        additions = []
        for item in entries:
            if not isinstance(item, dict) or item.get("kind") not in ("idea", "decision", "question") \
                    or item.get("source") not in ("human", "assistant"):
                raise Refused("Choose an idea, an author decision, or an open question with its source.")
            text = _text(item.get("text"))
            quote = _text(item["quote"]) if item.get("quote") else ""
            human = getattr(job, "human_text", None)
            if item["source"] == "human" and (not isinstance(human, str) or not quote or quote not in human):
                raise Refused("An author source must quote the actual human message.")
            if item["kind"] == "decision" and (item["source"] != "human" or quote != human.strip() or text != quote
                    or not DECISION.match(quote) or UNCERTAIN.search(quote)):
                raise Refused("A confirmed decision needs an explicit unquoted author choice; keep uncertain material tentative.")
            access.review_output(text, channel="research topic")
            if quote:
                access.review_output(quote, channel="research source")
            semantic = [item["kind"], _normal(text), item["source"], _normal(quote)]
            entry_id = _digest(json.dumps(semantic, ensure_ascii=False).encode())[:24]
            if any(entry["id"] == entry_id for entry in [*base["entries"], *additions]):
                continue
            payload = {"id": uuid.uuid4().hex, "project_key": _key(knowledge), "kind": "topic_statement",
                       "topic_id": identity, "topic_title": base["title"],
                       "entry_id": entry_id, "entry_kind": item["kind"], "text_sha256": _digest(text.encode()),
                       "source": item["source"], "quote": quote, "provider": str(job.provider),
                       "conversation": job.session_id or "", "turn": job.id}
            if item["source"] == "human":
                payload["human_text"] = human
            additions.append({"id": entry_id, "kind": item["kind"], "text": text, "provenance": payload})
        related = args.get("related_topics", [])
        if not isinstance(related, list) or any(not isinstance(value, str) or value not in known or value == identity for value in related):
            raise Refused("Related topics must identify other topics in this project.")
        combined = sorted(set(base["related_topics"]) | set(related))
        if not additions and combined == base["related_topics"]:
            if draft and not draft["ready"] and draft["owner"] != _batch(job):
                draft["application_sha256"] = _digest(_markdown(draft, knowledge))
                draft["owner"] = job.topic_batch
                _write_queue(knowledge, queue)
            return {"queued": bool(draft), "changed": False, "topic_id": identity, "path": base["path"]}
        if draft and draft["ready"]:
            raise Refused("This topic has a pending save; recover it before adding another update.")
        if len(base["entries"]) + len(additions) > 1000:
            raise Refused("The research topic statement limit was reached.")
        for entry in additions:
            entry["provenance"] = sign_receipt(entry["provenance"])
        base.update(entries=base["entries"] + additions, related_topics=combined, owner=_batch(job), ready=False,
                    base_revision=draft["base_revision"] if draft else prior["revision"] if prior else 0,
                    base_sha256=draft.get("base_sha256") if draft else prior.get("sha256") if prior else None)
        base["next_revision"] = (draft.get("next_revision", draft["base_revision"] + 1) if draft else
                                 prior["revision"] + 1 if prior else matched["revision"] + 1 if matched else 1)
        base.pop("revision", None)
        base.pop("sha256", None)
        queue["drafts"] = [topic for topic in queue["drafts"] if topic["id"] != identity] + [base]
        _write_queue(knowledge, queue)
        return {"queued": True, "changed": True, "topic_id": identity, "path": base["path"]}
    finally:
        project.files_lock.release()


def _apply(access, queue, changed, job=None, *, unpersisted=()):
    from research_access import NotationStop
    knowledge, project = access.knowledge, access.knowledge.project
    for draft in list(queue["drafts"]):
        if not draft["ready"]:
            continue
        try:
            _check_stop(job)
            _review(access, draft)
            _check_stop(job)
            data = knowledge._load()
            if not data or data["discussion"].get("mode") not in ("markdown", "github"):
                raise Refused("Project discussion records are not enabled.")
            if draft.get("received_reference") and data.get("provenance", {}).get("record_state") != "local":
                raise Conflict("The received research projection changed; its topic bytes were kept.")
            topics = _topics(knowledge, data)
            if _completed(knowledge, draft, topics):
                _forget(knowledge, queue, draft)
                continue
            prior = next((topic for topic in topics if topic["id"] == draft["id"]), None)
            rendered = _markdown(draft, knowledge)
            access.review_output(_local_markdown(draft).decode("utf-8"), channel="research record")
            _check_stop(job)
            digest = _digest(rendered)
            if prior and prior.get("sha256") == digest:
                _forget(knowledge, queue, draft)
                continue
            if ((prior["revision"] if prior else 0) != draft["base_revision"]
                    or (prior.get("sha256") if prior else None) != draft.get("base_sha256")):
                raise Conflict("The topic changed while this update was pending.")
            path = _path(knowledge, draft)
            _check_stop(job)
            current = path.read_bytes() if path.exists() else None
            if current != rendered:
                if (None if current is None else _digest(current)) not in (
                        draft.get("base_sha256"), draft.get("application_sha256"),
                        draft.get("received_reference", {}).get("document_sha256")):
                    raise Conflict("The topic document changed; its received text was kept.")
                _check_stop(job)
                replace_file(path, rendered, durable=True)
                project.turn.server_wrote([draft["path"]])
                changed.append(draft["path"])
            topic = {key: copy.deepcopy(draft[key]) for key in
                     ("id", "title", "path", "entries", "related_topics")}
            if draft.get("received_reference"):
                topic["received_reference"] = copy.deepcopy(draft["received_reference"])
            topic.update(sha256=digest, revision=draft.get("next_revision", draft["base_revision"] + 1))
            data["discussion"]["topics"] = [t for t in topics if t["id"] != topic["id"]] + [topic]
            # Once the document application starts, finish its canonical index
            # even if Stop arrives. Do not leave an unindexed document behind.
            knowledge._write(data)
            changed.append(RECORD)
            _forget(knowledge, queue, draft)
            if job is not None and not job.cancel.is_set():
                job.emit({"t": "research_record", "topic_id": topic["id"],
                          "title": access.review_auxiliary(topic["title"], channel="research record location"),
                          "path": topic["path"], "sha256": digest, "revision": topic["revision"]})
        except _Stopped:
            if draft in queue["drafts"] and draft["owner"] == getattr(job, "topic_batch", None):
                _forget(knowledge, queue, draft)
            return
        except NotationStop:
            if job is None or not job.cancel.is_set():
                raise
            if draft in queue["drafts"] and draft["owner"] == getattr(job, "topic_batch", None):
                _forget(knowledge, queue, draft)
            return
        except (OSError, ValueError):
            # A pending update can be retried without repeating model generation.
            automatic = draft["id"] not in unpersisted
            if draft in queue["drafts"] and draft.get("error") != FAILED:
                draft["error"] = FAILED
                automatic = _try_queue(knowledge, queue) or automatic
            if job is not None and not job.cancel.is_set():
                job.emit({"t": "research_record_error", "message": FAILED if automatic else REISSUE,
                          "retryable": automatic,
                          "topic_id": draft["id"], "path": draft["path"]})


def finish(access, job, *, success, session_id=None):
    """Make this successful Turn's drafts eligible, then save pending updates."""
    project, knowledge = access.knowledge.project, access.knowledge
    changed = []
    if not project.files_lock.acquire(timeout=SAVE_WAIT):
        raise Conflict("The project files are busy; research notes remain pending.")
    try:
        queue = _read_queue(knowledge)
        owner = getattr(job, "topic_batch", None)
        own = [draft for draft in queue["drafts"] if owner and draft["owner"] == owner]
        entry_versions = {draft["id"]: copy.deepcopy(draft["entries"]) for draft in own}
        unpersisted = []
        if own:
            if not success or job.cancel.is_set():
                queue["drafts"] = [draft for draft in queue["drafts"] if draft not in own]
            else:
                for draft in own:
                    draft["ready"] = True
                    if session_id:
                        for entry in draft["entries"]:
                            receipt = entry["provenance"]
                            if not receipt.get("conversation"):
                                payload = {key: value for key, value in receipt.items() if key != "signature"}
                                payload["id"] = uuid.uuid4().hex
                                payload["conversation"] = session_id
                                entry["provenance"] = sign_receipt(payload)
            if not _try_queue(knowledge, queue):
                unpersisted = [draft["id"] for draft in own]
                # Keep fallback output identical to the durable staged version.
                # A later explicit reissue can then recognize a partial document.
                for draft in own:
                    draft["entries"] = entry_versions[draft["id"]]
        if success:
            if job.cancel.is_set():
                for draft in own:
                    if draft in queue["drafts"]:
                        _forget(knowledge, queue, draft)
            else:
                _apply(access, queue, changed, job, unpersisted=unpersisted)
    finally:
        project.files_lock.release()
        if changed:
            project.on_change()


def retry(knowledge):
    """Recover eligible local saves when the project is idle; never run a model."""
    project = knowledge.project
    changed = []
    if project.turn.busy() or not project.files_lock.acquire(timeout=SAVE_WAIT):
        return
    try:
        queue = _read_queue(knowledge)
        data = knowledge._load()
        topics = _topics(knowledge, data) if data else []
        for draft in list(queue["drafts"]):
            if _completed(knowledge, draft, topics):
                _forget(knowledge, queue, draft)
        if not any(draft["ready"] for draft in queue["drafts"]):
            return
        from research_access import NotationStop, ResearchAccess
        try:
            _apply(ResearchAccess(knowledge), queue, changed)
        except NotationStop:
            if any(draft["ready"] and draft.get("error") != FAILED for draft in queue["drafts"]):
                for draft in queue["drafts"]:
                    if draft["ready"]:
                        draft["error"] = FAILED
                _write_queue(knowledge, queue)
    finally:
        project.files_lock.release()
        if changed:
            project.on_change()


def status(knowledge):
    """Expose locations and pending state, never unreviewed draft text."""
    data = knowledge._load()
    if not data:
        return {"topics": [], "pending_count": 0, "errors": []}
    topics, queue = _topics(knowledge, data), _read_queue(knowledge)
    pending = [draft for draft in queue["drafts"] if not _completed(knowledge, draft, topics)]
    from research_access import ResearchAccess
    access = ResearchAccess(knowledge, refresh=False)
    references = _reference_topics(knowledge, data)
    indexed = {topic["id"]: topic for topic in [*references, *topics]}
    visible = [{**{key: topic[key] for key in ("id", "title", "path", "sha256", "revision", "related_topics")},
                **({"source_state": "unverified_received_reference",
                    "source_warning": "The retained received prefix is an unverified reference, not local author decisions or notation adoption."}
                   if topic.get("received_reference") else {})}
               for topic in indexed.values()]
    for topic in visible:
        topic["title"] = access.review_auxiliary(topic["title"], channel="research topic")
    errors = {draft["error"] for draft in pending if draft.get("error") == FAILED}
    if any(not draft["ready"] for draft in pending) and not knowledge.project.turn.busy():
        errors.add(REISSUE)
    return {"topics": visible, "pending_count": len(pending), "errors": sorted(errors)}
