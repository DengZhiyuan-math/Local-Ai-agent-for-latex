"""Deep Code CLI backend: DeepSeek's terminal coding agent.

Deep Code (npm: @vegamo/deepcode-cli, command ``deepcode``) is the CLI that DeepSeek's API
documentation lists for agents. Each chat turn runs it non-interactively in the project:

    node <deepcode>/cli.js --exec --prompt <short> [--resume <session id>]

with the message on stdin: Deep Code appends piped stdin to the prompt, and stdin keeps a
long message with newlines, quotes or % intact, which an argument through Windows' npm
``deepcode.cmd`` shim would not. It prints only the final reply, at the end; afterwards
prism-local follows the session Deep Code saves (~/.deepcode/projects/<project code>/)
for live tool progress and the session id used to continue the conversation.

Settings are Deep Code's own (~/.deepcode/settings.json: model, base URL, API key,
permissions). prism-local only adds DEEPCODE_API_KEY from DEEPSEEK_API_KEY when Deep Code
has no key, and DEEPCODE_MODEL / DEEPCODE_REASONING_EFFORT for /model and /effort.

Deep Code has category permissions but no single-file permission. A temporary project
settings overlay denies Ask writes and registers the editor's compile MCP for Edit.
Scope uses post-turn restoration and is reported as such; it is not a sandbox.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from contextlib import contextmanager

from backends import Backend, Job, agent_env, cli_process, executable, file_place, find_bin
from fsutil import project_path, write_bytes
from gitenv import selects_repository
import mcp_compile
import registry
import sessionmeta

PACKAGE = "@vegamo/deepcode-cli"
ASK_NOTE = ("[Ask mode] Answer only: do not create, change or delete any file in this turn; "
            "do not compile. These instructions replace the mode of earlier turns.")
DEEPCODE_FILE_TOOL_RULE = """\
- Use Deep Code's native read, write and edit tools. Read before editing and use
  the snippet returned by read for edit. Use bash only for read-only searches.
  Do not change files or compile through bash.
"""


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


# Ask's no-write rule is the permission categories the turn's settings deny (write-in-cwd,
# delete-in-cwd, mcp: turn_settings); the first version they were checked on, with the
# real CLI, is 0.4.3 (docs/audits/2026-10-08-*). One that does not know them would let an
# Ask turn write, leaving only the restore after the turn.
MIN_VERSION = (0, 4, 3)


def installed_version(bin_path: str) -> tuple[int, int, int] | None:
    """Deep Code's version, from the package.json of the npm package its command starts;
    None when that cannot be told (a stand-in, a build of its own)."""
    entry = node_entry(bin_path)
    if len(entry) != 2 or Path(entry[1]).name != "cli.js":
        return None
    try:
        data = json.loads((Path(entry[1]).parent / "package.json").read_text(encoding="utf-8"))
        m = re.match(r"(\d+)\.(\d+)\.(\d+)", str(data.get("version", ""))) if data.get("name") == PACKAGE else None
    except (OSError, ValueError, AttributeError):
        return None
    return tuple(int(x) for x in m.groups()) if m else None


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
    index = read_settings(sessions_dir(root) / "sessions-index.json")
    entries = [e for e in (index.get("entries") or []) if isinstance(e, dict) and sessionmeta.valid_id(e.get("id"))]
    if not entries:
        return None
    best = max(entries, key=lambda e: _time(e.get("updateTime")))
    return best["id"] if _time(best.get("updateTime")) >= since - 5 \
        and DeepCode("deepcode").check_session(root, best["id"]) is None else None


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


def read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def user_settings() -> dict:
    return read_settings(Path.home() / ".deepcode" / "settings.json")


def injected_git(source: Path, settings: dict) -> str | None:
    """Why the settings read from `source` may not be used: their env sets Git's
    repository variables (GIT_DIR …), which would point the agent at another repository."""
    env = settings.get("env") or {}
    bad = sorted(k for k in env if selects_repository(k)) if isinstance(env, dict) else []
    return (f"Deep Code settings {source} inject Git repository variables: {', '.join(bad)}. "
            "Remove these variables before starting a turn.") if bad else None


def overlay_record(path: Path) -> Path:
    """Where a turn notes what it put in place of a project's Deep Code settings."""
    name = hashlib.sha256(str(path).encode()).hexdigest()[:20] + ".json"
    return registry.state_dir() / "deepcode-overlays" / name


