"""OpenAI Codex CLI backend.

Each chat turn runs the local Codex CLI non-interactively in the repository:

    codex exec --json --skip-git-repo-check --sandbox <read-only|workspace-write>
               [-m <model>] [-c model_reasoning_effort=<level>] [resume <id>] -

The prompt comes on stdin ("-"); Codex prints one JSON event per line
(thread.started, item.started/completed, turn.completed, turn.failed, error).
Codex reads the project's AGENTS.md itself. It uses its own login (`codex login`)
or OPENAI_API_KEY; prism-local passes no key.

Codex has no per-file write permission, so it cannot keep to the @-mentioned files
by itself: the agent manager reverts its writes outside them after the turn.
"""
from __future__ import annotations

from pathlib import Path

from backends import FILE_TOOL_RULE, SYSTEM_APPEND, CliBackend, Job, find_bin

SANDBOX = {"edit": "workspace-write", "ask": "read-only"}

CODEX_FILE_TOOL_RULE = """\
- Use Codex's native tools. Read and search project files with read-only shell commands
  (exec_command or shell_command); edit files with apply_patch in Edit mode.
  Do not use MCP resource discovery to find local project files.
  Do not use the shell to change or delete files, or to compile.
"""


def codex_bin() -> str | None:
    home = Path.home()
    return find_bin("CODEX_BIN", "codex", (str(home / ".local/bin/codex"),
                                           str(home / ".npm-global/bin/codex"),
                                           "/usr/local/bin/codex", "/opt/homebrew/bin/codex"))


def _rel(p: str, root: Path) -> str:
    try:
        return Path(p).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return p


class Codex(CliBackend):
    kind = "codex"
    label = "Codex CLI"
    models: list[str] = []
    efforts = ("minimal", "low", "medium", "high", "xhigh")
    enforces_scope = False

    def __init__(self, pid: str, spec: dict | None = None):
        super().__init__(pid, spec)
        self.bin_override = (spec or {}).get("bin")

    def bin(self) -> str | None:
        return self.bin_override or codex_bin()

    def unavailable(self) -> str | None:
        return None if self.bin() else \
            "Codex CLI not found. Install it (npm i -g @openai/codex) or set CODEX_BIN=/path/to/codex."

    def info(self) -> dict:
        return {**super().info(), "bin": self.bin()}

    def command(self, job: Job) -> tuple[list[str], str]:
        cmd = [self.bin(), "exec", "--json", "--skip-git-repo-check",
               "--sandbox", SANDBOX.get(job.mode, "read-only")]
        if job.model:
            cmd += ["-m", job.model]
        if job.effort:
            cmd += ["-c", f"model_reasoning_effort={job.effort}"]
        if job.session_id:
            cmd += ["resume", job.session_id]
        # Codex has no append-system-prompt flag. Refresh the editor instructions on
        # every turn so resumed chats also replace an earlier Claude-only tool rule.
        instructions = SYSTEM_APPEND.replace(FILE_TOOL_RULE, CODEX_FILE_TOOL_RULE)
        prompt = ("[Instructions from the editor for this turn]\n"
                  "These instructions replace earlier editor instructions.\n"
                  f"{instructions}\n[Message]\n{job.prompt}")
        return cmd + ["-"], prompt

    def handle(self, d: dict, job: Job, st: dict) -> None:
        t = d.get("type")
        if t == "thread.started":
            st["session_id"] = d.get("thread_id")
            job.emit({"t": "init", "session_id": st["session_id"], "model": job.model})
        elif t in ("item.started", "item.completed"):
            self._item(d.get("item") or {}, t == "item.completed", job, st)
        elif t == "turn.completed":
            u = d.get("usage") or {}
            st["usage"] = {"in": u.get("input_tokens"), "out": u.get("output_tokens")}
        elif t == "turn.failed":
            st.update(is_error=True, subtype="turn failed")
            job.emit({"t": "error", "message": ((d.get("error") or {}).get("message")
                                                or "Codex turn failed")})
        elif t == "error":
            st["is_error"] = True
            job.emit({"t": "error", "message": d.get("message") or "Codex error"})

    def _item(self, item: dict, completed: bool, job: Job, st: dict) -> None:
        kind = item.get("type") or item.get("item_type")
        iid = str(item.get("id") or "")
        seen: set = st.setdefault("tools", set())

        def tool(name: str, summary: str) -> None:
            if iid not in seen:
                seen.add(iid)
                job.emit({"t": "tool", "id": iid, "name": name, "summary": summary[:200]})

        if kind == "agent_message" and completed and item.get("text"):
            job.emit({"t": "message_start"})
            job.emit({"t": "text", "text": item["text"]})
        elif kind == "command_execution":
            tool("Bash", item.get("command") or "")
            if completed:
                code = item.get("exit_code")
                job.emit({"t": "tool_result", "id": iid,
                          "error": item.get("status") == "failed" or code not in (0, None),
                          "preview": str(item.get("aggregated_output") or "")[:300]})
        elif kind == "file_change":
            paths = [_rel(c.get("path", ""), job.root) for c in item.get("changes") or []]
            tool("Edit", ", ".join(paths))
            if completed:
                job.emit({"t": "tool_result", "id": iid, "error": item.get("status") == "failed",
                          "preview": ""})
        elif kind == "mcp_tool_call":
            tool(f"{item.get('server', '')}.{item.get('tool', '')}", "")
            if completed:
                job.emit({"t": "tool_result", "id": iid, "error": item.get("status") == "failed",
                          "preview": ""})
        elif kind == "web_search":
            tool("WebSearch", item.get("query") or "")
            if completed:
                job.emit({"t": "tool_result", "id": iid, "error": False, "preview": ""})
        elif kind == "error" and completed:
            job.emit({"t": "error", "message": item.get("message") or "Codex error"})
