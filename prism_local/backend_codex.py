"""OpenAI Codex CLI backend.

Each chat turn runs the local Codex CLI non-interactively in the repository:

    codex exec --json --skip-git-repo-check -c default_permissions="prism-editor"
               [-m <model>] [-c model_reasoning_effort=<level>] [resume <id>] -

The prompt comes on stdin ("-"); Codex prints one JSON event per line
(thread.started, item.started/completed, turn.completed, turn.failed, error).
Codex reads the project's AGENTS.md itself. It uses its own login (`codex login`)
or OPENAI_API_KEY; prism-local passes no key.

Each turn supplies a named native permissions profile. Ask permits no project writes.
Edit permits scoped files, or the project tree with repository metadata read-only.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
from functools import lru_cache

from backends import FILE_TOOL_RULE, SYSTEM_APPEND, NO_WINDOW, CliBackend, Job, executable, find_bin
from fsutil import project_path
import mcp_compile
import sessionmeta

PROFILE = "prism-editor"


@lru_cache(maxsize=8)
def runtime_support(bin_path: str, stamp: int) -> str | None:
    try:
        version = subprocess.run([bin_path, "--version"], capture_output=True, text=True,
                                 timeout=10, **NO_WINDOW)
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version.stdout)
        if version.returncode or not match or tuple(map(int, match.groups())) < (0, 138, 0):
            return "This Codex CLI version lacks the required named permission profiles. Update Codex CLI."
        p = subprocess.run([bin_path, "exec", "--help"], capture_output=True, text=True,
                           timeout=10, **NO_WINDOW)
        if p.returncode or "--ignore-user-config" not in p.stdout:
            return "This Codex CLI version lacks isolated per-turn configuration. Update Codex CLI."
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"Could not check Codex CLI compatibility: {e}"
    return None

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
    def check_session(self, root: Path, session_id: str) -> str | None:
        if sessionmeta.valid_id(session_id):
            home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            for directory in (home / "sessions", home / "archived_sessions"):
                for path in directory.rglob(f"rollout-*-{session_id}.jsonl"):
                    if sessionmeta.matches(path, root, session_id):
                        return None
        return "Codex session does not belong to this project. Start a new chat."

    kind = "codex"
    label = "Codex CLI"
    models: list[str] = []
    efforts = ("minimal", "low", "medium", "high", "xhigh")
    enforces_scope = True
    input_types = ("text", "image")

    def __init__(self, pid: str, spec: dict | None = None):
        super().__init__(pid, spec)
        self.bin_override = (spec or {}).get("bin")

    def bin(self) -> str | None:
        return self.bin_override or codex_bin()

    def unavailable(self) -> str | None:
        return None if executable(self.bin()) else \
            "Codex CLI not found. Install it (npm i -g @openai/codex) or set CODEX_BIN=/path/to/codex."

    def info(self) -> dict:
        return {**super().info(), "bin": self.bin()}

    def preflight(self, root: Path) -> str | None:
        binary = shutil.which(self.bin()) or self.bin()
        try:
            why = runtime_support(binary, Path(binary).stat().st_mtime_ns)
        except OSError as e:
            return f"Could not check Codex CLI executable: {e}"
        if why:
            return why
        try:
            p = subprocess.run([binary, "login", "status"], capture_output=True, text=True,
                               timeout=10, **NO_WINDOW)
            if p.returncode:
                detail = (p.stdout + p.stderr).strip()
                if "not logged in" in detail.lower():
                    return "Codex CLI is not authenticated. Run codex login and try again."
                return f"Could not check Codex CLI authentication: {detail[-400:]}"
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"Could not check Codex CLI authentication: {e}"
        return None

    @staticmethod
    def permission_config(job: Job) -> list[str]:
        root = job.root.resolve()
        rules = {"/": "read"}
        if job.mode == "edit":
            if job.scope:
                rules.update({str(project_path(root, rel)): "write" for rel in job.scope})
            else:
                rules[str(root)] = "write"
            # Never grant writes through an external link or to runtime instructions.
            for name in (".git", ".codex", ".agents"):
                rules[str(root / name)] = "read"
        table = "{" + ",".join(f"{json.dumps(p)}={json.dumps(v)}" for p, v in rules.items()) + "}"
        return [f'default_permissions="{PROFILE}"',
                f"permissions.{PROFILE}.filesystem={table}",
                f"permissions.{PROFILE}.network.enabled=false"]

    def command(self, job: Job) -> tuple[list[str], str]:
        cmd = [self.bin(), "exec", "--json", "--skip-git-repo-check",
               "--ignore-user-config", "--ignore-rules"]
        for setting in self.permission_config(job) + ['approval_policy="never"',
                'web_search="disabled"', "features.apps=false", "mcp_servers={}"]:
            cmd += ["-c", setting]
        if job.mode == "edit" and job.server_url:
            cfg = mcp_compile.config(job.server_url)
            cmd += ["-c", f"mcp_servers.prism.command={json.dumps(cfg['command'])}",
                    "-c", f"mcp_servers.prism.args={json.dumps(cfg['args'])}",
                    "-c", "mcp_servers.prism.enabled_tools=[\"compile\"]",
                    "-c", 'mcp_servers.prism.tools.compile.approval_mode="approve"',
                    "-c", "mcp_servers.prism.tool_timeout_sec=1200",
                    "-c", "mcp_servers.prism.required=true"]
        model = job.model or self.default_model
        if model:
            cmd += ["-m", model]
        if job.effort:
            cmd += ["-c", f"model_reasoning_effort={job.effort}"]
        if job.session_id:
            cmd += ["resume", job.session_id]
        for rel in job.attachments:
            from backends import attachment_type
            path = project_path(job.root, rel)
            if attachment_type(path) == "image":
                cmd += ["--image", str(path)]
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
                result = item.get("result") or {}
                failed = item.get("status") == "failed" or bool(item.get("error")) or \
                    bool(result.get("isError") or result.get("is_error"))
                preview = "\n".join(c.get("text", "") for c in result.get("content", [])
                                    if isinstance(c, dict))
                job.emit({"t": "tool_result", "id": iid, "error": failed,
                          "preview": preview[:300]})
        elif kind == "web_search":
            tool("WebSearch", item.get("query") or "")
            if completed:
                job.emit({"t": "tool_result", "id": iid, "error": False, "preview": ""})
        elif kind == "error" and completed:
            job.emit({"t": "error", "message": item.get("message") or "Codex error"})
