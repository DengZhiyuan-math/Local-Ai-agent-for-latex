"""Claude Code backend.

Each chat turn runs the local Claude Code CLI headlessly in the repository:

    claude -p <prompt> --output-format stream-json --verbose
           --include-partial-messages --permission-mode <mode> [--resume <id>]

so the agent loads the project's CLAUDE.md, skills and .claude/settings.json
permissions exactly as it would in the terminal. In "edit" mode file edits are accepted
automatically. Non-interactive runs cannot approve a shell command, so the shell tools
are left out unless the settings allow some commands, and then only those run. "ask" mode uses plan mode (read-only).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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


# Tools that run shell commands. A headless turn cannot ask you to approve one, so Claude
# Code refuses every command that no rule allows. Without such a rule the agent does not
# get these tools at all: it then reads, searches and edits with its own tools instead of
# cat, sed or rm, and leaves compiling to the editor.
SHELL_TOOLS = ("Bash", "PowerShell", "Monitor")
COMPILE_TOOL = "mcp__prism__compile"       # mcp_compile.py, as Claude Code names it


def shell_rules(root: Path | None) -> list[str]:
    """The permission rules for shell tools in the user's and the project's settings."""
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR")         else Path.home() / ".claude"
    files = [cfg / "settings.json"]
    if root:
        files += [Path(root) / ".claude" / "settings.json",
                  Path(root) / ".claude" / "settings.local.json"]
    rules = []
    for f in files:
        data = registry.read_json(f) or {}
        perms = data.get("permissions") if isinstance(data, dict) else None
        allow = perms.get("allow") if isinstance(perms, dict) else None
        for r in allow if isinstance(allow, list) else []:
            if isinstance(r, str) and r.split("(", 1)[0].strip() in SHELL_TOOLS:
                rules.append(r)
    return rules


def _loads_compile_tool(block: dict) -> bool:
    """Claude Code loading the compile tool before its first use: not a step to show."""
    q = str((block.get("input") or {}).get("query") or "")
    return block.get("name") == "ToolSearch" and COMPILE_TOOL in q         and all(COMPILE_TOOL == t.strip() for t in q.removeprefix("select:").split(","))


def context_tokens(usage: dict | None) -> int:
    """How much of the context window a message used: what it read plus what it wrote."""
    if not isinstance(usage, dict):
        return 0
    return sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                "cache_read_input_tokens", "output_tokens"))


def _tool_place(name: str, inp: dict, root: Path) -> dict:
    """Which file a file tool works on, and for a Read of part of it which lines, so the
    editor can show the agent there: {"path": rel, "lines": [first, last]}."""
    if name not in ("Read", "Edit", "Write", "MultiEdit") or not inp.get("file_path"):
        return {}
    place: dict = {"path": _summarize_tool(name, inp, root)}
    if name == "Read" and (inp.get("offset") or inp.get("limit")):
        try:
            first = max(1, int(inp.get("offset") or 1))
            place["lines"] = [first, first + max(1, int(inp.get("limit") or 2000)) - 1]
        except (TypeError, ValueError):
            pass
    return place


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


# The field of a file-writing tool whose text is shown while the model is still writing it.
LIVE_FIELD = {"Write": "content", "Edit": "new_string"}
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
FILE_PATH_RE = re.compile(r'"file_path"\s*:\s*("(?:[^"\\]|\\.)*")')


class LiveInput:
    """Decodes one string field of a tool's input while its JSON is still streaming in.

    `feed` takes the next fragment of the JSON and returns the newly decoded text of the
    field (or ""). It parses each character once, so a long file costs no more than its
    length. An escape cut off at the end of a fragment waits for the next fragment.
    """

    def __init__(self, field: str):
        self.key = re.compile(r'"%s"\s*:\s*"' % re.escape(field))
        self.buf, self.pos, self.done = "", -1, False   # pos: next char of the value
        self.path: str | None = None

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        if self.path is None:
            m = FILE_PATH_RE.search(self.buf)
            if m:
                try:
                    self.path = json.loads(m.group(1))
                except ValueError:
                    self.path = ""
        if self.done:
            return ""
        if self.pos < 0:
            m = self.key.search(self.buf)
            if not m:
                return ""
            self.pos = m.end()
        out, i, buf, n = [], self.pos, self.buf, len(self.buf)
        while i < n:
            c = buf[i]
            if c == '"':
                self.done = True
                break
            if c != "\\":
                out.append(c)
                i += 1
                continue
            if i + 1 >= n:
                break
            e = buf[i + 1]
            if e != "u":
                out.append(ESCAPES.get(e, e))
                i += 2
                continue
            if i + 6 > n:
                break
            try:
                cp = int(buf[i + 2:i + 6], 16)
            except ValueError:
                cp = 0xFFFD
            if 0xD800 <= cp < 0xDC00:                  # first half of a surrogate pair
                if i + 12 > n:
                    break
                try:
                    lo = int(buf[i + 8:i + 12], 16) if buf[i + 6:i + 8] == "\\u" else 0
                except ValueError:
                    lo = 0
                if 0xDC00 <= lo < 0xE000:
                    out.append(chr(0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)))
                    i += 12
                    continue
                cp = 0xFFFD
            elif 0xDC00 <= cp < 0xE000:
                cp = 0xFFFD
            out.append(chr(cp))
            i += 6
        self.pos = i
        return "".join(out)


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
        # Each model's context window, from the last turn's result (modelUsage).
        self.windows: dict[str, int] = {}

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
        allowed = self.scope_rules(job.scope) if job.scope else []
        if job.server_url:
            # The compile tool (mcp_compile.py): the editor's own build, no shell needed.
            cmd += ["--mcp-config", json.dumps({"mcpServers": {"prism": {
                "command": sys.executable,
                "args": [str(Path(__file__).with_name("mcp_compile.py")), "--url", job.server_url]}}})]
            allowed.append(COMPILE_TOOL)
        if allowed:
            cmd += ["--allowedTools", *allowed]
        if not shell_rules(job.root):
            cmd += ["--disallowedTools", *SHELL_TOOLS]
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
            # "none": the claude.ai login, whose turns count against the plan's usage
            # limits; anything else is an API key, billed per token.
            st["api_key_source"] = d.get("apiKeySource")
            st["model"] = d.get("model")
            self._remember_catalog(d)
            job.emit({"t": "init", "session_id": st["session_id"], "model": d.get("model")})
        elif t == "stream_event" and d.get("parent_tool_use_id") is None:
            ev = d.get("event") or {}
            et, delta = ev.get("type"), ev.get("delta") or {}
            if et == "content_block_delta" and delta.get("type") == "text_delta":
                job.emit({"t": "delta", "text": delta["text"]})
            elif et == "content_block_start" and                     (ev.get("content_block") or {}).get("type") in ("thinking", "redacted_thinking"):
                # Thinking starts. Its text follows only where the model sends it: newer
                # models think without showing it, and then the panel shows that it thinks.
                job.emit({"t": "thinking_start"})
            elif et == "content_block_delta" and delta.get("type") == "thinking_delta":
                if delta.get("thinking"):
                    job.emit({"t": "thinking", "text": delta["thinking"]})
            elif et == "content_block_start" and                     (ev.get("content_block") or {}).get("type") == "tool_use":
                # A tool call starts. Its input streams in as JSON fragments; for a file
                # write or edit, the text is shown as it is written (tool_live events):
                # "text" is the new text, "old" the text an Edit replaces.
                block = ev["content_block"]
                name = block.get("name")
                if name == "ToolSearch":
                    return                      # shown once its input says what it loads
                fields = {"text": LiveInput(LIVE_FIELD[name])} if name in LIVE_FIELD else {}
                if name == "Edit":
                    fields["old"] = LiveInput("old_string")
                st.setdefault("live", {})[ev.get("index")] = (block.get("id"), fields)
                job.emit({"t": "tool_start", "id": block.get("id"),
                          "name": "Compile" if name == COMPILE_TOOL else name})
            elif et == "content_block_delta" and delta.get("type") == "input_json_delta":
                tid, fields = st.get("live", {}).get(ev.get("index"), (None, {}))
                if fields:
                    chunk = delta.get("partial_json") or ""
                    out = {"t": "tool_live", "id": tid}
                    for key, li in fields.items():
                        had_path, was_done = li.path is not None, li.done
                        text = li.feed(chunk)
                        if key == "text" and li.path is not None and not had_path:
                            out["path"] = _summarize_tool("Write", {"file_path": li.path}, job.root)
                        if text:
                            out[key] = text
                        if key == "old" and li.done and not was_done:
                            out["old_done"] = True
                    if len(out) > 2:
                        job.emit(out)
            elif et == "message_start":
                st["live"] = {}
                job.emit({"t": "message_start"})
        elif t == "assistant" and d.get("parent_tool_use_id") is None:
            used = context_tokens((d.get("message") or {}).get("usage"))
            if used:
                st["context"] = {"used": used, "window": self.windows.get(st.get("model") or "")}
                job.emit({"t": "context", **st["context"]})
            for block in (d.get("message") or {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    job.emit({"t": "text", "text": block["text"]})
                elif block.get("type") == "tool_use":
                    if _loads_compile_tool(block):
                        continue
                    job.emit({"t": "tool", "id": block.get("id"),
                              "name": "Compile" if block.get("name") == COMPILE_TOOL
                              else block.get("name"),
                              "summary": _summarize_tool(block.get("name", ""),
                                                         block.get("input") or {}, job.root),
                              **_tool_place(block.get("name", ""), block.get("input") or {},
                                            job.root)})
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
            # total_cost_usd is what the turn would cost at API list prices. With the
            # claude.ai login nothing is billed, so report the tokens instead of a price.
            u = d.get("usage") or {}
            subscription = st.get("api_key_source") in (None, "none")
            window = self._remember_window(d.get("modelUsage") or {}, st.get("model"))
            if st.get("context"):
                st["context"] = {**st["context"], "window": window or st["context"].get("window")}
            st.update(session_id=d.get("session_id") or st.get("session_id"),
                      cost=None if subscription else d.get("total_cost_usd"),
                      billing="subscription" if subscription else "api",
                      usage={"in": sum(u.get(k) or 0 for k in (
                                 "input_tokens", "cache_creation_input_tokens",
                                 "cache_read_input_tokens")),
                             "out": u.get("output_tokens") or 0} if u else None,
                      duration=d.get("duration_ms"),
                      is_error=d.get("is_error"), subtype=d.get("subtype"),
                      denials=[p.get("tool_name") for p in d.get("permission_denials") or []],
                      # What each refused call was, e.g. the command: the panel names it.
                      denied=[{"tool": p.get("tool_name"),
                               "what": _summarize_tool(p.get("tool_name") or "",
                                                       p.get("tool_input") or {}, job.root)}
                              for p in d.get("permission_denials") or []])

    def _remember_window(self, model_usage: dict, model: str | None) -> int | None:
        """The context window of the turn's model (modelUsage also lists side calls, e.g.
        Haiku: take the turn's model, or else the one that read the most)."""
        entries = {k: v for k, v in model_usage.items() if isinstance(v, dict) and v.get("contextWindow")}
        if not entries:
            return None
        key = next((k for k in entries if model and (k == model or model.startswith(k))), None) \
            or max(entries, key=lambda k: (entries[k].get("inputTokens") or 0)
                   + (entries[k].get("cacheReadInputTokens") or 0))
        window = int(entries[key]["contextWindow"])
        if model:
            self.windows[model] = window
        return window

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
