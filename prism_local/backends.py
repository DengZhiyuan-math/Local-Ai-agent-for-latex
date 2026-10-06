"""AI backends for the agent panel, and the registry that lists them.

A backend runs one chat turn in the project directory and reports what happens as
events. The agent manager (agent.py) does everything that does not depend on the
backend: it checks the request, snapshots the editable files, computes the per-file
diffs afterwards and undoes turns.

Three kinds ship with prism-local:

- ``claude`` (backend_claude.py): the local Claude Code CLI.
- ``codex`` (backend_codex.py): the local OpenAI Codex CLI (``codex exec --json``).
- ``deepcode`` (backend_deepcode.py): Deep Code, DeepSeek's terminal agent (``deepcode --exec``).
- ``openai`` (backend_openai.py): any OpenAI-compatible chat-completions API with an
  API key: DeepSeek, OpenAI, OpenRouter, Qwen, Moonshot, a local Ollama or vLLM.
  prism-local runs the agent loop itself with a small set of file tools.

To add another backend, subclass `Backend` (or `CliBackend` for a CLI that prints
JSON lines), implement `run` (or `command` and `handle`), and add its type to
`kinds` in `load_backends`, which also applies the presets and the user's settings.

Events a backend emits through ``job.emit`` (the panel understands exactly these):

    {"t": "init", "session_id": str, "model": str|None}
    {"t": "message_start"}                      a new assistant message begins
    {"t": "delta", "text": str}                 streamed text of that message
    {"t": "text", "text": str}                  a whole message (when not streamed)
    {"t": "thinking_start"}                     the model starts thinking
    {"t": "thinking", "text": str}              streamed thinking (where the model shows it)
    {"t": "tool_start", "id": str, "name": str} a tool call begins (its input still streams)
    {"t": "tool_live", "id": str, "path"?: str, "text"?: str, "old"?: str, "old_done"?: true}
                                                the file a Write/Edit is writing, as it streams
    {"t": "tool", "id": str, "name": str, "summary": str}
    {"t": "tool_result", "id": str, "error": bool, "preview": str}
    {"t": "build", "result": dict}              a build the agent ran (agent.py, not backends)
    {"t": "context", "used": int, "window": int|None}   how full the context window is
    {"t": "rate", "rate": dict}                 Claude usage limits
    {"t": "error", "message": str}

`run` returns a dict with any of: session_id, exit (0 = success), is_error, subtype,
cost (USD, only when billed per token), billing ("subscription" or "api"), duration
(ms), usage ({"in": tokens, "out": tokens}), denials (tool
names refused), stderr.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from proc import NO_WINDOW, TREE, kill_tree  # noqa: F401 — re-exported


SYSTEM_APPEND = """\
You are being driven from prism-local, a local LaTeX web editor, not the
terminal. The author sees your text in a chat panel next to the LaTeX source
and the compiled PDF.
- The message may include a [Referenced] block listing files and line ranges
  (with their text) that the author @-mentioned. Treat it as the author's
  pointer to what they mean by "this", "here", "the selection".
- In edit mode each message ends with a [Scope for this turn] line saying which
  files you may change. It replaces the scope of every earlier message.
- The author reviews every turn's file changes as a diff with an undo button,
  so make focused, minimal edits and say which files you changed.
- Write display math with named LaTeX environments: \\begin{equation} ...
  \\end{equation} (or equation*, align, align*, gather, multline), never the
  shortcuts \\[ ... \\] or $$ ... $$. Use \\begin{...} environments rather than
  shortcuts elsewhere too.
- Read, search and change files with your file tools (Read, Grep, Glob, Edit,
  Write), not shell commands such as cat, sed, python or rm.
