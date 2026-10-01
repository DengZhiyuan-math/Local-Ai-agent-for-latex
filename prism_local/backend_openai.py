"""OpenAI-compatible API backend (DeepSeek, OpenAI, OpenRouter, Qwen, Kimi, Ollama, vLLM …).

There is no local agent CLI here, so prism-local runs the agent loop itself: it sends
the conversation to ``{base_url}/chat/completions`` with streaming and function
calling, runs the file tools the model asks for, sends back the results, and repeats
until the model answers without a tool call.

The tools are deliberately small: list_files, read_file and search in both modes;
write_file and edit_file only in edit mode, and only on files the turn may change
(the @-mentioned files, else any editable project file). There is no shell.

The API key comes from the environment variable named by ``api_key_env``. Sessions
live in memory and end when the server stops.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import mcp_compile
from backends import SYSTEM_APPEND, Backend, Job
from fsutil import with_line_ends_of, write_bytes

MAX_READ = 200_000        # characters of one read_file result
MAX_HITS = 200            # search results
MAX_SESSIONS = 20
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
TOOL_LABELS = {"list_files": "Glob", "read_file": "Read", "search": "Grep",
               "write_file": "Write", "edit_file": "Edit", "compile": "Compile"}


class ToolError(Exception):
    pass


class ApiError(Exception):
    pass


class OpenAICompat(Backend):
    kind = "openai"
    label = "OpenAI-compatible API"

    def __init__(self, pid: str, spec: dict | None = None):
        super().__init__(pid, spec)
        spec = spec or {}
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

    def unavailable(self) -> str | None:
        if self.api_key_env and not os.environ.get(self.api_key_env):
            return f"Set the environment variable {self.api_key_env} to use {self.label}."
        return None

    def check(self, model: str | None, effort: str | None) -> str | None:
        if not (model or self.default_model):
            return f"Choose a model for {self.label} first: /model <name>."
        return super().check(model, effort)

    def info(self) -> dict:
        return {**super().info(), "base_url": self.base_url, "api_key_env": self.api_key_env}

    # ------------------------------------------------------------ the loop
    def run(self, job: Job) -> dict:
        model = job.model or self.default_model
        saved = self.sessions.get(job.session_id or "")
        if saved is None:
            if job.session_id:
                job.emit({"t": "error", "message": "The earlier conversation ended when the "
                          "server restarted; this message starts a new one."})
            sid = uuid.uuid4().hex
            msgs = [{"role": "system", "content": self.system_prompt(job.root)}]
        else:
            sid, msgs = job.session_id, list(saved)
        job.emit({"t": "init", "session_id": sid, "model": model})
        msgs.append({"role": "user", "content": job.prompt})
        tools = READ_TOOLS + (WRITE_TOOLS if job.mode == "edit" else [])
        if job.mode == "edit" and job.server_url:
            tools = tools + [COMPILE_TOOL]
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
                    res.update(is_error=True, exit=1, subtype="API error", stderr=str(e))
                break
            usage["in"] += u.get("prompt_tokens") or 0
            usage["out"] += u.get("completion_tokens") or 0
            msg: dict = {"role": "assistant", "content": text if text or not calls else None}
            if calls:
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": c["arguments"]}}
                                     for c in calls]
                # DeepSeek's thinking mode wants its reasoning back while it is still
                # calling tools for the same message (and not in later turns).
                if reasoning:
                    msg["reasoning_content"] = reasoning
            msgs.append(msg)
            if not calls:
                break
            for c in calls:
                msgs.append({"role": "tool", "tool_call_id": c["id"],
                             "content": self._call(job, c, denials)})
        else:
            res.update(is_error=True, subtype=f"stopped after {self.max_steps} steps")
        for m in msgs:
            m.pop("reasoning_content", None)
        if res.get("subtype") != "API error":     # keep the conversation consistent
            self.sessions.pop(sid, None)
            self.sessions[sid] = msgs
            while len(self.sessions) > MAX_SESSIONS:
                self.sessions.pop(next(iter(self.sessions)))
        return res

    def system_prompt(self, root: Path) -> str:
        parts = [SYSTEM_APPEND, TOOL_PROMPT]
        for name in PROJECT_RULES:
            p = root / name
            if p.is_file():
                try:
                    parts.append(f"[{name} of this project]\n"
                                 + p.read_text(encoding="utf-8", errors="replace")[:20000])
                except OSError:
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
        calls: dict[int, dict] = {}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                job.http = resp
                for raw in resp:
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
                        raise ApiError(json.dumps(d["error"])[:2000])
                    usage = d.get("usage") or usage
                    for ch in d.get("choices") or []:
                        delta = ch.get("delta") or {}
                        if delta.get("content"):
                            text.append(delta["content"])
                            job.emit({"t": "delta", "text": delta["content"]})
                        if delta.get("reasoning_content"):
                            reasoning.append(delta["reasoning_content"])
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
            raise ApiError(f"HTTP {e.code} from {self.base_url}: {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(f"{type(e).__name__}: {e}") from None
        finally:
            job.http = None
        out = []
        for i in sorted(calls):
            c = calls[i]
            c["id"] = c["id"] or f"call_{uuid.uuid4().hex[:12]}"
            out.append(c)
        return "".join(text), out, "".join(reasoning), usage

    # ------------------------------------------------------------ tools
    def _call(self, job: Job, c: dict, denials: list[str]) -> str:
        name = c["name"]
        try:
            args = json.loads(c["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            args = None
        summary = (args or {}).get("path") or (args or {}).get("pattern") or ""
        job.emit({"t": "tool", "id": c["id"], "name": TOOL_LABELS.get(name, name),
                  "summary": str(summary)[:200]})
        try:
            if args is None:
                raise ToolError("the arguments are not a JSON object")
            out = self.tool(job, name, args, denials)
            err = False
        except ToolError as e:
            out, err = f"Error: {e}", True
        except (KeyError, TypeError) as e:
            out, err = f"Error: missing or bad argument {e}", True
        job.emit({"t": "tool_result", "id": c["id"], "error": err, "preview": out[:300]})
        return out

    @staticmethod
    def _path(job: Job, rel: str) -> tuple[str, Path]:
        rel = str(rel).replace("\\", "/")
        if rel.startswith("/") or re.match(r"[A-Za-z]:", rel):
            raise ToolError("give a path relative to the project root")
        root = job.root.resolve()
        p = (root / rel).resolve()
        if p != root and root not in p.parents:
            raise ToolError("the path is outside the project")
        r = p.relative_to(root).as_posix()
        if r == ".git" or r.startswith(".git/"):
            raise ToolError("the .git directory is off limits")
        return r, p

    def tool(self, job: Job, name: str, args: dict, denials: list[str]) -> str:
        if name == "compile":
            if job.mode != "edit" or not job.server_url:
                raise ToolError("compiling is not available in this turn")
            try:
                r = mcp_compile.build(job.server_url, bool(args.get("clean")))
            except (OSError, ValueError) as e:
                raise ToolError(f"could not compile: {e}") from None
            return mcp_compile.report(r)[0]
        if name == "list_files":
            return "\n".join(job.files()) or "(no editable files)"
        if name == "read_file":
            rel, p = self._path(job, args["path"])
            if not p.is_file():
                raise ToolError(f"no such file: {rel}")
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(1, int(args.get("offset") or 1))
            end = len(lines) if not args.get("limit") else start - 1 + int(args["limit"])
            body = "\n".join(f"{i:6}\t{lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1))
            if len(body) > MAX_READ:
                body = body[:MAX_READ] + "\n[… cut; read a smaller range with offset/limit]"
            return body or "(empty file)"
        if name == "search":
            try:
                rx = re.compile(args["pattern"], re.I if args.get("ignore_case") else 0)
            except re.error as e:
                raise ToolError(f"bad regular expression: {e}") from None
            under = str(args.get("path") or "").strip("/")
            hits = []
            for rel in job.files():
                if under and rel != under and not rel.startswith(under + "/"):
                    continue
                try:
                    text = (job.root / rel).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{rel}:{n}: {line[:300]}")
                        if len(hits) >= MAX_HITS:
                            return "\n".join(hits) + "\n[… more matches]"
            return "\n".join(hits) or "no matches"
        if name in ("write_file", "edit_file"):
            if job.mode != "edit":
                raise ToolError("this is a read-only (Ask) turn")
            rel, p = self._path(job, args["path"])
            if not job.writable(rel):
                denials.append(TOOL_LABELS[name])
                raise ToolError(f"you may not change {rel} in this turn"
                                + (f"; only {', '.join(job.scope)}" if job.scope else ""))
            if name == "write_file":
                new = with_line_ends_of(str(args["content"]), p)     # an existing file keeps its line ends
            else:
                if not p.is_file():
                    raise ToolError(f"no such file: {rel}")
                with open(p, encoding="utf-8", newline="") as f:
                    cur = f.read()
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
            write_bytes(p, new.encode("utf-8"))
            return f"{'Wrote' if name == 'write_file' else 'Edited'} {rel}"
        raise ToolError(f"unknown tool {name}")
