"""AI backends for the agent panel, and the registry that lists them.

A backend runs one chat turn in the project directory and reports what happens as
events. The agent manager (agent.py) does everything that does not depend on the
backend: it checks the request, snapshots project files, computes the per-file
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
    {"t": "error", "message": str}              (none after Stop: "done" says it stopped)

`run` returns a dict with any of: session_id, exit (0 = success), is_error, subtype,
error (what went wrong, in the backend's words), cost (USD, only when billed per token),
billing ("subscription" or "api"), duration (ms), usage ({"in": tokens, "out": tokens}),
denials (tool names refused), stderr. How the turn ended is decided from it once, by the
manager (agent.outcome), not by each backend: "done" carries outcome ("ok", "stopped",
"failed") and, for a failure no error event has told yet, error.
"""
from __future__ import annotations

import collections
import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from proc import NO_WINDOW, TREE, end_group, kill_tree, start  # noqa: F401 — re-exported
from fsutil import project_path


FILE_TOOL_RULE = """\
- Read, search and change files with your file tools (Read, Grep, Glob, Edit,
  Write), not shell commands such as cat, sed, python or rm.
"""

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
""" + FILE_TOOL_RULE + """\
- To compile, use the compile tool when you have it: it runs the editor's own
  build (the author's Compile button) and returns the errors with file:line.
  It builds the document you edited, a standalone .tex as well as the main file.
  Compile after substantial edits and fix the errors your changes caused.
  Never run pdflatex or latexmk yourself. Without the tool, do not compile:
  the author compiles in the editor.
- Never commit, push, or run git commands that modify the repository.
- Lines between "% [prism-local]" comment lines hold two co-authors' versions of the same
  passage, kept when they wrote it at the same time. Never delete those marker lines or
  either version unless the author asks you to settle that passage.
- Keep replies concise. If the project has a CLAUDE.md or AGENTS.md, follow it exactly.
- Do not claim an argument is correct, or a step proved, unless you checked it.
"""


def agent_env(base: dict | None = None) -> dict:
    """Remove inherited GitHub credentials and block normal git transports.

    This is a supplemental guard. A child can override environment variables;
    filesystem and network permissions must come from the backend runtime.
    """
    from gitenv import git_env
    env = git_env(base)
    env["GIT_ALLOW_PROTOCOL"] = "prism-local-agents-do-not-reach-remotes"
    env["GIT_TERMINAL_PROMPT"] = "0"
    for k in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        env.pop(k, None)
    # An empty gh configuration: gh finds no login (it keeps its token there or in the keyring
    # named by that configuration).
    import tempfile
    env["GH_CONFIG_DIR"] = os.path.join(tempfile.gettempdir(), "prism-local-no-gh-login")
    return env


def find_bin(env: str, name: str, extra: tuple[str, ...] = ()) -> str | None:
    """A CLI from the environment variable `env`, else PATH, else a few usual places."""
    return os.environ.get(env) or shutil.which(name) \
        or next((p for p in extra if Path(p).exists()), None)


FILE_TOOLS = ("read", "write", "edit", "str_replace", "create", "view", "open")


def file_place(name: str, args: dict) -> tuple[str | None, tuple[int, int] | None]:
    """Which file a tool call of a backend's own vocabulary (read_file, edit_file,
    write_file, str_replace_editor …) works on, and for a read of part of it which lines,
    from its arguments: (path as given, (first, last) from 1), for Job.tool."""
    low = str(name or "").lower()
    path = args.get("path") or args.get("file_path") or args.get("filename")
    if not path or not isinstance(path, str) or not any(w in low for w in FILE_TOOLS):
        return None, None
    lines = None
    try:
        if "read" in low or "view" in low:
            first = args.get("offset") or args.get("start_line") or args.get("line")
            count = args.get("limit")
            last = args.get("end_line")
            if first or count or last:
                first = max(1, int(first or 1))
                last = int(last) if last else first + max(1, int(count or 2000)) - 1
                lines = (first, max(first, last))
    except (TypeError, ValueError):
        lines = None
    return path, lines


class Tail:
    """The end of what a CLI wrote on stderr (all a turn ever shows of it), kept from the
    thread that reads it: not the whole of it, which a chatty CLI makes megabytes of."""

    def __init__(self, limit: int = 8000):
        self.limit = max(0, limit)
        self.parts, self.size, self.lock = collections.deque(), 0, threading.Lock()

    def add(self, text: str) -> None:
        if not text or not self.limit:
            return
        with self.lock:
            text = text[-self.limit:]
            self.parts.append(text)
            self.size += len(text)
            while self.parts and self.size > self.limit:
                first = self.parts.popleft()
                excess = self.size - self.limit
                if len(first) > excess:
                    self.parts.appendleft(first[excess:])
                    self.size = self.limit
                else:
                    self.size -= len(first)

    def __iter__(self):
        with self.lock:
            return iter(list(self.parts))


def shut_down(resp) -> None:
    """End an HTTP response another thread is reading (an API turn's stream, on Stop).
    Closing it would wait for the lock that reader holds until the next chunk comes, and
    then pull the stream from under it. Shutting its socket down ends the read at once on
    Linux and macOS; on Windows only at the next data, which is why the API backend reads
    in a thread of its own (backend_openai._stream) that the turn need not wait for."""
    try:
        resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        return
    except (AttributeError, OSError):
        pass
    try:
        resp.close()
    except Exception:  # noqa: BLE001 — closing from another thread may race
        pass


def executable(path: str | None) -> bool:
    return bool(path and (shutil.which(path) or
                (Path(path).is_file() and (os.name == "nt" or os.access(path, os.X_OK)))))


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
        self.before: dict = {}     # file contents and metadata around the turn
        self.after: dict = {}
        # What the editor wrote during the turn (AgentManager.server_wrote): the turn's
        # starting point for those files, folded into `before` once it ends (sealed).
        self.rebased: dict = {}
        self.rebased_folders: set[str] = set()
        self.sealed = False
        self.snapshot_issues: set[str] = set()
        self.metadata_before: set[str] = set()   # .git, .codex… present when it started
        self.attachments: list[str] = []
        self.errors_shown = 0                    # error events the page has had (emit)

    def emit(self, ev: dict) -> None:
        if ev.get("t") == "error":
            if self.cancel.is_set():
                return                  # what stopping it broke: "done" says it was stopped
            self.errors_shown += 1
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    @property
    def may_compile(self) -> bool:
        """Whether the turn has the editor's compile tool: an Edit turn, with the editor's server."""
        return self.mode == "edit" and bool(self.server_url)

    def tool(self, tid, name, summary="", path=None, lines=None) -> None:
        """A tool the agent called (the event's form is kept here, whatever shape the
        backend's own output had): its id, its name, a summary of its input, and, for a
        file tool, the file (`path`, as the backend has it: absolute or relative to the
        project; made project-relative here, and left out when it is outside) and for a
        read of part of it the lines (`lines`: first and last, from 1). The editor shows
        the agent there (turn.js: its marks), whichever backend runs."""
        ev = {"t": "tool", "id": "" if tid is None else str(tid), "name": str(name or "tool"),
              "summary": str(summary or "")[:200]}
        rel = self.relative(path) if path else None
        if rel:
            ev["path"] = rel
            if lines:
                try:
                    first, last = (int(x) for x in lines)
                    if 1 <= first <= last:
                        ev["lines"] = [first, last]
                except (TypeError, ValueError):
                    pass
        self.emit(ev)

    def relative(self, path) -> str | None:
        """A path the agent gave, relative to the project; None when it is outside."""
        try:
            p = Path(str(path))
            p = p if p.is_absolute() else self.root / p
            rel = p.resolve().relative_to(Path(self.root).resolve()).as_posix()
        except (ValueError, OSError, RuntimeError):
            return None
        return rel if rel not in ("", ".") else None

    def tool_result(self, tid, error, preview="") -> None:
        """How the tool call `tid` ended: whether it failed, the start of its output."""
        self.emit({"t": "tool_result", "id": "" if tid is None else str(tid), "error": bool(error),
                   "preview": str(preview or "")[:300]})

    def wait_events(self, after: int, timeout: float) -> tuple[list[dict], bool]:
        with self.cond:
            deadline = time.monotonic() + max(0, timeout)
            while len(self.events) <= after and not self.done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(remaining)
            # A short fixed window combines nearby tokens without postponing a batch
            # indefinitely. Completed turns and non-waiting polls return immediately.
            batch_until = min(deadline, time.monotonic() + .04)
            while len(self.events) > after and not self.done:
                remaining = batch_until - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(remaining)
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
    input_types = ("text",)
    rate: dict | None = None          # the usage limits last seen (usage_limits)

    def __init__(self, pid: str, spec: dict | None = None):
        spec = spec or {}
        self.id = pid
        self.label = spec.get("label") or self.label or pid
        if spec.get("models") is not None:
            self.models = [str(m) for m in spec["models"]]
        self.default_model = spec.get("default_model", self.default_model)
        if spec.get("efforts") is not None:
            self.efforts = tuple(str(e) for e in spec["efforts"])
        if spec.get("input_types") is not None:
            types = spec["input_types"]
            if not isinstance(types, list) or not types or any(t not in ("text", "image", "pdf") for t in types):
                raise ValueError("input_types must contain text, image or pdf")
            self.input_types = tuple(types)

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

    def check_scope(self, root: Path, scope: list[str]) -> str | None:
        """An Edit turn's @-mentioned files; a message here stops the turn."""
        return None

    def check_session(self, root: Path, session_id: str) -> str | None:
        return "This session's project cannot be verified. Start a new chat."

    def check_attachments(self, root: Path, paths: list[str]) -> str | None:
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            return "attachments must be a list of project file paths"
        for rel in paths:
            try:
                p = project_path(root, rel)
                if not p.is_file() or p.stat().st_size > 25 * 1024 * 1024:
                    return f"Attachment is missing or larger than 25 MB: {rel}"
                kind = attachment_type(p)
                if kind not in self.input_types:
                    return f"{self.label} does not support {kind} attachments: {rel}"
                if kind == "text":
                    data = p.read_bytes()
                    if b"\x00" in data:
                        return f"Attachment is not a supported text file: {rel}"
                    data.decode("utf-8")
            except (ValueError, OSError, RuntimeError) as e:
                return f"Cannot attach {rel}: {e}"
        return None

    def run(self, job: Job) -> dict:
        raise NotImplementedError

    def stop(self, job: Job) -> None:
        job.cancel.set()
        if job.proc and job.proc.poll() is None:
            kill_tree(job.proc)
        if job.http is not None:
            shut_down(job.http)

    def commands(self, root: Path, refresh: bool = False) -> dict:
        return {"skills": [], "commands": [], "terminal_only": []}

    def account(self, root: Path, fresh: bool = False) -> dict | None:
        """The account the backend would use ({"account", "allowed", "problem"}); None: it
        has none to show (an API key from the environment)."""
        return None

    def info(self) -> dict:
        why = self.unavailable()
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "available": why is None, "reason": why,
                "models": list(self.models), "default_model": self.default_model,
                "efforts": list(self.efforts), "skills": self.skills,
                "usage_limits": self.usage_limits, "enforces_scope": self.enforces_scope,
                "read_only_ask": self.read_only_ask, "input_types": list(self.input_types)}


def attachment_type(path: Path) -> str:
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        return "image"
    return "pdf" if path.suffix.lower() == ".pdf" else "text"


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
        with cli_process(job, cmd, stdin, agent_env(), bufsize=1) as (proc, lines, stderr_lines):
            if proc is None:
                return st                        # stopped before it started
            for line in lines:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    self.handle(d, job, st)
            proc.wait()
        st["exit"] = proc.returncode
        if st["exit"] != 0 or st.get("is_error"):
            st["stderr"] = "".join(stderr_lines)[-2000:]
        return st


OUTPUT_GRACE = 5      # seconds a CLI's output may stay open after it exited


@contextmanager
def cli_process(job: Job, cmd: list[str], stdin: str, env: dict, **popen):
    """A turn's CLI, its input `stdin` (the prompt goes through stdin so it can never be
    parsed as a flag). Yields (process, its output lines, its stderr lines so far); (None,
    [], []) when Stop was pressed before it started, so it never starts. Stop pressed while
    it starts still ends it. Its stderr is read from the start: a CLI that exits at once
    (stdin then cannot be written) still says why. And however the with-block is left (a
    handler raising on a line it did not expect), the CLI and the commands it started
    (proc.start) are ended, also those still running after it exited: none goes on
    writing the files after its turn.

    The output ends when the CLI's does, or OUTPUT_GRACE after the CLI exited (at once
    after Stop): a command it started (a background shell) may hold the output open for
    good, and the turn must not wait for it."""
    if job.cancel.is_set():
        yield None, [], []
        return
    err = Tail()
    out: queue.Queue = queue.Queue()
    proc = start(cmd, cwd=job.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env, **popen)
    job.proc = proc
    def drain_err():
        try:
            while chunk := proc.stderr.read(4096):
                err.add(chunk)
        except (OSError, ValueError):
            pass
        finally:
            closing(proc.stderr)        # read to the end, maybe long after the turn

    def read():
        try:
            for line in proc.stdout:
                out.put(line)
        except (OSError, ValueError):
            pass
        finally:
            closing(proc.stdout)
            out.put(None)
    drain = threading.Thread(target=drain_err, daemon=True)
    reader = threading.Thread(target=read, daemon=True)

    def lines():
        exited = None
        while True:
            try:
                line = out.get(timeout=0.2)
            except queue.Empty:
                if proc.poll() is None:
                    continue
                exited = exited or time.monotonic()
                if job.cancel.is_set() or time.monotonic() - exited > OUTPUT_GRACE:
                    return
                continue
            if line is None:
                return
            yield line
    try:
        if job.cancel.is_set():                  # Stop came while it started
            kill_tree(proc)
        drain.start()
        reader.start()
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except (OSError, ValueError):
            pass                                 # it has exited: its stderr says why
        yield proc, lines(), err
    finally:
        if proc.poll() is None:
            kill_tree(proc)
        end_group(proc)                          # and what it left running (it may hold its output)
        reader.join(0.5)
        drain.join(0.5 if reader.is_alive() else 5)   # held open too, most likely
        # Its pipes go with it. One whose reader still waits on it (a command the CLI
        # started holds it open) is closed by that reader when it ends: closing it here
        # would wait for that command.
        closing(proc.stdin)
        if not reader.is_alive():
            closing(proc.stdout)
        if not drain.is_alive():
            closing(proc.stderr)


def closing(pipe) -> None:
    try:
        pipe.close()
    except (OSError, ValueError):
        pass


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
