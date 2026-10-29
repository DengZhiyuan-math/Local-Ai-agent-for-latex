"""OpenAI-compatible API backend (DeepSeek, OpenAI, OpenRouter, Qwen, Kimi, Ollama, vLLM …).

There is no local agent CLI here, so prism-local runs the agent loop itself: it sends
the conversation to ``{base_url}/chat/completions`` with streaming and function
calling, runs the file tools the model asks for, sends back the results, and repeats
until the model answers without a tool call.

The tools are deliberately small: list_files, read_file and search in both modes;
write_file and edit_file only in edit mode, and only on files the turn may change
(the @-mentioned files, else any editable project file). There is no shell.

The API key comes from the environment variable named by ``api_key_env``. Sessions
also persist in private local state, bound to the project and provider configuration.
"""
from __future__ import annotations

import http.client
import json
import base64
import mimetypes
import hashlib
import threading
import time
import os
import queue
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import mcp_compile
from backends import Backend, Job, attachment_type, file_place
from fsutil import project_path, with_line_ends_of, write_bytes
import research_access
from texutil import LATEX_SOURCE_SUFFIXES

MAX_READ = 200_000        # characters of one read_file result
MAX_HITS = 200            # search results
MAX_SESSIONS = 20
MAX_SESSION_BYTES = 32 * 1024 * 1024
# What APIs answer when the conversation outgrew the model's context (OpenAI, DeepSeek,
# Qwen, Moonshot, Ollama, vLLM say it in one of these ways).
TOO_LONG = re.compile(r"context.length|context_length|maximum context|context window|too many tokens"
                      r"|prompt is too long|reduce the length|exceeds? the (?:model'?s )?(?:max|context)", re.I)
PROJECT_RULES = ("CLAUDE.md", "AGENTS.md")

TOOL_PROMPT = """\
You work on the author's LaTeX project through these tools: list_files,
read_file and search, and in edit mode write_file, edit_file and compile.
Paths are relative to the project root. Read a file before you change it;
prefer edit_file with a short unique old_string over rewriting a whole file.
compile runs the editor's own build and returns the errors with file:line.
You cannot run commands.
"""


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


_PATH = {"type": "string", "description": "Path relative to the project root"}
READ_TOOLS = [
    _fn("list_files", "List the project's editable files (.tex, .bib, .sty, …).", {}, []),
    _fn("read_file", "Read a text file of the project, with line numbers.",
        {"path": _PATH,
         "offset": {"type": "integer", "description": "First line to read (1-based)"},
         "limit": {"type": "integer", "description": "Number of lines to read"}}, ["path"]),
    _fn("search", "Search the editable files with a regular expression.",
        {"pattern": {"type": "string"},
         "path": {"type": "string", "description": "Only files under this path"},
         "ignore_case": {"type": "boolean"}}, ["pattern"]),
]
WRITE_TOOLS = [
    _fn("write_file", "Create a file or replace its whole content.",
        {"path": _PATH, "content": {"type": "string"}}, ["path", "content"]),
    _fn("edit_file", "Replace old_string by new_string in a file. old_string must occur "
        "exactly once unless replace_all is true.",
        {"path": _PATH, "old_string": {"type": "string"}, "new_string": {"type": "string"},
         "replace_all": {"type": "boolean"}}, ["path", "old_string", "new_string"]),
]
COMPILE_TOOL = _fn("compile", mcp_compile.TOOL["description"],
                   mcp_compile.TOOL["inputSchema"]["properties"], [])
PROPOSE_TOOL = _fn("propose_notation", "Present one new notation as a pending candidate and end the Turn. "
                   "This never authorizes using it in the manuscript or a derivation.",
                   {"notation": {"type": "string", "description": "One complete proposed mathematical identifier"},
                    "definition": {"type": "string", "description": "Its intended definition"},
                    "scope": {"type": "string", "description": "Where the notation would be used"},
                    "reason": {"type": "string", "description": "Why this new notation is proposed"},
                    "source_expression": {"type": "string", "description": "Original expression, when it abbreviates one"}},
                   ["notation", "definition", "scope", "reason"])
TOPIC_TOOL = _fn("record_research_topic", "Automatically record meaningful research discussion on a continuing topic. "
                "The editor saves it after this Turn; this tool never starts research or adopts notation.",
                {"topic_id": {"type": "string", "description": "Reuse an existing topic ID when applicable"},
                 "title": {"type": "string"},
                 "entries": {"type": "array", "items": {"type": "object", "properties": {
                     "kind": {"type": "string", "enum": ["idea", "decision", "question"]},
                     "text": {"type": "string"},
                     "source": {"type": "string", "enum": ["human", "assistant"]},
                     "quote": {"type": "string", "description": "Exact original author text for a human source"}},
                     "required": ["kind", "text", "source"]}},
                 "related_topics": {"type": "array", "items": {"type": "string"}}},
                ["title", "entries"])
