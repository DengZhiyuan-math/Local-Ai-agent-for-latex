"""Web-tool approvals for a running CLI. The CLI still applies its own deny rules.

A grant is bound to the project, profile, provider and tool. Fetch grants name one
HTTP origin; search grants name WebSearch. Once grants are never cached. Session
grants last for the conversation in this server; persistent grants live in user
state, outside the project and its agent-editable settings.
"""
from __future__ import annotations

import ipaddress
import json
import threading
import weakref
from pathlib import Path
from urllib.parse import urlsplit

import registry


SCOPES = ("once", "session", "persistent")


def web_target(tool: str, inp: dict) -> str:
    if tool == "WebSearch":
        if not isinstance(inp.get("query"), str) or not inp["query"].strip():
            raise ValueError("WebSearch needs a query")
        return "WebSearch"
    if tool != "WebFetch":
        raise ValueError("Only WebSearch and WebFetch can be approved in this panel")
    url = inp.get("url")
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        raise ValueError("WebFetch needs an HTTP URL")
    try:
        p = urlsplit(url)
        host, port = p.hostname, p.port
    except ValueError as e:
        raise ValueError("Invalid WebFetch URL") from e
    if p.scheme not in ("http", "https") or not host or p.username or p.password:
        raise ValueError("WebFetch needs an HTTP URL without credentials")
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")) or \
            any(c in host for c in "*()%,\\"):
        raise ValueError("Local or malformed WebFetch host cannot be approved")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or not all(c.isalnum() or c in ".-" for c in host):
            raise ValueError("Invalid WebFetch host")
    else:
        if not addr.is_global:
            raise ValueError("Private WebFetch address cannot be approved")
    authority = f"[{host}]" if ":" in host else host
    if port and port != (443 if p.scheme == "https" else 80):
        authority += f":{port}"
    return f"{p.scheme}://{authority}"


class WebPermissions:
    def __init__(self):
        self.cond = threading.Condition()
        self.pending: dict[tuple[int, str], dict] = {}
        self.sessions: set[tuple[str, str, str, str]] = set()
        self.cancelled = weakref.WeakKeyDictionary()

    @staticmethod
    def path() -> Path:
        return registry.state_dir() / "web-permissions.json"

    def request(self, job, request_id: str, tool: str, inp: dict,
                session_id: str | None, namespace: str) -> dict:
        """Wait for the panel in a worker thread; the CLI reader remains available."""
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200 \
                or not isinstance(inp, dict):
            return {"behavior": "deny", "message": "Malformed permission request"}
        try:
            target = web_target(tool, inp)
            original = json.loads(json.dumps(inp))
        except (ValueError, TypeError) as e:
            return {"behavior": "deny", "message": str(e)}
        # Fresh conversations receive a CLI session id before tool use. If an older
        # CLI omits it, keep session grants confined to this one turn.
        session = session_id or job.session_id or f"turn:{job.id}"
        grant = (namespace, tool, target)
        key = (id(job), request_id)
        with self.cond:
            cancelled = self.cancelled.get(job, set())
            if job.cancel.is_set() or None in cancelled or request_id in cancelled:
                return {"behavior": "deny", "message": "Turn stopped"}
            try:
                grants = registry.read_state(self.path()).get("grants", [])
                persistent = isinstance(grants, list) and list(grant) in grants
            except registry.StateUnreadable:
                persistent = False                  # damaged state never grants access
            if persistent or (namespace, session, tool, target) in self.sessions:
                return {"behavior": "allow", "updatedInput": original}
            if key in self.pending:
                return {"behavior": "deny", "message": "Duplicate permission request id"}
            entry = {"job": job, "input": original, "grant": grant,
                     "session": session, "answer": None}
            self.pending[key] = entry
            job.emit({"t": "permission", "request_id": request_id, "tool": tool,
                      "input": original, "target": target, "scopes": list(SCOPES)})
            while entry["answer"] is None and not job.cancel.is_set() and not job.done:
                if job.proc is not None and job.proc.poll() is not None:
                    break
                self.cond.wait(.2)
            self.pending.pop(key, None)
            return entry["answer"] or {"behavior": "deny", "message": "Turn stopped"}

    def resolve(self, job, request_id: str, decision: str, scope: str = "once") -> dict:
        if decision not in ("allow", "deny") or scope not in SCOPES:
            return {"error": "Invalid permission decision or scope"}
        with self.cond:
            entry = self.pending.get((id(job), request_id))
            if not entry or entry["answer"] is not None or job.done or job.cancel.is_set():
                return {"error": "Permission request is no longer pending"}
            namespace, tool, target = entry["grant"]
            if decision == "allow" and scope == "persistent":
                try:
                    path = self.path()
                    with registry.FileLock(path.with_suffix(".lock"), timeout=10):
                        data = registry.read_state(path)
                        grants = data.setdefault("grants", [])
                        if not isinstance(grants, list):
                            raise ValueError("Saved permission grants are damaged")
                        if list(entry["grant"]) not in grants:
                            grants.append(list(entry["grant"]))
                        registry.write_json(path, data)
                except (OSError, ValueError, TimeoutError) as e:
                    return {"error": f"Could not save permission: {e}"}
            if decision == "allow" and scope == "session":
                self.sessions.add((namespace, entry["session"], tool, target))
            answer = {"behavior": decision}
            if decision == "allow":
                answer["updatedInput"] = entry["input"]
            else:
                answer["message"] = "The user denied this web operation"
            for (identity, rid), pending in self.pending.items():
                same_grant = pending["grant"] == entry["grant"] and (
                    scope == "persistent" or pending["session"] == entry["session"])
                if pending is entry or (decision == "allow" and scope != "once" and same_grant
                                        and pending["answer"] is None):
                    pending["answer"] = {**answer, "updatedInput": pending["input"]} \
                        if decision == "allow" else dict(answer)
                    pending["job"].emit({"t": "permission_resolved", "request_id": rid,
                                         "decision": decision, "scope": scope})
            self.cond.notify_all()
            return {"ok": True}

    def cancel(self, job, request_id: str | None = None) -> None:
        with self.cond:
            self.cancelled.setdefault(job, set()).add(request_id)
            for (identity, rid), entry in list(self.pending.items()):
                if identity == id(job) and (request_id is None or request_id == rid) \
                        and entry["answer"] is None:
                    entry["answer"] = {"behavior": "deny", "message": "Request cancelled"}
                    job.emit({"t": "permission_resolved", "request_id": rid,
                              "decision": "deny", "scope": "once"})
            self.cond.notify_all()