def heal_overlay(path: Path) -> None:
    """A turn's settings overlay left behind by an editor killed during the turn: put back
    what was there before, where the file still is that overlay. Taken for the original
    instead, the next turn would keep its deny rules for good (an Ask turn's write-in-cwd
    denied in every Edit turn after, and in Deep Code run from a terminal)."""
    record = overlay_record(path)
    try:
        noted = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    try:
        current = path.read_bytes() if path.is_file() else None
        if current is not None and hashlib.sha256(current).hexdigest() == noted.get("overlay"):
            if noted.get("original") is None:
                path.unlink()
                if not noted.get("directory_existed"):
                    try:
                        path.parent.rmdir()
                    except OSError:
                        pass
            else:
                write_bytes(path, base64.b64decode(noted["original"]))
    except (OSError, ValueError):
        return                                   # left as it is; tried again next turn
    try:
        record.unlink()
    except OSError:
        pass


class SessionLog:
    """A session's file as Deep Code writes it during a turn, read as it grows: each read
    parses only the lines added since the one before. A line still being written waits
    for its end. A file that is shorter than what was read, or whose start or last read
    part changed (Deep Code rewrote it), is read again from the start."""

    CHECK = 4096          # bytes compared at the start and before the end of what was read

    def __init__(self, path: Path):
        self.path = path
        self.messages: list[dict] = []
        self.offset = 0
        self.head = self.tail = b""

    def read(self) -> list[dict]:
        """The whole messages in the file so far ([] when it cannot be read)."""
        try:
            with self.path.open("rb") as f:
                if self.offset:
                    size = os.fstat(f.fileno()).st_size
                    f.seek(self.offset - len(self.tail))
                    tail = f.read(len(self.tail))
                    f.seek(0)
                    if size < self.offset or tail != self.tail or f.read(len(self.head)) != self.head:
                        self.messages, self.offset, self.head, self.tail = [], 0, b"", b""
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if isinstance(m, dict):
                self.messages.append(m)
        if not self.offset:
            self.head = data[:min(end, self.CHECK)]
        self.offset += end
        self.tail = (self.tail + data[max(0, end - self.CHECK):end])[-self.CHECK:]
        return self.messages


