"""Deep Code CLI backend: DeepSeek's terminal coding agent.

Deep Code (npm: @vegamo/deepcode-cli, command ``deepcode``) is the CLI that DeepSeek's API
documentation lists for agents. Each chat turn runs it non-interactively in the project:

    node <deepcode>/cli.js --exec --prompt <short> [--resume <session id>]

with the message on stdin: Deep Code appends piped stdin to the prompt, and stdin keeps a
long message with newlines, quotes or % intact, which an argument through Windows' npm
``deepcode.cmd`` shim would not. It prints only the final reply, at the end; afterwards
prism-local reads the session Deep Code saved (~/.deepcode/projects/<project code>/) for
the session id, to continue the conversation, and for the tools it used.

Settings are Deep Code's own (~/.deepcode/settings.json: model, base URL, API key,
permissions). prism-local only adds DEEPCODE_API_KEY from DEEPSEEK_API_KEY when Deep Code
has no key, and DEEPCODE_MODEL / DEEPCODE_REASONING_EFFORT for /model and /effort.

Deep Code has neither a per-file write permission nor a read-only mode on its command
line: the agent manager reverts its writes outside the @-mentioned files, and every write
of an Ask turn.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from backends import SYSTEM_APPEND, TREE, Backend, Job, agent_env, find_bin

PACKAGE = "@vegamo/deepcode-cli"
ASK_NOTE = ("[Ask mode] Answer only: do not create, change or delete any file in this turn; "
            "the editor undoes any change.")


def deepcode_bin() -> str | None:
    home = Path.home()
    return find_bin("DEEPCODE_BIN", "deepcode", (str(home / ".local/bin/deepcode"),
                                                 str(home / ".npm-global/bin/deepcode"),
                                                 "/usr/local/bin/deepcode", "/opt/homebrew/bin/deepcode"))


def node_entry(bin_path: str) -> list[str]:
    """How to start Deep Code: `node .../cli.js` when the command is npm's shim (a .cmd on
    Windows cannot pass a multi-line prompt safely), else the command itself."""
    p = Path(bin_path)
    if p.suffix.lower() == ".py":                # a stand-in (the tests' fake Deep Code)
        import sys
        return [sys.executable, str(p)]
    if p.suffix.lower() in (".cmd", ".ps1", "") or p.is_symlink():
        for cli in (p.parent / "node_modules" / PACKAGE / "cli.js",
                    p.resolve().parent / "cli.js",
                    p.parent.parent / "lib" / "node_modules" / PACKAGE / "cli.js"):
            if cli.is_file():
                node = shutil.which("node") or str(p.parent / "node.exe")
                return [node, str(cli)]
    return [bin_path]


def project_code(root: Path) -> str:
    """The folder name Deep Code keeps a project's sessions under (its getProjectCode)."""
    root_s = str(root.resolve())
    legacy = re.sub(r"[\\/]", "-", root_s).replace(":", "")
    if len(legacy) <= 64:
        return legacy
    key = root_s.lower() if os.name == "nt" else root_s
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    base = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9._-]", "-", root.resolve().name)).strip("-.")
    prefix = re.sub(r"[-.]+$", "", base[:64 - 16 - 1]) or "project"
    return f"{prefix}-{digest}"


def sessions_dir(root: Path) -> Path:
    return Path.home() / ".deepcode" / "projects" / project_code(root)