- To compile, use the compile tool when you have it: it runs the editor's own
  build (the author's Compile button) and returns the errors with file:line.
  Compile after substantial edits and fix the errors your changes caused.
  Never run pdflatex or latexmk yourself. Without the tool, do not compile:
  the author compiles in the editor.
- Never commit, push, or run git commands that modify the repository.
- Keep replies concise. If the project has a CLAUDE.md or AGENTS.md, follow it exactly.
- Do not claim an argument is correct, or a step proved, unless you checked it.
"""


def find_bin(env: str, name: str, extra: tuple[str, ...] = ()) -> str | None:
    """A CLI from the environment variable `env`, else PATH, else a few usual places."""
    return os.environ.get(env) or shutil.which(name) \
        or next((p for p in extra if Path(p).exists()), None)


class Job:
    """One chat turn: the request, its event log and the file snapshots around it."""

    def __init__(self, jid: int):
        self.id = jid
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.done = False
        self.cancel = threading.Event()
        self.proc: subprocess.Popen | None = None   # a CLI backend's process
        self.http = None                            # an API backend's open response
        # The request, filled in by AgentManager.start.
        self.provider = ""
        self.prompt = ""
        self.session_id: str | None = None
        self.mode = "ask"
        self.model: str | None = None
        self.effort: str | None = None
        self.scope: list[str] | None = None     # files this turn may change (None: any)
        self.server_url: str | None = None      # the editor server (the compile tool)
        self.root = Path(".")
        self.files: Callable[[], list[str]] = lambda: []    # editable files
        self.writable: Callable[[str], bool] = lambda rel: False
        self.before: dict[str, bytes | None] = {}     # file contents around the turn
        self.after: dict[str, bytes | None] = {}

    def emit(self, ev: dict) -> None:
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def wait_events(self, after: int, timeout: float) -> tuple[list[dict], bool]:
        with self.cond:
            if len(self.events) <= after and not self.done:
                self.cond.wait(timeout)
            return self.events[after:], self.done


class Backend:
    """Base class. Subclasses set the class attributes and implement `run`."""

    kind = ""
    id = ""
    label = ""
    models: list[str] = []            # suggestions for /model; any valid name is accepted
    default_model: str | None = None
    efforts: tuple[str, ...] = ()     # accepted effort levels; empty: no effort setting
    # False: the backend cannot stop writes outside the @-mentioned files, so the
    # manager reverts such writes after the turn.
    enforces_scope = True
    # False: the backend has no read-only mode, so the manager reverts every write of an
    # Ask turn.
    read_only_ask = True
    skills = False                    # offers skills / slash commands (/api/agent/commands)
    usage_limits = False              # reports subscription usage limits (/api/agent/usage)

    def __init__(self, pid: str, spec: dict | None = None):
        spec = spec or {}
        self.id = pid
        self.label = spec.get("label") or self.label or pid
        if spec.get("models") is not None:
            self.models = [str(m) for m in spec["models"]]
        self.default_model = spec.get("default_model", self.default_model)
        if spec.get("efforts") is not None:
            self.efforts = tuple(str(e) for e in spec["efforts"])

    def unavailable(self) -> str | None:
        """None when the backend can run, else why not (shown to the author)."""
        return None

    def check(self, model: str | None, effort: str | None) -> str | None:
        if effort and effort not in self.efforts:
            return (f"{self.label} has no effort setting." if not self.efforts else
                    f"Effort for {self.label} must be one of: {', '.join(self.efforts)}")
        return None

    def preflight(self, root: Path) -> str | None:
        """Checked right before each turn; a message here stops the turn (nothing is sent)."""
        return None

    def run(self, job: Job) -> dict:
        raise NotImplementedError

    def stop(self, job: Job) -> None:
        job.cancel.set()
        if job.proc and job.proc.poll() is None:
            kill_tree(job.proc)
        if job.http is not None:
            try:
                job.http.close()
            except Exception:  # noqa: BLE001 — closing from another thread may race
                pass

    def commands(self, root: Path, refresh: bool = False) -> dict:
        return {"skills": [], "commands": [], "terminal_only": []}

    def info(self) -> dict:
        why = self.unavailable()
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "available": why is None, "reason": why,
                "models": list(self.models), "default_model": self.default_model,
                "efforts": list(self.efforts), "skills": self.skills,
                "usage_limits": self.usage_limits, "enforces_scope": self.enforces_scope}


class CliBackend(Backend):
    """A local CLI that takes the prompt on stdin and prints one JSON object per line."""

    def command(self, job: Job) -> tuple[list[str], str]:
        """The argv to start and the text to write to its stdin."""
        raise NotImplementedError

    def handle(self, d: dict, job: Job, st: dict) -> None:
        """Turn one JSON line into events; keep what `run` returns in `st`."""
        raise NotImplementedError

    def run(self, job: Job) -> dict:
        cmd, stdin = self.command(job)
        st: dict = {}
        stderr_lines: list[str] = []
        # The prompt goes through stdin so it can never be parsed as a flag. TREE: Stop
        # also ends the commands the CLI started.
        job.proc = subprocess.Popen(cmd, cwd=job.root, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                                    **TREE)
        job.proc.stdin.write(stdin)
        job.proc.stdin.close()
        threading.Thread(target=lambda: stderr_lines.extend(job.proc.stderr), daemon=True).start()
        for line in job.proc.stdout:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                self.handle(d, job, st)
        job.proc.wait()
        st["exit"] = job.proc.returncode
        if st["exit"] != 0 or st.get("is_error"):
            st["stderr"] = "".join(stderr_lines)[-2000:]
        return st


# ---------------------------------------------------------------- registry

# API presets. The key is read from the named environment variable, never from a file
# in the project (projects are often shared or committed).
PRESETS: dict[str, dict] = {
    "deepseek": {"type": "openai", "label": "DeepSeek", "base_url": "https://api.deepseek.com",
                 "api_key_env": "DEEPSEEK_API_KEY",
                 "models": ["deepseek-chat", "deepseek-reasoner"]},
    "openai": {"type": "openai", "label": "OpenAI API", "base_url": "https://api.openai.com/v1",
               "api_key_env": "OPENAI_API_KEY", "models": [],
               "efforts": ["minimal", "low", "medium", "high"]},
    "openrouter": {"type": "openai", "label": "OpenRouter",
                   "base_url": "https://openrouter.ai/api/v1",
                   "api_key_env": "OPENROUTER_API_KEY", "models": []},
    "qwen": {"type": "openai", "label": "Qwen (DashScope)",
             "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "api_key_env": "DASHSCOPE_API_KEY", "models": ["qwen-plus", "qwen-max"]},
    "moonshot": {"type": "openai", "label": "Kimi (Moonshot)", "base_url": "https://api.moonshot.cn/v1",
                 "api_key_env": "MOONSHOT_API_KEY", "models": []},
    "ollama": {"type": "openai", "label": "Ollama (local)", "base_url": "http://127.0.0.1:11434/v1",
               "api_key_env": None, "models": []},
}


def config_path() -> Path:
    """User-level agent settings: $PRISM_AGENTS, else ~/.prism-local/agents.json."""
    return Path(os.environ.get("PRISM_AGENTS") or Path.home() / ".prism-local" / "agents.json")


def read_config(path: Path | None = None) -> tuple[dict, str | None]:
    """The settings file as a dict, and an error message if it could not be read."""
    p = path or config_path()
    if not p.exists():
        return {}, None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("the file must hold a JSON object")
        return data, None
    except (OSError, ValueError) as e:
        return {}, f"{p}: {e}"


def load_backends(path: Path | None = None) -> tuple[dict[str, Backend], str, str | None]:
    """All backends (built-ins, presets, then the user's settings), the default id,
    and an error message if the settings file was bad.

    Settings file (JSON)::

        {
          "default": "deepseek",
          "providers": {
            "deepseek": {"default_model": "deepseek-chat"},
            "codex": {"bin": "C:/tools/codex.cmd"},
            "my-vllm": {"type": "openai", "label": "My vLLM",
                        "base_url": "http://gpu-box:8000/v1",
                        "api_key_env": "MY_VLLM_KEY", "models": ["qwen3-32b"]}
          }
        }

    An entry with the id of a built-in or preset changes only the fields it gives.
    ``"enabled": false`` hides a provider.
    """
    from backend_claude import ClaudeCode
    from backend_codex import Codex
    from backend_deepcode import DeepCode
    from backend_openai import OpenAICompat
    kinds = {"claude": ClaudeCode, "codex": Codex, "deepcode": DeepCode, "openai": OpenAICompat}

    data, err = read_config(path)
    specs: dict[str, dict] = {"claude": {"type": "claude"}, "codex": {"type": "codex"},
                              "deepcode": {"type": "deepcode"}}
    specs.update({k: dict(v) for k, v in PRESETS.items()})
    user = data.get("providers") or {}
    if not isinstance(user, dict):
        user, err = {}, err or "'providers' must be an object"
    for pid, spec in user.items():
        if isinstance(spec, dict):
            specs[pid] = {**specs.get(pid, {}), **spec}
    out: dict[str, Backend] = {}
    for pid, spec in specs.items():
        if spec.get("enabled") is False:
            continue
        cls = kinds.get(spec.get("type") or "openai")
        if cls is None:
            err = err or f"provider {pid}: unknown type {spec.get('type')!r}"
            continue
        try:
            out[pid] = cls(pid, spec)
        except (TypeError, ValueError) as e:
            err = err or f"provider {pid}: {e}"
    default = os.environ.get("PRISM_AGENT") or data.get("default") or "claude"
    if default not in out:
        default = next(iter(out), "claude")
    return out, default, err