class DeepCode(Backend):
    kind = "deepcode"
    label = "Deep Code (DeepSeek)"
    models = ["deepseek-v4-pro", "deepseek-flash"]
    efforts = ("low", "high", "max")
    enforces_scope = False
    read_only_ask = False           # category checks are not an OS sandbox; also restore writes
    file_tool_rule = DEEPCODE_FILE_TOOL_RULE

    def __init__(self, pid: str, spec: dict | None = None):
        super().__init__(pid, spec)
        self.bin_override = (spec or {}).get("bin")

    def bin(self) -> str | None:
        return self.bin_override or deepcode_bin()

    def has_key(self, root: Path | None = None) -> bool:
        env = user_settings().get("env") or {}
        if root is not None:
            try:
                project = read_settings(project_path(root, ".deepcode/settings.json"))
                env = {**env, **(project.get("env") or {})}
            except (ValueError, OSError, TypeError):
                pass
        return bool(os.environ.get("DEEPCODE_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
                    or (isinstance(env, dict) and env.get("API_KEY")))

    def unavailable(self) -> str | None:
        binary = self.bin()
        if not executable(binary) and not (binary and Path(binary).suffix == ".py" and Path(binary).is_file()):
            return (f"Deep Code not found. Install it (npm install -g {PACKAGE}) or set "
                    "DEEPCODE_BIN=/path/to/deepcode.")
        version = installed_version(binary)
        if version is not None and version < MIN_VERSION:
            need = ".".join(map(str, MIN_VERSION))
            return (f"This Deep Code ({'.'.join(map(str, version))}) is older than {need}, the first "
                    f"version Prism can keep from changing files in Ask mode. Update it: "
                    f"npm install -g {PACKAGE}")
        return None

    def preflight(self, root: Path) -> str | None:
        try:
            project = project_path(root, ".deepcode/settings.json")
        except (ValueError, OSError):
            return "Deep Code settings must be inside this project"
        # What the turn would find wrong only once it had started (turn_settings), said here,
        # with the file: nothing is started for a turn that cannot run.
        try:
            if project.is_symlink():
                return (f"Deep Code settings {project} must be a regular file of this project, "
                        "not a link. Replace the link with the file.")
            if project.is_file() and not isinstance(json.loads(project.read_bytes() or b"{}"), dict):
                return f"Deep Code settings {project} must hold a JSON object ({{ … }}). Fix the file, or remove it."
        except ValueError as e:
            return f"Deep Code settings {project} are not valid JSON ({e}). Fix the file, or remove it."
        except OSError as e:
            return f"Deep Code settings {project} cannot be read: {e}"
        inherited = user_settings().get("mcpServers") or {}
        if inherited:
            names = ", ".join(sorted(map(str, inherited))) if isinstance(inherited, dict) else "some"
            return (f"Deep Code would bring the MCP servers of {Path.home() / '.deepcode' / 'settings.json'} "
                    f"({names}) into this turn, which the editor cannot restrict. Remove them there, "
                    "or start Deep Code with a profile that has none.")
        for source in (Path.home() / ".deepcode/settings.json", project):
            bad = injected_git(source, read_settings(source))
            if bad:
                return bad
        if not self.has_key(root):
            return ("Deep Code has no DeepSeek API key: set DEEPSEEK_API_KEY, or API_KEY in "
                    "~/.deepcode/settings.json or .deepcode/settings.json.")
        return None

    def check_session(self, root: Path, session_id: str) -> str | None:
        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
            return "Invalid Deep Code session id. Start a new chat."
        path = sessions_dir(root)
        if path.is_symlink():
            return "Deep Code session directory must belong to this project. Start a new chat."
        directory = path.resolve()
        index = read_settings(directory / "sessions-index.json")
        session = directory / f"{session_id}.jsonl"
        if not any(isinstance(e, dict) and e.get("id") == session_id for e in index.get("entries") or []) \
                or not session.is_file() or session.resolve().parent != directory \
                or not sessionmeta.matches(session, root, session_id):
            return "Deep Code session does not belong to this project. Start a new chat."
        return None

    def info(self) -> dict:
        return {**super().info(), "bin": self.bin()}

    def env(self, job: Job) -> dict:
        env = agent_env(root=job.root)
        settings_env = user_settings().get("env") or {}
        if not env.get("DEEPCODE_API_KEY") and env.get("DEEPSEEK_API_KEY") and \
                not (isinstance(settings_env, dict) and settings_env.get("API_KEY")):
            env["DEEPCODE_API_KEY"] = env["DEEPSEEK_API_KEY"]
        model = job.model or self.default_model
        if model:
            env["DEEPCODE_MODEL"] = model
        if job.effort:
            env["DEEPCODE_REASONING_EFFORT"] = job.effort
        env.setdefault("NO_COLOR", "1")
        return env

    def message(self, job: Job) -> str:
        text = job.prompt
        if job.mode != "edit":
            text = f"{text}\n\n{ASK_NOTE}"
        return (f"[Instructions from the editor]\nThese instructions replace earlier editor instructions.\n"
                f"{self.system_text()}\n[Message]\n{text}")

    @contextmanager
    def turn_settings(self, job: Job):
        # Deep Code exposes settings only through files, not CLI flags. Restore the exact
        # original bytes afterwards, and never overwrite a concurrent settings edit.
        path = project_path(job.root, ".deepcode/settings.json")
        if path.is_symlink():
            raise ValueError("Deep Code settings must be a regular project file")
        heal_overlay(path)
        existed = path.exists()
        original = path.read_bytes() if existed else None
        directory_existed = path.parent.exists()
        settings = json.loads(original) if original is not None else {}
        if not isinstance(settings, dict):
            raise ValueError("Deep Code settings must hold a JSON object")
        # AgentManager.start ran preflight, but the file may have changed since (a sync, the
        # author): the copy this turn passes on is checked again.
        bad = injected_git(path, settings)
        if bad:
            raise ValueError(bad)
        deny = ["write-out-cwd", "write-in-tmp", "delete-out-cwd", "mutate-git-log", "network"]
        if job.mode != "edit":
            deny += ["write-in-cwd", "delete-in-cwd", "mcp"]
        previous = settings.get("permissions") or {}
        settings["permissions"] = {**previous, "deny": list(dict.fromkeys(previous.get("deny", []) + deny))}
        settings["mcpServers"] = {"prism": mcp_compile.config(job.server_url)} \
            if job.may_compile else {}
        # A project overlay cannot remove user MCP servers because Deep Code merges them.
        # Deny their tool category unless the editor compile server is the only one.
        inherited = user_settings().get("mcpServers") or {}
        if inherited:
            raise ValueError("Deep Code inherits user MCP servers. Use a profile without external MCP servers.")
        overlay = (json.dumps(settings, indent=2) + "\n").encode()
        record = overlay_record(path)
        registry.write_json(record, {"path": str(path), "overlay": hashlib.sha256(overlay).hexdigest(),
                                     "original": None if original is None else base64.b64encode(original).decode(),
                                     "directory_existed": directory_existed})
        write_bytes(path, overlay)
        try:
            yield
        finally:
            try:
                safe = not path.is_symlink() and project_path(job.root, ".deepcode/settings.json") == path
                unchanged = safe and path.is_file() and path.read_bytes() == overlay
            except (OSError, ValueError, RuntimeError):
                unchanged = False
            if unchanged:
                if original is None:
                    path.unlink()
                    if not directory_existed:
                        try:
                            path.parent.rmdir()
                        except OSError:
                            pass
                else:
                    write_bytes(path, original)
            else:
                job.emit({"t": "error", "message": "Deep Code settings changed during the turn; "
                          "the editor kept that edit instead of restoring the earlier settings."})
            try:
                record.unlink()
            except OSError:
                pass

    def run(self, job: Job) -> dict:
        # AgentManager.start has run preflight and check_session; turn_settings checks
        # again what may have changed since (the settings it passes on).
        with self.turn_settings(job):
            return self._run_cli(job)

    def _run_cli(self, job: Job) -> dict:
        cmd = node_entry(self.bin()) + ["--exec", "--prompt",
                                        "Do what the message in the <stdin> block asks."]
        if job.session_id:
            cmd += ["--resume", job.session_id]
        start = time.time()
        job.emit({"t": "thinking_start"})       # it prints nothing until the reply
        out_lines: list[str] = []
        seen, results = set(), set()
        live_sid, stamp, checked = job.session_id, None, 0.0
        logs: dict[str, SessionLog] = {}

        def log(sid: str) -> SessionLog:
            return logs.setdefault(sid, SessionLog(sessions_dir(job.root) / f"{sid}.jsonl"))
        # A resumed session holds the earlier turns: only what follows them is this turn's
        # (its new message may be only halfway written when the file is first looked at).
        earlier = len(log(job.session_id).read()) if job.session_id else 0

        def progress():
            nonlocal live_sid, stamp, checked
            now = time.monotonic()
            if now - checked < .5:
                return
            checked = now
            candidate = live_sid or latest_session(job.root, start)
            if not candidate:
                return
            try:
                stat = (sessions_dir(job.root) / f"{candidate}.jsonl").stat()
                current = (stat.st_mtime_ns, stat.st_size)
                if current == stamp or stat.st_mtime_ns < int(start * 1e9):
                    return
                if self.check_session(job.root, candidate):
                    return
                live_sid = candidate
                self._replay_tools(job, live_sid, earlier, seen=seen, results=results, log=log(live_sid))
                stamp = current
            except (OSError, UnicodeError, ValueError):
                pass                            # a session may be halfway through a write

        with cli_process(job, cmd, self.message(job), self.env(job), on_idle=progress) as (proc, lines, err_lines):
            if proc is None:
                return {"billing": "api"}       # stopped before it started
            for line in lines:
                out_lines.append(line)
            proc.wait()
        st: dict = {"exit": proc.returncode, "billing": "api"}
        sid = job.session_id or latest_session(job.root, start)
        if sid:
            bad = self.check_session(job.root, sid)
            if bad:
                raise ValueError(bad)
            st["session_id"] = sid
            job.emit({"t": "init", "session_id": sid, "model": job.model})
            self._replay_tools(job, sid, earlier, seen=seen, results=results, log=log(sid))
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

    def _replay_tools(self, job: Job, sid: str, earlier: int = 0, *, seen=None, results=None,
                      log: SessionLog | None = None) -> None:
        """The tools this turn used, from the session Deep Code saved (it prints none).
        `earlier`: how many messages the session held before this turn; `log`: the
        session's file as read so far this turn."""
        msgs = (log or SessionLog(sessions_dir(job.root) / f"{sid}.jsonl")).read()
        if len(msgs) < earlier:              # rewritten shorter: its last user message decides
            earlier = 0
        # This turn's part: after its user message, the last one after the earlier turns.
        start = max((i for i, m in enumerate(msgs) if i >= earlier and m.get("role") == "user"),
                    default=earlier - 1)
        seen = set() if seen is None else seen
        results = set() if results is None else results
        for offset, m in enumerate(msgs[start + 1:]):
            params = m.get("messageParams") or {}
            calls = m.get("tool_calls") or params.get("tool_calls") or []
            # Native Deep Code session records store the function on the tool message.
            if m.get("role") == "tool" and (m.get("meta") or {}).get("function"):
                calls = [{"id": params.get("tool_call_id"), "function": m["meta"]["function"]}]
            for call_index, call in enumerate(calls):
                cid = str(call.get("id") or f"deepcode-{start}-{offset}-{call_index}")
                if cid in seen:
                    continue
                seen.add(cid)
                fn = call.get("function") or {}
                name = fn.get("name") or call.get("name") or "tool"
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                summary, path, lines = "", None, None
                if isinstance(args, dict):
                    summary = str(args.get("path") or args.get("file_path") or args.get("command")
                                  or args.get("pattern") or args.get("query") or "")
                    path, lines = file_place(name, args)
                job.tool(cid, name, summary, path=path, lines=lines)
            if m.get("role") == "tool":
                rid = str(m.get("tool_call_id") or params.get("tool_call_id")
                          or f"deepcode-result-{start}-{offset}")
                if rid in results:
                    continue
                results.add(rid)
                content = m.get("content")
                try:
                    result = json.loads(content or "{}")
                except (TypeError, ValueError):
                    result = {}
                failed = isinstance(result, dict) and (result.get("ok") is False or bool(result.get("error")))
                job.tool_result(rid, failed, content)
