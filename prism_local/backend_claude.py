"""Claude Code backend.

Each chat turn runs the local Claude Code CLI headlessly in the repository:

    claude -p <prompt> --output-format stream-json --verbose
           --include-partial-messages --permission-mode <mode> [--resume <id>]

so the agent loads the project's CLAUDE.md, skills and .claude/settings.json
permissions exactly as it would in the terminal. In "edit" mode file edits are accepted
automatically; Bash is limited to the project's allowlist (non-interactive
runs cannot approve anything else). "ask" mode uses plan mode (read-only).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import registry
from backends import NO_WINDOW, SYSTEM_APPEND, TREE, CliBackend, Job, find_bin, kill_tree

MODES = {"edit": "acceptEdits", "ask": "plan"}


def claude_bin() -> str | None:
    # Where the installers put it, for a server started from a desktop menu whose PATH
    # lacks ~/.local/bin or npm's folder.
    home = Path.home()
    return find_bin("CLAUDE_BIN", "claude",
                    (str(home / ".local/bin/claude"), str(home / ".claude/local/claude"),
                     str(home / ".npm-global/bin/claude"), "/usr/local/bin/claude",
                     "/opt/homebrew/bin/claude"))


# ---------------------------------------------------------------- which account

# Any of these makes Claude Code use something other than the claude.ai login: an API
# key (billed per token), another account's token, or a cloud provider.
OVERRIDE_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")
_account_cache: dict = {}


def auth_overrides(root: Path | None) -> list[str]:
    """Settings that would take Claude Code away from the account login."""
    found = [f"the environment variable {v}" for v in OVERRIDE_ENV if os.environ.get(v)]
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") \
        else Path.home() / ".claude"
    files = [cfg / "settings.json"]
    if root:
        files += [Path(root) / ".claude" / "settings.json",
                  Path(root) / ".claude" / "settings.local.json"]
    for f in files:
        data = registry.read_json(f) or {}
        if not isinstance(data, dict):
            continue
        if data.get("apiKeyHelper"):
            found.append(f"apiKeyHelper in {f}")
        env = data.get("env") if isinstance(data.get("env"), dict) else {}
        found += [f"{v} in {f}" for v in OVERRIDE_ENV if env.get(v)]
    return found


def claude_account(exe: str | None, root: Path | None = None, max_age: float = 30) -> dict:
    """Who Claude Code is logged in as, from `claude auth status` (local, about 0.2 s)."""
    key = (exe, str(root))
    hit = _account_cache.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    info: dict = {"logged_in": False}
    if not exe:
        info["error"] = "Claude Code CLI not found"
    else:
        try:
            r = subprocess.run([exe, "auth", "status"], cwd=str(root) if root else None,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=20, **NO_WINDOW)
            m = re.search(r"\{.*\}", r.stdout, re.S)
            d = json.loads(m.group(0)) if m else {}
            info = {"logged_in": bool(d.get("loggedIn")), "email": d.get("email"),
                    "org": d.get("orgName"), "subscription": d.get("subscriptionType"),
                    "auth_method": d.get("authMethod"), "api_provider": d.get("apiProvider")}
            if not m:
                info["error"] = (r.stderr or r.stdout).strip()[:300] or "no answer"
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            info["error"] = str(e)
    info["overrides"] = auth_overrides(root)
    _account_cache[key] = (time.time(), info)
    return info


def allowed_account() -> str:
    """The only Claude account prism-local may use (Home → Settings); empty: any."""
    data = registry.read_json(registry.state_dir() / "settings.json") or {}
    return str(data.get("claude_account") or "").strip()


def account_problem(info: dict, allowed: str) -> str | None:
    """Why a turn must not run with this login, or None. Only when an account is set."""
    if not allowed:
        return None
    where = "Home → Settings allows only " + allowed
    if info.get("error"):
        return f"Could not check which Claude account is logged in ({info['error']}). {where}, so nothing was sent."
    if info.get("overrides"):
        return ("Claude Code would not use your account login here, because of "
                + "; ".join(info["overrides"]) + ". That bills an API key or another account. "
                f"{where}, so nothing was sent.")
    if (info.get("auth_method") or "claude.ai") != "claude.ai" \
            or (info.get("api_provider") or "firstParty") != "firstParty":
        return (f"Claude Code is using {info.get('auth_method')} / {info.get('api_provider')}, "
                f"not a claude.ai account login. {where}, so nothing was sent.")
    if not info.get("logged_in"):
        return f"Claude Code is not logged in. {where}. Run `claude` in a terminal and log in."
    if (info.get("email") or "").lower() != allowed.lower():
        return (f"Claude Code is logged in as {info.get('email')}, not {allowed}. {where}, so "
                "nothing was sent. Run `claude` in a terminal and use /login, or change the setting.")
    return None


def _summarize_tool(name: str, inp: dict, root: Path) -> str:
    def rel(p):
        try:
            return Path(p).resolve().relative_to(root).as_posix()
        except (ValueError, OSError, TypeError):
            return str(p)
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return rel(inp.get("file_path") or inp.get("notebook_path") or "")
    if name == "Bash":
        return inp.get("command", "")[:200]
    if name in ("Grep", "Glob"):
        return (inp.get("pattern") or "") + (f"  in {rel(inp['path'])}" if inp.get("path") else "")
    if name == "Skill":
        return inp.get("skill", "")
    if name in ("Agent", "Task"):
        return inp.get("description", "")
    for v in inp.values():
        if isinstance(v, str):
            return v[:120]
    return ""


class ClaudeCode(CliBackend):
    kind = "claude"
    label = "Claude Code"
    models = ["opus", "sonnet", "haiku", "fable", "best", "opusplan", "sonnet[1m]", "opus[1m]"]
    efforts = ("low", "medium", "high", "xhigh", "max")
    skills = True
    usage_limits = True

    def __init__(self, pid: str, spec: dict | None = None):
        super().__init__(pid, spec)
        self.bin_override = (spec or {}).get("bin")
        self.rate: dict | None = None   # last rate_limit_info seen, plus "at"
        self.probe_lock = threading.Lock()
        # Skills and slash commands Claude Code offers in this project, from its init event.
        self.catalog: dict | None = None
        self.catalog_lock = threading.Lock()

    def bin(self) -> str | None:
        return self.bin_override or claude_bin()

    def unavailable(self) -> str | None:
        return None if self.bin() else \
            "Claude Code CLI not found. Start the editor with CLAUDE_BIN=/path/to/claude."

    def info(self) -> dict:
        return {**super().info(), "bin": self.bin(), "rate": self.rate}

    def account(self, root: Path | None, fresh: bool = False) -> dict:
        info = claude_account(self.bin(), root, max_age=0 if fresh else 30)
        allowed = allowed_account()
        return {"account": info, "allowed": allowed, "problem": account_problem(info, allowed)}

    def preflight(self, root: Path) -> str | None:
        # Checked afresh before every turn when an account is set in Home → Settings.
        return self.account(root, fresh=True)["problem"] if allowed_account() else None

    @staticmethod
    def scope_rules(scope: list[str]) -> list[str]:
        """Claude Code permission rules that allow writing exactly the files in `scope`."""
        return [f"{tool}(./{rel})" for rel in scope for tool in ("Edit", "Write", "MultiEdit")]

    def command(self, job: Job) -> tuple[list[str], str]:
        perm = MODES.get(job.mode, "plan")
        if job.scope:
            # acceptEdits would accept an edit to any file. In default mode a headless run
            # refuses every write that no rule allows, so only these files can change.
            perm = "default"
        cmd = [self.bin(), "-p", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--permission-mode", perm,
               "--append-system-prompt", SYSTEM_APPEND]
        if job.scope:
            cmd += ["--allowedTools", *self.scope_rules(job.scope)]
        if job.session_id:
            cmd += ["--resume", job.session_id]
        if job.model:
            cmd += ["--model", job.model]
        if job.effort:
            cmd += ["--effort", job.effort]
        return cmd, job.prompt

    def handle(self, d: dict, job: Job, st: dict) -> None:
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
            st["session_id"] = d.get("session_id")
            self._remember_catalog(d)
            job.emit({"t": "init", "session_id": st["session_id"], "model": d.get("model")})
        elif t == "stream_event" and d.get("parent_tool_use_id") is None:
            ev = d.get("event") or {}
            if ev.get("type") == "content_block_delta" and \
                    (ev.get("delta") or {}).get("type") == "text_delta":
                job.emit({"t": "delta", "text": ev["delta"]["text"]})
            elif ev.get("type") == "message_start":
                job.emit({"t": "message_start"})
        elif t == "assistant" and d.get("parent_tool_use_id") is None:
            for block in (d.get("message") or {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    job.emit({"t": "text", "text": block["text"]})
                elif block.get("type") == "tool_use":
                    job.emit({"t": "tool", "id": block.get("id"), "name": block.get("name"),
                              "summary": _summarize_tool(block.get("name", ""),
                                                         block.get("input") or {}, job.root)})
        elif t == "user" and d.get("parent_tool_use_id") is None:
            for block in (d.get("message") or {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content")
                    if isinstance(content, list):
                        content = " ".join(c.get("text", "") for c in content
                                           if isinstance(c, dict))
                    job.emit({"t": "tool_result", "id": block.get("tool_use_id"),
                              "error": bool(block.get("is_error")),
                              "preview": str(content or "")[:300]})
        elif t == "rate_limit_event" and d.get("rate_limit_info"):
            self.rate = {**d["rate_limit_info"], "at": time.time()}
            job.emit({"t": "rate", "rate": self.rate})
        elif t == "result":
            st.update(session_id=d.get("session_id") or st.get("session_id"),
                      cost=d.get("total_cost_usd"), duration=d.get("duration_ms"),
                      is_error=d.get("is_error"), subtype=d.get("subtype"),
                      denials=[p.get("tool_name") for p in d.get("permission_denials") or []])

    def _remember_catalog(self, init: dict) -> None:
        skills = [s for s in init.get("skills") or [] if isinstance(s, str)]
        self.catalog = {
            "skills": skills,
            "commands": [c for c in init.get("slash_commands") or [] if isinstance(c, str)],
            "terminal_only": init.get("terminal_slash_commands") or [],
            "agents": init.get("agents") or [],
            "version": init.get("claude_code_version"),
            "at": time.time(),
        }

    def commands(self, root: Path, refresh: bool = False) -> dict:
        """Skills and slash commands available here, as Claude Code lists them.

        Each turn's init event refreshes the list. Before the first turn, start the
        CLI in the project, read its init event (which comes before any model call)
        and stop it. It runs with Haiku and no tools, so even if a request slips out
        before the process is stopped it costs next to nothing.
        """
        with self.catalog_lock:
            if self.catalog and not refresh:
                return self.catalog
            exe = self.bin()
            if not exe:
                return {"error": "Claude Code CLI not found"}
            bad = self.preflight(root)          # the account check of a turn, before any start
            if bad:
                return {"error": bad}
            cmd = [exe, "-p", "--model", "haiku", "--tools", "", "--no-session-persistence",
                   "--output-format", "stream-json", "--verbose"]
            try:
                proc = subprocess.Popen(cmd, cwd=root, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding="utf-8", errors="replace", **TREE)
            except OSError as e:
                return {"error": f"Could not start Claude Code: {e}"}
            timer = threading.Timer(60, kill_tree, args=(proc,))
            timer.start()
            try:
                proc.stdin.write("ok")
                proc.stdin.close()
                for line in proc.stdout:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") == "system" and d.get("subtype") == "init":
                        self._remember_catalog(d)
                        break
            except OSError:
                pass
            finally:
                timer.cancel()
                kill_tree(proc)
            return self.catalog or {"error": "Claude Code did not report its commands"}

    def probe_rate(self) -> dict:
        """Refresh usage limits with the cheapest possible call.

        Haiku, no tools, no MCP, no skills, no session file, run outside the
        repository so CLAUDE.md is not loaded (about $0.001 at list price).
        """
        exe = self.bin()
        if not exe:
            return {"error": "Claude Code CLI not found"}
        if not self.probe_lock.acquire(blocking=False):
            return {"rate": self.rate, "busy": True}
        try:
            cmd = [exe, "-p", "--model", "haiku", "--tools", "",
                   "--system-prompt", "Reply with the single word ok.",
                   "--no-session-persistence", "--strict-mcp-config",
                   "--disable-slash-commands", "--output-format", "stream-json", "--verbose"]
            try:
                out = subprocess.run(cmd, input="ok", capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=90,
                                     cwd=tempfile.gettempdir(), **NO_WINDOW).stdout
            except subprocess.TimeoutExpired:
                return {"error": "usage check timed out", "rate": self.rate}
            except OSError as e:
                return {"error": f"Could not start Claude Code: {e}", "rate": self.rate}
            for line in out.splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "rate_limit_event" and d.get("rate_limit_info"):
                    self.rate = {**d["rate_limit_info"], "at": time.time()}
            return {"rate": self.rate}
        finally:
            self.probe_lock.release()