def latest_session(root: Path, since: float) -> str | None:
    """The id of the session Deep Code updated last, if it did so after `since`."""
    try:
        index = json.loads((sessions_dir(root) / "sessions-index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = [e for e in (index.get("entries") or []) if isinstance(e, dict) and e.get("id")]
    if not entries:
        return None
    best = max(entries, key=lambda e: _time(e.get("updateTime")))
    return best["id"] if _time(best.get("updateTime")) >= since - 5 else None


def _time(v) -> float:
    """updateTime as seconds, whether Deep Code wrote milliseconds or an ISO date."""
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    if isinstance(v, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def user_settings() -> dict:
    try:
        data = json.loads((Path.home() / ".deepcode" / "settings.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class DeepCode(Backend):
    kind = "deepcode"
    label = "Deep Code (DeepSeek)"
    models = ["deepseek-v4-pro", "deepseek-flash"]
    efforts = ("low", "high", "max")
    enforces_scope = False
    read_only_ask = False           # no read-only mode: an Ask turn's writes are undone

    def __init__(self, pid: str, spec: dict | None = None):
        super().__init__(pid, spec)
        self.bin_override = (spec or {}).get("bin")

    def bin(self) -> str | None:
        return self.bin_override or deepcode_bin()

    def has_key(self) -> bool:
        env = user_settings().get("env") or {}
        return bool(os.environ.get("DEEPCODE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
                    or (isinstance(env, dict) and env.get("API_KEY")))

    def unavailable(self) -> str | None:
        if not self.bin():
            return (f"Deep Code not found. Install it (npm install -g {PACKAGE}) or set "
                    "DEEPCODE_BIN=/path/to/deepcode.")
        if not self.has_key():
            return ("Deep Code has no DeepSeek API key: set DEEPSEEK_API_KEY, or API_KEY in "
                    "~/.deepcode/settings.json, then restart prism-local.")
        return None

    def info(self) -> dict:
        return {**super().info(), "bin": self.bin()}

    def env(self, job: Job) -> dict:
        env = agent_env()                       # no git remote, no GitHub login
        settings_env = user_settings().get("env") or {}
        if not env.get("DEEPCODE_API_KEY") and env.get("DEEPSEEK_API_KEY") and \
                not (isinstance(settings_env, dict) and settings_env.get("API_KEY")):
            env["DEEPCODE_API_KEY"] = env["DEEPSEEK_API_KEY"]
        if job.model:
            env["DEEPCODE_MODEL"] = job.model
        if job.effort:
            env["DEEPCODE_REASONING_EFFORT"] = job.effort
        env.setdefault("NO_COLOR", "1")
        return env

    def message(self, job: Job) -> str:
        text = job.prompt
        if job.mode != "edit":
            text = f"{text}\n\n{ASK_NOTE}"
        if not job.session_id:
            # No flag adds to Deep Code's system prompt; the first message carries it, and
            # the resumed session keeps it.
            text = f"[Instructions from the editor]\n{SYSTEM_APPEND}\n[Message]\n{text}"
        return text

    def run(self, job: Job) -> dict:
        cmd = node_entry(self.bin()) + ["--exec", "--prompt",
                                        "Do what the message in the <stdin> block asks."]
        if job.session_id:
            cmd += ["--resume", job.session_id]
        start = time.time()
        job.emit({"t": "thinking_start"})       # it prints nothing until the reply
        out_lines: list[str] = []
        err_lines: list[str] = []
        job.proc = subprocess.Popen(cmd, cwd=job.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                    errors="replace", env=self.env(job), **TREE)
        threading.Thread(target=lambda: err_lines.extend(job.proc.stderr), daemon=True).start()
        job.proc.stdin.write(self.message(job))
        job.proc.stdin.close()
        for line in job.proc.stdout:
            out_lines.append(line)
        job.proc.wait()
        st: dict = {"exit": job.proc.returncode, "billing": "api"}
        sid = latest_session(job.root, start) or job.session_id
        if sid:
            st["session_id"] = sid
            job.emit({"t": "init", "session_id": sid, "model": job.model})
            self._replay_tools(job, sid, start)
        reply = "".join(out_lines).strip()
        if reply:
            job.emit({"t": "message_start"})
            job.emit({"t": "text", "text": reply})
        if st["exit"] != 0:
            st["is_error"] = True
            st["stderr"] = "".join(err_lines)[-2000:]
            if not job.cancel.is_set():
                msg = "".join(err_lines).strip()[-600:] or f"Deep Code exited with code {st['exit']}"
                job.emit({"t": "error", "message": msg})
        return st

    def _replay_tools(self, job: Job, sid: str, since: float) -> None:
        """The tools this turn used, from the session Deep Code saved (it prints none)."""
        try:
            lines = (sessions_dir(job.root) / f"{sid}.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        msgs = []
        for line in lines:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if isinstance(m, dict):
                msgs.append(m)
        # This turn's part: after the last user message that carries our prompt.
        start = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=-1)
        for m in msgs[start + 1:]:
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                name = fn.get("name") or call.get("name") or "tool"
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                summary = ""
                if isinstance(args, dict):
                    summary = str(args.get("path") or args.get("file_path") or args.get("command")
                                  or args.get("pattern") or args.get("query") or "")
                job.emit({"t": "tool", "id": str(call.get("id") or len(job.events)),
                          "name": name, "summary": summary[:200]})
            if m.get("role") == "tool":
                content = m.get("content")
                job.emit({"t": "tool_result", "id": str(m.get("tool_call_id") or ""),
                          "error": False, "preview": str(content)[:300] if content else ""})
