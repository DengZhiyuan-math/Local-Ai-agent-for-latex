"""OpenAI Codex CLI backend.

Research-controlled Turns use app-server with editor-owned checked tools and a
native read-only boundary. Original cwd, native Conversation and current Turn
scope are retained; only the parent editor can apply manuscript edits.

Without research access the legacy path runs `codex exec --json` with a named
native permission profile: Ask is read-only; Edit permits scoped files or the
project tree while repository metadata remains read-only. Codex uses its own
login; prism-local passes no credential.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import queue
import threading
import time
from functools import lru_cache

from backends import (CliBackend, Job, executable, find_bin, agent_env, start,
                      kill_tree, end_group)
from proc import run as run_bounded
from fsutil import METADATA, is_metadata, metadata_paths, project_path
import mcp_compile
import sessionmeta

PROFILE = "prism-editor"


@lru_cache(maxsize=8)
def runtime_support(bin_path: str, stamp: int) -> str | None:
    try:
        version = run_bounded([bin_path, "--version"], text=True, timeout=10)
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version.stdout)
        if version.returncode or not match or tuple(map(int, match.groups())) < (0, 138, 0):
            return "This Codex CLI version lacks the required named permission profiles. Update Codex CLI."
        for subcommand in (["exec"], ["exec", "resume"]):
            p = run_bounded([bin_path, *subcommand, "--help"], text=True, timeout=10)
            if p.returncode or any(flag not in p.stdout for flag in
                                   ("--ignore-user-config", "--ignore-rules")):
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
    # A GUI launcher often has an older PATH than the terminal. The Windows
    # desktop app bundles a native CLI in a versioned directory outside PATH.
    # Resolve it each time so app updates do not leave a pinned, missing binary.
    binary = find_bin("CODEX_BIN", "codex", (str(home / ".local/bin/codex"),
                                           str(home / ".local/bin/codex.exe"),
                                           str(home / ".npm-global/bin/codex"),
                                           "/usr/local/bin/codex", "/opt/homebrew/bin/codex"))
    if binary or os.name != "nt":
        return binary
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
    candidates = []
    try:
        for path in (local / "OpenAI/Codex/bin").glob("*/codex.exe"):
            try:
                if path.is_file():
                    candidates.append((path.stat().st_mtime_ns, str(path)))
            except OSError:
                continue  # An update may remove one version while we inspect it.
    except OSError:
        pass
    return max(candidates)[1] if candidates else None


def protected_scope(root: Path, rel: str) -> bool:
    """Whether an @-mentioned file is metadata, by the given name or its resolved target
    (an internal link). A specific file write would override a broader metadata read rule."""
    return is_metadata(Path(rel.replace("\\", "/"))) or \
        is_metadata(project_path(root, rel).relative_to(root))


def protected_paths(root: Path):
    """Protect existing nested metadata too.

    Exact subtree rules are portable; recursive read globs are not on all CLI builds.
    Re-scan each turn so metadata added between turns is also protected.
    """
    yield from (root / name for name in sorted(METADATA))

    def unreadable(error):
        # Do not silently grant writes to a subtree we could not inspect.
        raise OSError(f"Could not check {error.filename} for repository metadata, so this "
                      f"Edit turn did not start: {error.strerror or error}") from error

    yield from metadata_paths(root, unreadable)


def _rel(p: str, root: Path) -> str:
    try:
        return Path(p).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return p


class Codex(CliBackend):
    def check_scope(self, root: Path, scope: list[str]) -> str | None:
        blocked = [rel for rel in scope if protected_scope(root, rel)]
        if blocked:
            return (f"Codex cannot edit {', '.join(blocked)}: .git, .codex, .agents and .aws "
                    "stay read-only for the agent. Remove the mention and try again.")
        return None

    def check_session(self, root: Path, session_id: str) -> str | None:
        if sessionmeta.valid_id(session_id):
            home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            for directory in (home / "sessions", home / "archived_sessions"):
                for path in directory.rglob(f"rollout-*-{session_id}.jsonl"):
                    if sessionmeta.matches(path, root, session_id):
                        return None
        return "Codex session does not belong to this project. Start a new chat."

    kind = "codex"
    research_support = True
    label = "Codex CLI"
    models: list[str] = []
    efforts = ("minimal", "low", "medium", "high", "xhigh")
    enforces_scope = True
    input_types = ("text", "image")
    file_tool_rule = CODEX_FILE_TOOL_RULE

    def __init__(self, pid: str, spec: dict | None = None):
        super().__init__(pid, spec)
        self.bin_override = (spec or {}).get("bin")
        self._models_lock = threading.Lock()
        self._models_at = 0.0
        self._models_key = None
        self._models_error = None

    def model_catalog(self, root: Path, refresh: bool = False) -> dict:
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        def stamp(path):
            try:
                stat = path.stat()
                return stat.st_mtime_ns, stat.st_size
            except OSError:
                return None
        key = (self.bin(), str(root.resolve()), str(home), stamp(home / "auth.json"),
               stamp(home / "config.toml"))
        with self._models_lock:
            if key != self._models_key:
                self._models_at, self._models_key, self._discovered_models = 0.0, key, []
                self._catalog_default_model = None
            if refresh or not self._models_at or time.time() - self._models_at >= 300:
                try:
                    self._discovered_models = self._read_models(root)
                    self._catalog_default_model = next((m["id"] for m in self._discovered_models
                                                        if m.get("is_default")), None)
                    self._models_at, self._models_error = time.time(), None
                except (OSError, ValueError, queue.Empty, subprocess.SubprocessError) as error:
                    self._models_error = f"Could not list Codex models: {error}"
            result = {"models": self._discovered_models or super().model_catalog(root)["models"],
                      "source": "codex app-server", "at": self._models_at or None,
                      "default_model": self.default_model or self._catalog_default_model}
            if self._models_error:
                result.update(stale=True, error=self._models_error)
            return result

    def _read_models(self, root: Path) -> list[dict]:
        """Metadata RPC only; no thread is started and no inference request is made."""
        proc = start([self.bin(), "app-server", "--listen", "stdio://", "-c", "features.hooks=false",
                      "-c", "features.apps=false"], cwd=root, env=agent_env(root=root), stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        responses = queue.Queue()
        def read():
            try:
                for line in proc.stdout:
                    try:
                        responses.put(json.loads(line))
                    except ValueError:
                        pass
            finally:
                responses.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline, serial = time.monotonic() + 15, 0
        def send(message):
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        def request(method, params):
            nonlocal serial
            serial += 1
            send({"id": serial, "method": method, "params": params})
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ValueError("metadata request timed out")
                message = responses.get(timeout=left)
                if message is None:
                    raise ValueError("app-server ended before answering")
                if isinstance(message, dict) and message.get("id") == serial:
                    if message.get("error"):
                        raise ValueError(str(message["error"])[:300])
                    result = message.get("result") or {}
                    if not isinstance(result, dict):
                        raise ValueError("app-server returned an invalid result")
                    return result
        try:
            request("initialize", {"clientInfo": {"name": "prism_local", "version": "1"}})
            send({"method": "initialized"})
            cursor, models = None, {}
            for _ in range(20):
                result = request("model/list", {"cursor": cursor, "limit": 100, "includeHidden": False})
                if not isinstance(result.get("data"), list):
                    raise ValueError("app-server returned no model list")
                for entry in result["data"]:
                    if not isinstance(entry, dict) or entry.get("hidden"):
                        continue
                    mid = entry.get("model") or entry.get("id")
                    if not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,119}", mid):
                        continue
                    levels = entry.get("supportedReasoningEfforts") or []
                    if not isinstance(levels, list):
                        levels = []
                    efforts = [e.get("reasoningEffort") for e in levels
                               if isinstance(e, dict) and isinstance(e.get("reasoningEffort"), str)]
                    model = {"id": mid, "label": str(entry.get("displayName") or mid),
                             "efforts": list(self.model_efforts.get(mid, efforts))}
                    if entry.get("isDefault"):
                        model["is_default"] = True
                    if entry.get("defaultReasoningEffort") in model["efforts"]:
                        model["default_effort"] = entry["defaultReasoningEffort"]
                    models[mid] = model
                cursor = result.get("nextCursor")
                if not cursor:
                    return list(models.values())
            raise ValueError("model list has too many pages")
        finally:
            proc.stdin.close()
            if proc.poll() is None:
                kill_tree(proc)
            end_group(proc)
            reader.join(1)
            if not reader.is_alive():
                proc.stdout.close()

    def bin(self) -> str | None:
        return self.bin_override or codex_bin()

    def unavailable(self) -> str | None:
        return None if executable(self.bin()) else \
            "Codex CLI not found. Install it (npm i -g @openai/codex) or set CODEX_BIN=/path/to/codex."

    def info(self) -> dict:
        from claude_sandbox import availability
        runtime = availability("codex")
        supported = runtime["supported"]
        return {**super().info(), "bin": self.bin(),
                "research_initialization": supported,
                "notation_guard": {"application": supported, "display": supported,
                                   "proposals": supported, "decisions": supported},
                "research_runtime": runtime}

    def preflight(self, root: Path) -> str | None:
        binary = shutil.which(self.bin()) or self.bin()
        try:
            why = runtime_support(binary, Path(binary).stat().st_mtime_ns)
        except OSError as e:
            return f"Could not check Codex CLI executable: {e}"
        if why:
            return why
        try:
            p = run_bounded([binary, "login", "status"], text=True, timeout=10)
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
        # On Windows "/" is not absolute and Codex rejects the profile; ":root" names
        # the filesystem root there.
        rules = {":root" if os.name == "nt" else "/": "read"}
        if job.mode == "edit":
            if job.scope:
                for rel in job.scope:
                    # check_scope refuses these before the turn; never grant them here either.
                    if not protected_scope(root, rel):
                        rules[str(project_path(root, rel))] = "write"
            else:
                rules[str(root)] = "write"
            # Never grant writes through an external link or to runtime instructions.
            for path in protected_paths(root):
                rules[str(path)] = "read"
        table = "{" + ",".join(f"{json.dumps(p)}={json.dumps(v)}" for p, v in rules.items()) + "}"
        return [f'default_permissions="{PROFILE}"',
                f"permissions.{PROFILE}.filesystem={table}",
                f"permissions.{PROFILE}.network.enabled=false"]

    def command(self, job: Job) -> tuple[list[str], str]:
        cmd = [self.bin(), "exec", "--json", "--skip-git-repo-check",
               "--ignore-user-config", "--ignore-rules"]
        # Ignoring user config also drops windows.sandbox. Without an enabled
        # Windows sandbox, exec's never-approve policy rejects even file reads.
        # Select the native sandbox explicitly; keep the per-turn filesystem
        # profile and user hooks/MCP isolation intact.
        platform_config = ['windows.sandbox="elevated"'] if os.name == "nt" else []
        for setting in self.permission_config(job) + platform_config + ['approval_policy="never"',
                'web_search="disabled"', "features.apps=false", "features.hooks=false", "mcp_servers={}"]:
            cmd += ["-c", setting]
        if job.may_compile:
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
        prompt = ("[Instructions from the editor for this turn]\n"
                  "These instructions replace earlier editor instructions.\n"
                  f"{self.system_text()}\n[Message]\n{job.prompt}")
        return cmd + ["-"], prompt

    def check_research(self, root: Path) -> str | None:
        from claude_sandbox import availability
        result = availability("codex")
        return None if result["supported"] else result["reason"]

    def run(self, job: Job) -> dict:
        if job.research_access is not None:
            from codex_research import run
            return run(self, job)
        return super().run(job)

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

        def tool(name: str, summary: str, path: str | None = None) -> None:
            if iid not in seen:
                seen.add(iid)
                job.tool(iid, name, summary, path=path)

        if kind == "agent_message" and completed and item.get("text"):
            job.emit({"t": "message_start"})
            job.emit({"t": "text", "text": item["text"]})
        elif kind == "command_execution":
            tool("Bash", item.get("command") or "")
            if completed:
                code = item.get("exit_code")
                job.tool_result(iid, item.get("status") == "failed" or code not in (0, None),
                                item.get("aggregated_output"))
        elif kind == "file_change":
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            paths = [_rel(c.get("path", ""), job.root) for c in changes if isinstance(c, dict)]
            tool("Edit", ", ".join(paths), paths[0] if len(paths) == 1 else None)
            if completed:
                job.tool_result(iid, item.get("status") == "failed")
        elif kind == "mcp_tool_call":
            tool(f"{item.get('server', '')}.{item.get('tool', '')}", "")
            if completed:
                result = item.get("result") if isinstance(item.get("result"), dict) else {}
                content = result.get("content") if isinstance(result.get("content"), list) else []
                failed = item.get("status") == "failed" or bool(item.get("error")) or \
                    bool(result.get("isError") or result.get("is_error"))
                job.tool_result(iid, failed, "\n".join(str(c.get("text", "")) for c in content
                                                       if isinstance(c, dict)))
        elif kind == "web_search":
            tool("WebSearch", item.get("query") or "")
            if completed:
                job.tool_result(iid, False)
        elif kind == "reasoning":
            # Its reasoning, as the panel shows a model's thinking (a static "working…" before).
            thought: set = st.setdefault("reasoning", set())
            if iid not in thought:
                thought.add(iid)
                job.emit({"t": "thinking_start"})
            if completed and item.get("text"):
                job.emit({"t": "thinking", "text": str(item["text"])})
        elif kind == "error" and completed:
            # An error item is Codex's non-fatal notice (its turn goes on); turn.failed and a
            # top-level error say how a turn failed.
            job.emit({"t": "error", "message": item.get("message") or "Codex error", "notice": True})
