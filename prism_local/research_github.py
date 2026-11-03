"""Project-lifetime GitHub discussion synchronization, separate from file sync.

Local topic documents remain authoritative. A private intent journal distinguishes
confirmed delivery from unknown POST outcomes; unknown writes are reconciled, not
blindly repeated. External comments are discussion data, never human decisions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import uuid

from errors import Conflict, Refused
from fsutil import replace_file
from guarded_compile import _read_plain
import github_issues
from project import SAVE_WAIT
import registry
from research_access import ResearchAccess
from research_edits import sign_receipt, verify_receipt
import research_topics
import research_github_approvals as approvals

MAX_STATE = 8 * 1024 * 1024
ERROR = "GitHub discussion synchronization could not finish. Local research notes are preserved."
UNKNOWN = "The publication result is unknown. Existing GitHub records will be checked before another delivery."


class _Obsolete(Exception):
    pass


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class GitHubDiscussions:
    def __init__(self, knowledge, *, adapter=None, target=None, is_open=lambda: True,
                 clock=time.monotonic, interval=30):
        self.knowledge, self.project = knowledge, knowledge.project
        self.adapter, self.target = adapter, target or (lambda: {})
        self.is_open, self.clock, self.interval = is_open, clock, interval
        self._root = self.project.root
        self._key = registry.project_key(self._root)
        self._lock = threading.Lock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread = None
        self._closed = False
        self._generation = 0
        self._next = 0.0
        self._was_open = False
        self._local = None
        self._error = None

    def _client(self, allowed=None):
        if self.adapter is None:
            return github_issues.IssueAdapter(allowed=allowed)
        return self.adapter

    def _path(self):
        folder = registry.state_dir() / "research-github"
        path = folder / (self._key + ".json")
        if folder.is_symlink() or path.is_symlink():
            raise Refused("GitHub discussion progress must use regular local files.")
        return path

    def _read(self):
        path = self._path()
        try:
            if path.stat().st_size > MAX_STATE:
                raise Refused("GitHub discussion progress exceeds its local limit.")
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": 1, "project_key": self._key, "repositories": {}}
        except (UnicodeError, json.JSONDecodeError) as error:
            raise Refused("GitHub discussion progress is damaged; nothing was published.") from error
        if (not isinstance(state, dict) or state.get("schema") != 1 or state.get("project_key") != self._key
                or not isinstance(state.get("repositories"), dict)):
            raise Refused("GitHub discussion progress is damaged; nothing was published.")
        for repo in state["repositories"].values():
            if not isinstance(repo, dict) or not isinstance(repo.get("topics"), dict):
                raise Refused("GitHub discussion progress is damaged.")
            for identity, record in repo["topics"].items():
                if (not re.fullmatch(r"[0-9a-f]{24}", identity) or not isinstance(record, dict)
                        or not isinstance(record.get("comments", {}), dict)
                        or not isinstance(record.get("delivered_bodies", []), list)
                        or not all(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
                                   for digest in record.get("delivered_bodies", []))
                        or ("issue" in record and not isinstance(record["issue"], dict))
                        or ("intent" in record and (not isinstance(record["intent"], dict)
                            or record["intent"].get("kind") not in ("issue", "comment")
                            or not isinstance(record["intent"].get("body"), str)
                            or type(record["intent"].get("uncertain")) is not bool))
                        or any(field in record and not isinstance(record[field], dict)
                               for field in ("proposal_batches", "proposal_replies", "proposal_reply_heads"))):
                    raise Refused("GitHub discussion progress is damaged.")
        return state

    def _write(self, state):
        raw = (_json(state) + "\n").encode("utf-8")
        if len(raw) > MAX_STATE:
            raise Refused("GitHub discussion progress exceeds its local limit.")
        path = self._path()
        if path.exists() and path.read_bytes() == raw:
            return
        replace_file(path, raw, durable=True)

    def _consent(self, receipt=None):
        path = self._path().with_name(self._key + ".configuration.json")
        if path.is_symlink():
            raise Refused("Discussion settings must use regular local state files.")
        if receipt is not None:
            replace_file(path, (_json({"project_key": self._key, "receipt_id": receipt["id"]}) + "\n").encode(), durable=True)
            return receipt["id"]
        try:
            if path.stat().st_size > 16384:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (UnicodeError, json.JSONDecodeError):
            return None
        return data.get("receipt_id") if isinstance(data, dict) and data.get("project_key") == self._key else None

    def _witnessed(self, config, mode):
        receipt = config.get("receipt")
        payload = {"mode": mode, **{key: config.get(key) for key in
                   ("repository", "bound_repository", "approvers", "project_id")}}
        return bool(isinstance(receipt, dict) and receipt.get("kind") == "discussion_configuration"
                    and receipt.get("configuration") == payload and receipt.get("id") == self._consent()
                    and verify_receipt(receipt, self._key))

    def _bound(self):
        view = self.target()
        current, bound = view.get("target"), view.get("bound_target")
        if (not view.get("own") or view.get("blocked_reason") or not isinstance(current, dict)
                or not isinstance(bound, dict) or any(current.get(key) != bound.get(key) for key in
                    ("repository_identity", "branch_ref", "local_branch"))):
            raise Refused("Confirm the project's GitHub repository in the Git menu before using discussion issues.")
        identity = current.get("repository_identity", "")
        if not isinstance(identity, str) or not re.fullmatch(r"github\.com/[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", identity):
            raise Refused("The confirmed project target is not a GitHub repository.")
        return identity.removeprefix("github.com/")

    def _configuration(self, data):
        discussion = (data or {}).get("discussion", {})
        config = discussion.get("github")
        if discussion.get("mode") != "github":
            return None
        if not isinstance(config, dict):
            raise Refused("Confirm the GitHub discussion settings in this editor.")
        if not self._witnessed(config, "github"):
            raise Refused("Confirm the GitHub discussion settings in this editor.")
        if config.get("bound_repository") != self._bound():
            raise Refused("The bound GitHub repository changed. Confirm the discussion settings again.")
        return config

    def _snapshot(self):
        if self._closed or not self.is_open() or self.project.turn.busy():
            raise _Obsolete
        if self.project.root != self._root or not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise _Obsolete
        try:
            data = self.knowledge._load()
            if not data:
                raise _Obsolete
            config = self._configuration(data)
            if config is None:
                raise _Obsolete
            topics = copy.deepcopy(research_topics._topics(self.knowledge, data))
            return {"config": copy.deepcopy(config), "topics": topics,
                    "stamp": _sha(_json(data).encode()), "generation": self._generation}
        finally:
            self.project.files_lock.release()

    def _current(self, snapshot):
        now = self._snapshot()
        if now["stamp"] != snapshot["stamp"] or now["generation"] != snapshot["generation"]:
            raise _Obsolete

    def _call(self, snapshot, method, *args):
        self._current(snapshot)
        def allowed():
            try:
                self._current(snapshot)
                return True
            except (_Obsolete, OSError, ValueError):
                return False
        result = getattr(self._client(allowed), method)(*args)
        self._current(snapshot)
        return result

    @staticmethod
    def _repo(row):
        if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1
                or not isinstance(row.get("full_name"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", row["full_name"])
                or row.get("html_url") != "https://github.com/" + row["full_name"]):
            raise Refused("GitHub returned an invalid repository identity.")
        return {key: row[key] for key in ("id", "full_name", "html_url")}

    @staticmethod
    def _issue(row, full, number=None):
        if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1
                or type(row.get("number")) is not int or row["number"] < 1
                or number is not None and row["number"] != number
                or row.get("html_url") != f"https://github.com/{full}/issues/{row['number']}"
                or not isinstance(row.get("body"), str) or row.get("pull_request")):
            raise Refused("GitHub returned an invalid topic issue.")
        return row

    @staticmethod
    def _comment(row, full, number):
        if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1
                or not isinstance(row.get("body"), str) or not isinstance(row.get("user"), dict)
                or type(row["user"].get("id")) is not int or not isinstance(row["user"].get("login"), str)
                or row.get("issue_url") != f"https://api.github.com/repos/{full}/issues/{number}"
                or row.get("html_url") != f"https://github.com/{full}/issues/{number}#issuecomment-{row['id']}"):
            raise Refused("GitHub returned an invalid discussion comment.")
        return row

    def configure(self, mode, approvers, expected_repository=None):
        if mode not in ("markdown", "github") or not isinstance(approvers, list) or len(approvers) > 100:
            raise Refused("Choose Markdown or GitHub discussions and a bounded list of accounts.")
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", name) for name in approvers):
            raise Refused("Use GitHub account names without spaces or URL syntax.")
        approvers = sorted(set(name.lower() for name in approvers))
        if self._closed or self.project.turn.busy():
            raise Conflict("Wait until this project's current Turn finishes before changing discussion settings.")
        bound = self._bound() if mode == "github" else None
        if mode == "github" and expected_repository is not None and expected_repository != bound:
            raise Conflict("The project's repository changed; refresh its discussion settings.")
        def allowed():
            try:
                return not self._closed and not self.project.turn.busy() and self.project.root == self._root and bound == self._bound()
            except (OSError, ValueError):
                return False
        repository = self._repo(self._client(allowed).repository(bound)) if mode == "github" else None
        if self._closed or self.project.turn.busy() or mode == "github" and bound != self._bound():
            raise Conflict("The project changed while its discussion settings were checked.")
        if not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise Conflict("The project files are busy; try changing discussion settings again.")
        changed = False
        try:
            data = self.knowledge._load()
            if not data:
                raise Refused("Initialize the research baseline before configuring discussions.")
            old = data["discussion"].get("github", {})
            config = {"repository": repository if mode == "github" else old.get("repository"),
                      "bound_repository": bound if mode == "github" else old.get("bound_repository"),
                      "approvers": approvers,
                      "project_id": old.get("project_id") if re.fullmatch(r"[0-9a-f]{32}", str(old.get("project_id", ""))) else uuid.uuid4().hex}
            if (data["discussion"].get("mode") == mode and all(old.get(key) == value for key, value in config.items())
                    and self._witnessed(old, mode)):
                return self.status()
            config["receipt"] = sign_receipt({"id": uuid.uuid4().hex, "project_key": self._key,
                "kind": "discussion_configuration", "configuration": {"mode": mode, **copy.deepcopy(config)}})
            self._generation += 1
            data["discussion"]["github"] = config
            data["discussion"]["mode"] = mode
            self._consent(config["receipt"])
            self.knowledge._write(data)
            changed = True
        finally:
            self.project.files_lock.release()
            if changed:
                self.project.on_change()
        self._next, self._error = 0.0, None
        self.wake()
        return self.status()

    def _topic_marker(self, snapshot, topic):
        reference = research_topics.publication_reference(self.knowledge, topic)
        project_id = snapshot["config"]["project_id"]
        if reference is not None:
            if (reference["repository"] != snapshot["config"]["repository"]
                    or reference["topic_id"] != topic["id"]):
                raise Refused("The received topic's GitHub repository differs from the confirmed binding.")
            project_id = reference["project_id"]
        return f"<!-- prism-topic:{project_id}:{topic['id']} -->", reference is not None

    def _body(self, snapshot, topic, *, marker=None):
        research_topics._path(self.knowledge, topic)
        raw = _read_plain(self._root, topic["path"], research_topics.MAX_QUEUE)
        if _sha(raw) != topic["sha256"]:
            raise Refused("The local topic changed; its received text was kept.")
        text = raw.decode("utf-8")
        ResearchAccess(self.knowledge, refresh=False).review_output(text, channel="GitHub topic")
        if marker is None:
            marker, _ = self._topic_marker(snapshot, topic)
        update = f"<!-- prism-update:{topic['revision']}:{topic['sha256']} -->"
        return marker, marker + "\n" + update + "\n\n" + text

    def share_proposals(self, topic_id, subjects):
        try:
            approvals.share(self, topic_id, subjects)
        except _Obsolete as error:
            raise Conflict("The project changed while sharing proposals. Refresh its page and try again.") from error

    @staticmethod
    def _cache_comments(record, comments):
        versions = record.setdefault("comment_versions", {})
        for row in comments:
            history = versions.setdefault(str(row["id"]), [])
            if not history or history[-1] != row:
                history.append(copy.deepcopy(row))
        record["comments"] = {str(row["id"]): row for row in comments}

    def _deliver(self, snapshot, state, record, topic):
        full = snapshot["config"]["repository"]["full_name"]
        marker, received = self._topic_marker(snapshot, topic)
        marker, body = self._body(snapshot, topic, marker=marker)
        intent = record.get("intent")
        issue = record.get("issue")
        if issue is None:
            rows = self._call(snapshot, "list_issues", full)
            if not isinstance(rows, list):
                raise Refused("GitHub returned an invalid topic list.")
            matches = [self._issue(row, full) for row in rows
                       if isinstance(row, dict) and str(row.get("body", "")).startswith(marker + "\n") and not row.get("pull_request")]
            if len(matches) > 1:
                raise Refused("Multiple GitHub issues claim this research topic; publication is paused.")
            if matches:
                issue = matches[0]
                record["issue"] = {key: issue[key] for key in ("id", "number", "html_url")}
            elif received:
                raise Refused("The received topic's GitHub issue was not found; publication will retry without creating another issue.")
            elif intent and intent.get("kind") == "issue" and intent.get("uncertain"):
                return
        if issue is not None:
            expected_id = issue["id"]
            issue = self._issue(self._call(snapshot, "get_issue", full, issue["number"]), full, issue["number"])
            if issue["id"] != expected_id or not issue["body"].startswith(marker + "\n"):
                raise Refused("The linked issue no longer identifies this topic; local notes remain saved.")
            record["issue"] = {key: issue[key] for key in ("id", "number", "html_url")}
        comments = self._call(snapshot, "list_comments", full, issue["number"]) if issue is not None else []
        comments = [self._comment(row, full, issue["number"]) for row in comments]
        if issue is not None:
            self._cache_comments(record, comments)
        if intent and issue is not None:
            recovered = issue["body"] == intent["body"] or any(row["body"] == intent["body"] for row in comments)
            if recovered:
                record.update(published_revision=intent["revision"], published_sha256=intent["sha256"])
                record.setdefault("delivered_bodies", []).append(_sha(intent["body"].encode()))
            # Existence of the uniquely marked issue settles issue identity even
            # if a coauthor edited its body before the unknown response recovered.
            if recovered or intent["kind"] == "issue":
                record.pop("intent", None)
                intent = None
        delivered = issue is not None and (issue["body"] == body or any(row["body"] == body for row in comments))
        if delivered:
            record.update(published_revision=topic["revision"], published_sha256=topic["sha256"])
            record["delivered_bodies"] = sorted(set(record.get("delivered_bodies", [])) | {_sha(body.encode())})
            record.pop("intent", None)
        elif record.get("published_sha256") != topic["sha256"]:
            if intent and intent.get("uncertain"):
                self._write(state)
                return
            record["intent"] = {"kind": "issue" if issue is None else "comment", "revision": topic["revision"],
                                "sha256": topic["sha256"], "body": body, "uncertain": True}
            self._current(snapshot)
            self._write(state)
            try:
                if issue is None:
                    result = self._call(snapshot, "create_issue", full, topic["title"], body)
                    issue = self._issue(result, full)
                    if issue["body"] != body:
                        raise Refused("GitHub did not confirm the sent topic content.")
                    record["issue"] = {key: issue[key] for key in ("id", "number", "html_url")}
                else:
                    result = self._comment(self._call(snapshot, "create_comment", full, issue["number"], body), full, issue["number"])
                    if result["body"] != body:
                        raise Refused("GitHub did not confirm the sent discussion content.")
                    comments.append(result)
            except _Obsolete:
                raise
            except Exception as error:
                record["intent"]["uncertain"] = bool(getattr(error, "uncertain", True))
                self._current(snapshot)
                self._write(state)
                raise
            record.update(published_revision=topic["revision"], published_sha256=topic["sha256"])
            record["delivered_bodies"] = sorted(set(record.get("delivered_bodies", [])) | {_sha(body.encode())})
            record.pop("intent", None)
        if issue is not None:
            self._cache_comments(record, comments)
        self._current(snapshot)
        self._write(state)

    def _comment_views(self, config, repository):
        access, mirror = ResearchAccess(self.knowledge, refresh=False), []
        full = config["repository"]["full_name"]
        for topic_id, record in repository["topics"].items():
            for row in record.get("comments", {}).values():
                self._comment(row, full, record["issue"]["number"])
                if _sha(row["body"].encode()) in record.get("delivered_bodies", []):
                    continue
                preview = access.review_auxiliary(row["body"], channel="external GitHub discussion")
                author = {"id": row["user"]["id"], "login": access.review_auxiliary(row["user"]["login"])[:100],
                          "type": row["user"].get("type") if row["user"].get("type") in ("User", "Bot", "Organization") else "Unknown"}
                mirror.append({"id": row["id"], "topic_id": topic_id, "repository_id": config["repository"]["id"],
                    "issue_number": record["issue"]["number"], "author": author,
                    "preview": preview[:4000], "url": row["html_url"], "updated_at": row.get("updated_at", ""),
                    "body_sha256": _sha(row["body"].encode()), "approval": False,
                    "source": "external GitHub discussion; not verified approval"})
        mirror.sort(key=lambda row: (row["topic_id"], row["id"]))
        return mirror

    def _mirror(self, snapshot, repository):
        mirror = self._comment_views(snapshot["config"], repository)
        self._current(snapshot)
        if not self.project.files_lock.acquire(timeout=SAVE_WAIT):
            raise Conflict("The project files are busy; discussion changes will retry later.")
        changed = False
        try:
            self._current(snapshot)
            data = self.knowledge._load()
            if data["discussion"].get("external_comments", []) != mirror:
                data["discussion"]["external_comments"] = mirror
                self.knowledge._write(data)
                changed = True
        finally:
            self.project.files_lock.release()
        if changed:
            self.project.on_change()

    def tick(self):
        opened = not self._closed and self.is_open()
        if not opened:
            self._was_open = False
            return
        local = None
        try:
            snapshot = self._snapshot()
            local = snapshot["stamp"]
            if self._was_open and self.clock() < self._next and local == self._local:
                return
            if not self._lock.acquire(blocking=False):
                return
            try:
                with registry.FileLock(self._path().with_suffix(".lock"), timeout=1):
                    snapshot = self._snapshot()
                    full = snapshot["config"]["repository"]["full_name"]
                    repo = self._repo(self._call(snapshot, "repository", full))
                    if repo["id"] != snapshot["config"]["repository"]["id"]:
                        raise Refused("The GitHub repository identity changed; confirm settings again.")
                    state = self._read()
                    repository = state["repositories"].setdefault(str(repo["id"]), {"topics": {}})
                    for topic in snapshot["topics"]:
                        record = repository["topics"].setdefault(topic["id"], {})
                        self._deliver(snapshot, state, record, topic)
                        approvals.deliver(self, snapshot, state, record, topic)
                        if approvals.ingest(self, snapshot, state, record, topic):
                            self._local, self._error = None, None
                            self.wake()
                            return
                    self._mirror(snapshot, repository)
                    self._error = None
            finally:
                self._lock.release()
            self._was_open, self._local, self._next = True, local, self.clock() + self.interval
        except _Obsolete:
            return
        except Exception:
            self._error = ERROR
            self._was_open, self._next = True, self.clock() + self.interval
            if local is not None:
                self._local = local

    def status(self):
        data = self.knowledge._load()
        discussion = (data or {}).get("discussion", {})
        mode, config = discussion.get("mode", "markdown"), discussion.get("github", {})
        config = config if isinstance(config, dict) else {}
        try:
            repository_view = self._repo(config.get("repository"))
        except (TypeError, ValueError, KeyError):
            repository_view = None
        approvers = config.get("approvers", [])
        approvers = approvers if isinstance(approvers, list) and all(isinstance(name, str) for name in approvers) else []
        result = {"mode": mode, "repository": repository_view, "approvers": approvers,
                  "connection": "gh", "bound_repository": None, "topics": [], "comments": [], "proposal_batches": [],
                  "decision_ids": [],
                  "state": "off" if mode != "github" else "waiting", "error": self._error}
        try:
            result["bound_repository"] = self._bound()
        except (OSError, ValueError):
            pass
        if not data or mode != "github":
            return result
        try:
            self._configuration(data)
            state = self._read()
            repository = state["repositories"].get(str(config["repository"]["id"]), {"topics": {}})
            access = ResearchAccess(self.knowledge, refresh=False)
            for topic in research_topics._topics(self.knowledge, data):
                record = repository["topics"].get(topic["id"], {})
                if record.get("issue"):
                    self._issue({**record["issue"], "body": ""}, config["repository"]["full_name"])
                publication = "unknown" if record.get("intent", {}).get("uncertain") else (
                    "published" if record.get("published_sha256") == topic["sha256"] else "pending")
                result["topics"].append({"id": topic["id"], "title": access.review_auxiliary(topic["title"]),
                    "path": topic["path"], "local_revision": topic["revision"],
                    "published_revision": record.get("published_revision", 0), "publication": publication,
                    **({"issue_url": record["issue"]["html_url"]} if record.get("issue") else {})})
            result["comments"] = self._comment_views(config, repository)
            result["proposal_batches"] = approvals.views(self, config, repository, data)
            result["decision_ids"] = [row["id"] for row in self.knowledge._report(data).get("github_decisions", [])]
            if self._closed or not self.is_open():
                result["state"] = "paused"
            elif any(row["publication"] == "unknown" for row in [*result["topics"], *result["proposal_batches"]]):
                result.update(state="unknown", error=UNKNOWN)
            elif self._error:
                result["state"] = "error"
            elif all(row["publication"] == "published" for row in result["topics"]):
                result["state"] = "ok"
        except (OSError, ValueError, TypeError, KeyError):
            result.update(state="error", error=ERROR, topics=[], comments=[])
        return result

    def wake(self):
        self._wake.set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._closed = False
        self._stop.clear()
        self._generation += 1
        def run():
            while not self._stop.is_set():
                self.tick()
                self._wake.wait(1)
                self._wake.clear()
        self._thread = threading.Thread(target=run, name="research-github", daemon=True)
        self._thread.start()

    def close(self):
        self._closed = True
        self._generation += 1
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=25)