NOTATION_PROMPT = """[Prism notation protocol]
Use current project knowledge for every Turn. Reuse known symbols, including changes
of use, without requesting notation approval. Standard operators and layout are exempt.
Before introducing an unknown mathematical identifier, call propose_notation with its
definition, scope, reason and original expression when applicable. A proposal ends
this Turn. Keep the original expression until the editor records a human decision;
neither a model assertion nor the proposal itself authorizes use. Do not continue a
derivation or file edit with the candidate after proposing it.
"""
TOOL_LABELS = {"list_files": "Glob", "read_file": "Read", "search": "Grep",
               "write_file": "Write", "edit_file": "Edit", "compile": "Compile",
               "propose_notation": "Propose notation", "record_research_topic": "Record research topic"}


class ToolError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, message: str, summary="API error"):
        self.summary = summary
        super().__init__(message)


class OpenAICompat(Backend):
    kind = "openai"
    label = "OpenAI-compatible API"
    file_tool_rule = ""             # its file tools are the only tools it has (TOOL_PROMPT)

    def __init__(self, pid: str, spec: dict | None = None):
        super().__init__(pid, spec)
        spec = spec or {}
        if "pdf" in self.input_types:
            raise ValueError("API attachment transport supports text and image; PDF is not implemented")
        self.base_url = str(spec.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise ValueError("base_url is required")
        self.api_key_env = spec.get("api_key_env")
        self.headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
        self.max_steps = int(spec.get("max_steps") or 40)
        self.timeout = float(spec.get("timeout") or 300)
        if not self.default_model and self.models:
            self.default_model = self.models[0]
        self.sessions: dict[str, list[dict]] = {}
        self.session_projects: dict[str, str] = {}
        self._models_lock = threading.Lock()
        self._models_key = None
        self._models_at = 0.0
        self._models_error = None

    def model_catalog(self, root: Path, refresh: bool = False) -> dict:
        # Model discovery is metadata only. No prompt or completion is sent.
        key = hashlib.sha256(os.environ.get(self.api_key_env or "", "").encode()).hexdigest()
        with self._models_lock:
            if key != self._models_key:
                self._discovered_models = []
                self._models_at = 0.0
                self._models_key = key
            if not refresh and self._models_at and time.time() - self._models_at < 300:
                result = {"models": self._discovered_models, "source": "provider", "at": self._models_at,
                          "default_model": self.default_model}
                if self._models_error:
                    result.update(stale=True, error=self._models_error)
                return result
            headers = {"Accept": "application/json", **self.headers}
            if self.api_key_env:
                headers["Authorization"] = f"Bearer {os.environ.get(self.api_key_env, '')}"
            try:
                why = self.unavailable()
                if why:
                    raise ValueError(why)
                req = urllib.request.Request(self.base_url + "/models", headers=headers)
                with urllib.request.urlopen(req, timeout=min(self.timeout, 15)) as response:
                    data = json.load(response)
                entries = data.get("data", data.get("models")) if isinstance(data, dict) else None
                if not isinstance(entries, list):
                    raise ValueError("Provider did not return a model list")
                models = {}
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    mid = entry.get("id") or entry.get("slug")
                    if not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,119}", mid):
                        continue
                    levels = entry.get("supported_reasoning_efforts", entry.get("supportedReasoningEfforts"))
                    if isinstance(levels, list):
                        levels = [e.get("reasoningEffort") if isinstance(e, dict) else e for e in levels]
                        levels = [e for e in levels if isinstance(e, str)]
                    else:
                        # Standard /models has no effort metadata. Do not invent it.
                        levels = list(self.model_efforts.get(mid, self.efforts))
                    models[mid] = {"id": mid, "label": str(entry.get("display_name") or entry.get("name") or mid),
                                   "efforts": list(self.model_efforts.get(mid, levels))}
                self._discovered_models = list(models.values())
                self._models_at, self._models_error = time.time(), None
                return {"models": self._discovered_models, "source": "provider", "at": self._models_at,
                        "default_model": self.default_model}
            except (OSError, ValueError, urllib.error.URLError) as error:
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                self._models_error = f"Could not list models: {error}"
                fallback = self._discovered_models or super().model_catalog(root)["models"]
                return {"models": fallback, "source": "provider" if self._discovered_models else "configuration",
                        "at": self._models_at or None, "stale": True, "default_model": self.default_model,
                        "error": self._models_error}

    def check_session(self, root: Path, session_id: str) -> str | None:
        import registry
        if not isinstance(session_id, str):
            return "The API session is unavailable for this project. Start a new chat."
        if session_id not in self.sessions:
            try:
                path = self._session_path(root, session_id)
                if path and path.is_file() and path.stat().st_size <= MAX_SESSION_BYTES:
                    record = registry.read_state(path, tries=1)
                    messages = record.get("messages")
                    if (record.get("version") == 1 and record.get("session_id") == session_id
                            and record.get("project_key") == registry.project_key(root.resolve())
                            and record.get("provider_config") == self._session_config()
                            and isinstance(messages, list) and messages
                            and all(isinstance(m, dict) and m.get("role") in ("system", "user", "assistant", "tool")
                                    and isinstance(m.get("content"), (str, list, type(None)))
                                    for m in messages)):
                        self.sessions[session_id] = messages
                        self.session_projects[session_id] = record["project_key"]
                        self._trim_sessions()
            except (OSError, ValueError):
                pass
        if session_id not in self.sessions or self.session_projects.get(session_id) != registry.project_key(root.resolve()):
            return "The API session is unavailable for this project. Start a new chat."
        return None

    def _session_config(self) -> str:
        # Bind credentials without storing their values in a session record.
        fields = {"provider": self.id, "base_url": self.base_url, "api_key_env": self.api_key_env,
                  "credential": os.environ.get(self.api_key_env or "", ""), "headers": self.headers}
        return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _session_path(self, root: Path, session_id: str) -> Path | None:
        import registry
        if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id):
            return None
        folder = registry.state_dir() / "api-conversations"
        project = hashlib.sha256(registry.project_key(root.resolve()).encode()).hexdigest()
        path = folder / f"{self._session_config()}-{project}-{session_id}.json"
        if folder.is_symlink() or path.is_symlink():
            raise ValueError("API conversation state must not follow a symbolic link.")
        return path

    def _trim_sessions(self) -> None:
        while len(self.sessions) > MAX_SESSIONS:
            oldest = next(iter(self.sessions))
            self.sessions.pop(oldest)
            self.session_projects.pop(oldest, None)

    def _save_session(self, root: Path, session_id: str, messages: list[dict]) -> bool:
        import registry
        path = self._session_path(root, session_id)
        if path is None:
            return False     # legacy in-memory sessions may have another identity
        record = {"version": 1, "session_id": session_id, "project_key": registry.project_key(root.resolve()),
                  "provider_config": self._session_config(), "messages": messages}
        if len(json.dumps(record, ensure_ascii=False).encode()) > MAX_SESSION_BYTES:
            raise ValueError("The API conversation exceeds the local persistence limit.")
        with registry.FileLock(path.with_suffix(".lock"), timeout=3):
            registry.write_state(path, record)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        return True

    def unavailable(self) -> str | None:
        if self.api_key_env and not os.environ.get(self.api_key_env):
            return f"Set the environment variable {self.api_key_env} to use {self.label}."
        return None

    def check(self, model: str | None, effort: str | None) -> str | None:
        if not (model or self.default_model):
            return f"Choose a model for {self.label} first: /model <name>."
        return super().check(model, effort)

    def info(self) -> dict:
        return {**super().info(), "base_url": self.base_url, "api_key_env": self.api_key_env,
                "research_initialization": True,
                "notation_guard": {"application": True, "display": True, "proposals": True,
                                   "decisions": True}}

    # ------------------------------------------------------------ the loop
    def run(self, job: Job) -> dict:
        import registry
        if job.research_access is not None:
            job.controlled_cli = True   # all protected writes are owned by the parent gate
        model = job.model or self.default_model
        saved = self.sessions.get(job.session_id or "")   # checked by AgentManager.start (check_session)
        if saved is None:
            sid = uuid.uuid4().hex
            msgs = [{"role": "system", "content": self.system_prompt(job.root)}]
        else:
            sid, msgs = job.session_id, list(saved)
        if job.research_access is not None:
            # A restored Conversation must receive this Turn's current baseline.
            msgs = [m for m in msgs if not (m.get("role") == "system"
                    and str(m.get("content", "")).startswith(("[Prism research baseline]", "[Prism notation protocol]")))]
            msgs.append({"role": "system", "content": job.research_access.context()})
            msgs.append({"role": "system", "content": NOTATION_PROMPT})
        job.emit({"t": "init", "session_id": sid, "model": model})
        content = [{"type": "text", "text": job.prompt}]
        for rel in job.attachments:
            _, p = self._path(job, rel)
            if attachment_type(p) == "image":
                mime = mimetypes.guess_type(p.name)[0]
                encoded = base64.b64encode(p.read_bytes()).decode("ascii")
                content.append({"type": "text", "text": f"[Attached image: {rel}; {mime}]"})
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
            else:
                text = p.read_text(encoding="utf-8")
                content[0]["text"] += f"\n\n[Attached text: {rel}]\n{text}"
        msgs.append({"role": "user", "content": content if len(content) > 1 else content[0]["text"]})
        tools = READ_TOOLS + (WRITE_TOOLS if job.mode == "edit" else [])
        if job.may_compile:
            tools = tools + [COMPILE_TOOL]
        if job.research_access is not None:
            tools = tools + [PROPOSE_TOOL]
            if job.research_access.initialized:
                tools = tools + [TOPIC_TOOL]
        usage = {"in": 0, "out": 0}
        denials: list[str] = []
        res = {"session_id": sid, "exit": 0, "usage": usage, "denials": denials}
        for _ in range(self.max_steps):
            if job.cancel.is_set():
                res.update(is_error=True, subtype="stopped")
                break
            job.emit({"t": "message_start"})
            try:
                text, calls, reasoning, u = self._complete(job, model, msgs, tools)
            except ApiError as e:
                if job.cancel.is_set():
                    res.update(is_error=True, subtype="stopped")
                else:
                    detail = self._diagnostic(job, str(e), e.summary)
                    res.update(is_error=True, exit=1, subtype="API error", stderr=detail)
                    if TOO_LONG.search(str(e)):
                        res["error"] = "This conversation is now too long for the model: start a new chat."
                break
            usage["in"] += u.get("prompt_tokens") or 0
            usage["out"] += u.get("completion_tokens") or 0
            if job.cancel.is_set():
                # Stopped while it streamed: what it said is kept, the tools it asked for
                # do not run (the next message must not see a call left unanswered).
                if text:
                    msgs.append({"role": "assistant", "content": text})
                res.update(is_error=True, subtype="stopped")
                break
            # A model can put a write and a proposal in the same completion. The
            # proposal takes precedence over every other call, regardless of order.
            proposed = [c for c in calls if c["name"] == "propose_notation"] \
                if job.research_access is not None else []
            msg: dict = {"role": "assistant", "content": None if proposed else text if text or not calls else None}
            if calls:
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": c["arguments"]}}
                                     for c in calls]
                # DeepSeek's thinking mode wants its reasoning back while it is still
                # calling tools for the same message (and not in later turns).
                if reasoning:
                    msg["reasoning_content"] = reasoning
            msgs.append(msg)
            if proposed:
                stop = None
                try:
                    if len(proposed) == 1:
                        reply = self._call(job, proposed[0], denials)
                    else:
                        for c in proposed:
                            job.tool(c["id"], TOOL_LABELS["propose_notation"], "")
                        items = []
                        for c in proposed:
                            args = json.loads(c["arguments"] or "{}")
                            if not isinstance(args, dict):
                                raise ValueError("The notation arguments must be JSON objects.")
                            items.append({key: args[key] for key in
                                ("notation", "definition", "scope", "reason", "source_expression") if key in args})
                        proposals = job.research_access.propose_many(items)
                        raise research_access.NotationStop(proposals)
                except research_access.NotationStop as error:
                    stop = error
                    reply = "Notation proposal could not be saved." if error.error else "Notation proposal saved; awaiting a human decision."
                except (OSError, ValueError) as error:
                    stop = research_access.NotationStop(error=f"Notation proposals could not be saved: {error}")
                    reply = "Notation proposals could not be saved."
                if len(proposed) > 1:
                    for c in proposed:
                        job.tool_result(c["id"], bool(stop and stop.error), reply)
                for c in calls:
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": reply if c in proposed
                                 else "Not run: notation review takes precedence in this Turn."})
                if stop is not None:
                    self._notation_stop(job, res, stop)
                    break
                # Invalid or unnecessary proposals may be corrected by the model,
                # but their sibling write/compile calls have never been executed.
                continue
            try:
                if job.research_access is not None:
                    text = job.research_access.review_output(text, channel="chat")
                    reasoning_visible = job.research_access.review_auxiliary(reasoning, channel="thinking")
                    msg["content"] = text if text or not calls else None
                else:
                    reasoning_visible = ""       # unguarded responses already streamed
            except research_access.NotationStop as error:
                msg["content"] = "[Editor withheld an unapproved mathematical draft; see pending notation candidates.]"
                for c in calls:
                    msgs.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": "Not run: notation review ended this Turn."})
                self._notation_stop(job, res, error)
                break
            except (OSError, ValueError) as error:
                stop = research_access.NotationStop(error=f"Notation review failed: {error}")
                msg["content"] = "[Editor withheld a mathematical draft because notation review failed.]"
                for c in calls:
                    msgs.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": "Not run: notation review failed."})
                self._notation_stop(job, res, stop)
                break
            if not calls:
                self._publish_reviewed(job, text, reasoning_visible)
                break
            stopped_for_notation = False
            for index, c in enumerate(calls):
                try:
                    reply = "Not run: the user stopped the turn." if job.cancel.is_set() \
                        else self._call(job, c, denials)
                except research_access.NotationStop as error:
                    msg["content"] = "[Editor withheld an unapproved mathematical draft; see pending notation candidates.]"
                    msgs.append({"role": "tool", "tool_call_id": c["id"],
                                 "content": "Draft not applied: notation review ended this Turn."})
                    for skipped in calls[index + 1:]:
                        msgs.append({"role": "tool", "tool_call_id": skipped["id"],
                                     "content": "Not run: notation review ended this Turn."})
                    self._notation_stop(job, res, error)
                    stopped_for_notation = True
                    break
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": reply})
            if stopped_for_notation:
                break
            self._publish_reviewed(job, text, reasoning_visible)
        else:
            res.update(is_error=True, subtype=f"stopped after {self.max_steps} steps")
        for m in msgs:
            m.pop("reasoning_content", None)
        if res.get("subtype") != "API error":     # keep the conversation consistent
            self.sessions.pop(sid, None)
            self.sessions[sid] = msgs
            self.session_projects[sid] = registry.project_key(job.root.resolve())
            self._trim_sessions()
            try:
                res["session_persisted"] = self._save_session(job.root, sid, msgs)
            except (OSError, ValueError):
                res["session_persisted"] = False
                res["session_warning"] = "Conversation history could not be saved for restart; this session remains in memory."
                job.emit({"t": "error", "message": res["session_warning"]})
        return res

    @staticmethod
    def _publish_reviewed(job: Job, text: str, reasoning: str) -> None:
        if job.research_access is None or job.cancel.is_set():
            return
        if text:
            job.emit({"t": "delta", "text": text})
        if reasoning:
            job.emit({"t": "thinking", "text": reasoning})

    @staticmethod
    def _notation_stop(job: Job, res: dict, stop) -> None:
        if job.cancel.is_set():
            res.update(is_error=True, subtype="stopped")
            return
        if stop.error:
            job.emit({"t": "error", "message": OpenAICompat._diagnostic(job, str(stop.error),
                                                                      "Notation review failed")})
            res.update(is_error=True, exit=1, subtype="notation review failed")
            return
        for proposal in stop.proposals:
            if proposal["status"] == "pending":
                job.emit({"t": "notation_proposal", "proposal": proposal})
        res["notation_pending"] = True

    @staticmethod
    def _diagnostic(job: Job, text: str, summary: str) -> str:
        if job.research_access is None:
            return text
        try:
            checked = job.research_access.review_auxiliary(text, channel="error")
        except Exception:  # an error in its checker must not publish the unchecked diagnostic
            return summary + ". Error details could not be checked."
        return text if checked == text else summary + ". " + checked

    @staticmethod
    def _auxiliary(job: Job, text: str) -> str:
        if job.research_access is None:
            return text
        try:
            return job.research_access.review_auxiliary(text, channel="tool")
        except research_access.NotationStop:
            raise
        except (OSError, ValueError) as error:
            raise research_access.NotationStop(error=f"Notation preview review failed: {error}") from None

    @staticmethod
    def _source_preview(job: Job, source: str, preview: str, path: Path) -> str:
        if job.research_access is None:
            return preview
        try:
            return job.research_access.review_source_preview(source, preview,
                                                             latex=path.suffix.lower() in LATEX_SOURCE_SUFFIXES)
        except Exception:  # a source-reading preview cannot create an adoption request
            return "Mathematical preview withheld because its source could not be checked."

    def system_prompt(self, root: Path) -> str:
        parts = [self.system_text(), TOOL_PROMPT]
        for name in PROJECT_RULES:
            try:
                p = project_path(root, name)
                if p.is_file():
                    parts.append(f"[{name} of this project]\n"
                                 + p.read_text(encoding="utf-8", errors="replace")[:20000])
            except (OSError, ValueError, RuntimeError):
                pass
        return "\n".join(parts)

    def _complete(self, job: Job, model: str, msgs: list[dict], tools: list[dict]):
        """One streamed chat completion: (text, tool calls, reasoning, usage)."""
        body = {"model": model, "messages": msgs, "stream": True,
                "stream_options": {"include_usage": True}, "tools": tools}
        if job.effort:
            body["reasoning_effort"] = job.effort
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
                   **self.headers}
        if self.api_key_env:
            headers["Authorization"] = f"Bearer {os.environ.get(self.api_key_env, '')}"
        req = urllib.request.Request(self.base_url + "/chat/completions",
                                     data=json.dumps(body).encode(), headers=headers,
                                     method="POST")
        text, reasoning, usage = [], [], {}
        access = job.research_access
        guarded = access is not None
        calls: dict[int, dict] = {}
        try:
            for raw in self._stream(job, req):
                if job.cancel.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    d = json.loads(data)
                except ValueError:
                    continue
                if d.get("error"):
                    raise ApiError(json.dumps(d["error"])[:2000], "The API provider returned an error")
                usage = d.get("usage") or usage
                for ch in d.get("choices") or []:
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        text.append(delta["content"])
                        if not guarded:
                            job.emit({"t": "delta", "text": delta["content"]})
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                        if not guarded:
                            job.emit({"t": "thinking", "text": delta["reasoning_content"]})
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)),
                                                {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] = slot["name"] or fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read().decode("utf-8", "replace")[:2000]
            raise ApiError(f"HTTP {e.code} from {self.base_url}: {detail}",
                           f"HTTP {e.code} from the API provider") from None
        except (urllib.error.URLError, OSError, ValueError, AttributeError, http.client.HTTPException) as e:
            # AttributeError, HTTPException: the stream ended under the reader (Stop).
            raise ApiError(f"{type(e).__name__}: {e}", f"{type(e).__name__} from the API provider") from None
        finally:
            job.http = None
        out = []
        for i in sorted(calls):
            c = calls[i]
            c["id"] = c["id"] or f"call_{uuid.uuid4().hex[:12]}"
            out.append(c)
        answer, thought = "".join(text), "".join(reasoning)
        if guarded and job.cancel.is_set():
            # Nothing from an interrupted, unchecked draft has been published.
            answer, thought = "", ""
        return answer, out, thought, usage

    def _stream(self, job: Job, req):
        """The response's lines, read by a thread of its own, which hands them over. Stop
        then ends the turn at once: a read blocked in that thread cannot be woken on
        Windows (neither closing the response, which waits for the lock the read holds,
        nor shutting its socket down does), so it is left to end by itself, at its next
        data or the request's timeout, and closes the response then. What that thread
        raised (an HTTPError, the connection lost) is raised here."""
        lines: queue.Queue = queue.Queue()

        def pump():
            resp = None
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                job.http = resp
                for raw in resp:
                    lines.put(raw)
                    if job.cancel.is_set():
                        break
                lines.put(None)
            except BaseException as e:  # noqa: BLE001 — handed to the turn
                lines.put(e)
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:  # noqa: BLE001
                        pass

        threading.Thread(target=pump, daemon=True, name="api-stream").start()
        while True:
            try:
                raw = lines.get(timeout=0.2)
            except queue.Empty:
                if job.cancel.is_set():
                    return
                continue
            if raw is None:
                return
            if isinstance(raw, BaseException):
                raise raw
            yield raw

    # ------------------------------------------------------------ tools
    def _call(self, job: Job, c: dict, denials: list[str]) -> str:
        name = c["name"]
        try:
            args = json.loads(c["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            args = None
        summary = str((args or {}).get("path") or (args or {}).get("pattern") or "")
        summary = self._auxiliary(job, summary)
        path, lines = file_place(name, args or {})
        path = job.relative(path) if path else None
        if path is not None and self._auxiliary(job, path) != path:
            path, lines = None, None
        label = TOOL_LABELS.get(name, name)
        if self._auxiliary(job, label) != label:
            label = "Unknown tool"
        job.tool(c["id"], label, summary, path=path, lines=lines)
        # Raw source stays available to the provider. A cropped source preview
        # carries its separate check of the full source context to the UI.
        display = {}
        try:
            if args is None:
                raise ToolError("the arguments are not a JSON object")
            out = self.tool(job, name, args, denials, preview=display)
            err = False
        except research_access.NotationStop as error:
            job.tool_result(c["id"], bool(error.error),
                            "Notation review could not be saved." if error.error
                            else "Draft held for a notation proposal.")
            raise
        except ToolError as e:
            out, err = f"Error: {e}", True
        except (KeyError, TypeError) as e:
            out, err = f"Error: missing or bad argument {e}", True
        except Exception as e:  # noqa: BLE001 — a tool failing is the model's to hear, not the turn's end
            out, err = f"Error: {type(e).__name__}: {e}", True
        try:
            preview = self._auxiliary(job, display.get("text", out))
        except research_access.NotationStop:
            job.tool_result(c["id"], True, "Notation preview could not be checked.")
            raise
        job.tool_result(c["id"], err, preview)
        return out

    @staticmethod
    def _path(job: Job, rel: str) -> tuple[str, Path]:
        try:
            p = project_path(job.root, rel)
        except (ValueError, OSError, RuntimeError) as e:
            raise ToolError(str(e)) from None
        return p.relative_to(job.root.resolve()).as_posix(), p

    def tool(self, job: Job, name: str, args: dict, denials: list[str], *, preview=None) -> str:
        if (job.research_access is not None and name in ("read_file", "write_file", "edit_file")
                and isinstance(args.get("path"), str)):
            return self._source_tool(job, name, args, denials, preview=preview)
        return self._tool(job, name, args, denials, preview=preview)

    def _source_tool(self, job, name, args, denials, *, preview=None):
        """Keep whole-file replacements bound to this Turn's observed version."""
        import snapshot
        project = job.research_access.knowledge.project
        if not project.files_lock.acquire(timeout=3):
            raise ToolError("The source files are busy; read the file again before editing.")
        try:
            rel, path = self._path(job, args["path"])
            versions = getattr(job, "source_versions", None)
            if versions is None:
                versions = job.source_versions = {name: state.digest if state is not None else None
                                                  for name, state in job.before.items()}
            def state():
                try:
                    return snapshot.file_state(path, budget=0)
                except FileNotFoundError:
                    return None
            try:
                before_state = state()
            except OSError as error:
                raise ToolError("The source version could not be checked; read the file again.") from error
            before = before_state.digest if before_state is not None else None
            changed = before != versions.get(rel)
            if changed and name == "write_file":
                if before_state != job.controlled_writes.get(rel):
                    project.turn.server_wrote([rel])
                raise ToolError("The source changed since its observed version; read it again before replacing the whole file.")
            operation = {"expected": versions.get(rel)}
            out = self._tool(job, name, args, denials, preview=preview, source=operation)
            after_state = state()
            after = after_state.digest if after_state is not None else None
            observed = operation["digest"]
            if name == "read_file" and (before != after or observed != after):
                project.turn.server_wrote([rel])
                if preview is not None:
                    preview.pop("text", None)
                raise ToolError("The source changed while it was read; read the file again before editing.")
            if name == "read_file" and changed and before_state != job.controlled_writes.get(rel):
                project.turn.server_wrote([rel])
            # A precise edit uses current matching text, not an observed whole
            # file. It cannot authorize a later whole-file replacement by itself.
            if name in ("read_file", "write_file") or operation["input"] == operation["expected"]:
                versions[rel] = observed
            if name in ("write_file", "edit_file"):
                if observed != after:
                    job.controlled_writes.pop(rel, None)
                    project.turn.server_wrote([rel])
                    raise ToolError("The source changed after the draft was applied; read it again before editing.")
                job.controlled_writes[rel] = after_state
            return out
        finally:
            project.files_lock.release()

    def _tool(self, job: Job, name: str, args: dict, denials: list[str], *, preview=None, source=None) -> str:
        if name == "record_research_topic":
            if job.research_access is None or not job.research_access.initialized:
                raise ToolError("Initialize the research baseline before recording topic notes.")
            from research_topics import stage
            try:
                return json.dumps(stage(job.research_access, job, args), ensure_ascii=False)
            except research_access.NotationStop:
                raise
            except (OSError, ValueError) as error:
                raise ToolError(str(error)) from None
        if name == "propose_notation":
            if job.research_access is None:
                raise ToolError("notation proposals are unavailable without project knowledge")
            proposal_args = {key: args[key] for key in ("notation", "definition", "scope", "reason", "source_expression")
                             if key in args}
            try:
                proposal = job.research_access.propose(proposal_args)
            except research_access.NotationStop:
                raise
            except OSError as error:
                raise research_access.NotationStop(error=f"Notation proposal could not be saved: {error}") from None
            except ValueError as error:
                raise ToolError(str(error)) from None
            raise research_access.NotationStop(proposals=[proposal])
        if name == "compile":
            if not job.may_compile:
                raise ToolError("compiling is not available in this turn")
            try:
                r = job.research_access.guarded_compile(clean=bool(args.get("clean")), cancel=job.cancel) \
                    if job.research_access is not None \
                    else mcp_compile.build(job.server_url, bool(args.get("clean")))
            except research_access.NotationStop:
                raise
            except (OSError, ValueError) as e:
                raise ToolError(f"could not compile: {e}") from None
            text, failed = mcp_compile.report(r)
            if failed:
                raise ToolError(text)
            return text
        if name == "list_files":
            return "\n".join(job.files()) or "(no editable files)"
        if name == "read_file":
            rel, p = self._path(job, args["path"])
            if not p.is_file():
                raise ToolError(f"no such file: {rel}")
            try:
                raw = p.read_bytes()
                text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                if "\x00" in text:
                    raise UnicodeError
            except UnicodeError:
                raise ToolError(f"{rel} is binary; attach it with a provider that supports its input type") from None
            if source is not None:
                source["digest"] = hashlib.sha256(raw).hexdigest()
            lines = text.splitlines()
            start = max(1, int(args.get("offset") or 1))
            end = len(lines) if not args.get("limit") else start - 1 + int(args["limit"])
            body = "\n".join(f"{i:6}\t{lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1))
            if len(body) > MAX_READ:
                body = body[:MAX_READ] + "\n[… cut; read a smaller range with offset/limit]"
            out = body or "(empty file)"
            if preview is not None:
                preview["text"] = self._source_preview(job, text, out, p)
            return out
        if name == "search":
            try:
                rx = re.compile(args["pattern"], re.I if args.get("ignore_case") else 0)
            except re.error as e:
                raise ToolError(f"bad regular expression: {e}") from None
            under = str(args.get("path") or "").strip("/")
            hits, visible = [], []
            for rel in job.files():
                if under and rel != under and not rel.startswith(under + "/"):
                    continue
                try:
                    _, p = self._path(job, rel)
                    text = p.read_text(encoding="utf-8", errors="replace")
                except (OSError, ToolError):
                    continue
                file_hits = []
                for n, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        hit = f"{rel}:{n}: {line[:300]}"
                        hits.append(hit)
                        file_hits.append(hit)
                        if len(hits) >= MAX_HITS:
                            break
                if file_hits and preview is not None:
                    visible.append(self._source_preview(job, text, "\n".join(file_hits), p))
                if len(hits) >= MAX_HITS:
                    if preview is not None:
                        preview["text"] = "\n".join(visible) + "\n[… more matches]"
                    return "\n".join(hits) + "\n[… more matches]"
            if preview is not None:
                preview["text"] = "\n".join(visible) or "no matches"
            return "\n".join(hits) or "no matches"
        if name in ("write_file", "edit_file"):
            if job.mode != "edit":
                raise ToolError("this is a read-only (Ask) turn")
            rel, p = self._path(job, args["path"])
            if not job.writable(rel):
                denials.append(TOOL_LABELS[name])
                raise ToolError(f"you may not change {rel} in this turn"
                                + (f"; only {', '.join(job.scope)}" if job.scope else ""))
            expected = p.read_bytes() if p.is_file() else None
            if source is not None:
                source["input"] = hashlib.sha256(expected).hexdigest() if expected is not None else None
                if source["input"] != source["expected"]:
                    owned = job.controlled_writes.get(rel)
                    if owned is None or source["input"] != owned.digest:
                        job.research_access.knowledge.project.turn.server_wrote([rel])
                    if name == "write_file":
                        raise ToolError("The source changed since its observed version; read it again before replacing the whole file.")
            if name == "write_file":
                new = with_line_ends_of(str(args["content"]), p)     # an existing file keeps its line ends
            else:
                if not p.is_file():
                    raise ToolError(f"no such file: {rel}")
                cur = expected.decode("utf-8")
                old, rep = str(args["old_string"]), str(args["new_string"])
                if "\r\n" in cur:           # the model writes \n; keep the file's line ends
                    old, rep = (s.replace("\r\n", "\n").replace("\n", "\r\n") for s in (old, rep))
                n = cur.count(old) if old else 0
                if n == 0:
                    raise ToolError("old_string was not found; read the file again")
                if n > 1 and not args.get("replace_all"):
                    raise ToolError(f"old_string occurs {n} times; give more context "
                                    "or set replace_all")
                new = cur.replace(old, rep) if args.get("replace_all") else cur.replace(old, rep, 1)
            if job.research_access is not None:
                try:
                    if job.cancel.is_set():
                        raise ToolError("Not run: the user stopped the turn.")
                    job.research_access.apply_write(p, new, expected)
                except research_access.NotationStop:
                    raise
                except ValueError as e:
                    raise ToolError(str(e)) from None
            else:
                write_bytes(p, new.encode("utf-8"))
            if source is not None:
                source["digest"] = hashlib.sha256(new.encode("utf-8")).hexdigest()
            return f"{'Wrote' if name == 'write_file' else 'Edited'} {rel}"
        raise ToolError(f"unknown tool {name}")
