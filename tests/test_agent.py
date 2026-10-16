"""Unit tests for the agent manager and its backends (prism_local/agent.py, backend_*.py).

No real CLI or API is started: CLI backends are checked through the command they
would run and the events they make from sample output, and the OpenAI-compatible
backend runs against a fake chat-completions server on 127.0.0.1.
"""
import itertools
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/: tmpdirs, test_lifecycle
from tmpdirs import tmpdir  # noqa: E402
from test_proc import gone  # noqa: E402
import agent  # noqa: E402
import backends  # noqa: E402
import backend_claude  # noqa: E402
import mcp_compile  # noqa: E402
from backend_claude import ClaudeCode  # noqa: E402
from backend_codex import Codex  # noqa: E402
from backend_openai import OpenAICompat  # noqa: E402


def kill_pid(pid: int) -> None:
    """End a process a test's fake CLI left running (TerminateProcess on Windows)."""
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def manager(root=Path("."), files=lambda: [], **kinds):
    """An AgentManager whose backends are exactly `kinds` (id -> Backend)."""
    return agent.AgentManager(lambda: Path(root), files, backends=(kinds, next(iter(kinds)), None))


def job_for(**kw):
    j = backends.Job(1)
    for k, v in kw.items():
        setattr(j, k, v)
    return j


def no_account_lock(test):
    """Ignore the allowed Claude account in the user's own settings, which would make a
    test run the real `claude auth status`."""
    from unittest import mock
    patcher = mock.patch("backend_claude.allowed_account", return_value="")
    patcher.start()
    test.addCleanup(patcher.stop)


class ModelAndEffort(unittest.TestCase):
    def setUp(self):
        no_account_lock(self)
        self.claude = ClaudeCode("claude", {"bin": "claude"})     # never actually started
        self.m = manager(claude=self.claude)

    def test_model_names(self):
        for ok in ("opus", "sonnet[1m]", "claude-opus-5-5", "opusplan", "deepseek-chat",
                   "deepseek/deepseek-chat"):
            self.assertTrue(agent.MODEL_RE.fullmatch(ok), ok)
        for bad in ("--dangerously-skip-permissions", "-x", "a b", ""):
            self.assertFalse(agent.MODEL_RE.fullmatch(bad), bad)

    def test_rejects_flags_as_model_and_unknown_effort(self):
        self.assertIn("error", self.m.start("hi", None, "ask", model="--dangerously-skip-permissions"))
        self.assertIn("error", self.m.start("hi", None, "ask", effort="turbo"))
        self.assertIn("error", self.m.start("hi", None, "ask", provider="nope"))
        self.assertIsNone(self.m.active)

    def test_efforts_are_per_backend(self):
        codex = Codex("codex", {"bin": "codex"})
        self.assertIsNone(codex.check(None, "minimal"))
        self.assertIsNotNone(codex.check(None, "max"))
        ds = OpenAICompat("deepseek", backends.PRESETS["deepseek"])
        self.assertIsNotNone(ds.check(None, "high"), "DeepSeek has no effort setting")


class ClaudeScope(unittest.TestCase):
    """The scope note comes from the manager; the permission rules from the backend."""

    def setUp(self):
        no_account_lock(self)
        self.claude = ClaudeCode("claude", {"bin": "claude"})
        self.m = manager(claude=self.claude)
        self.jobs = []
        self.m._run = lambda job, backend: self.jobs.append(job)    # never start Claude

    def cmd(self):
        for _ in range(50):
            if self.jobs:
                return self.claude.command(self.jobs[-1])           # (argv, stdin)
            time.sleep(0.02)
        self.fail("no job")

    def test_mentions_allow_only_those_files(self):
        self.m.start("hi", None, "edit", scope=["main.tex", "sections/intro.tex"])
        cmd, prompt = self.cmd()
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "default")
        rules = cmd[cmd.index("--allowedTools") + 1:]
        self.assertIn("Edit(./main.tex)", rules)
        self.assertIn("Write(./sections/intro.tex)", rules)
        self.assertTrue(prompt.startswith("hi"))
        self.assertIn("ONLY these files in this turn: main.tex, sections/intro.tex", prompt)

    def test_no_mentions_edit_anything(self):
        self.m.start("/code-review", None, "edit")
        cmd, prompt = self.cmd()
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertNotIn("--allowedTools", cmd)
        self.assertTrue(prompt.startswith("/code-review"), "a /command must stay first")
        self.assertIn("no longer apply", prompt)

    def test_ask_mode_ignores_scope(self):
        self.m.start("hi", None, "ask", scope=["main.tex"])
        cmd, prompt = self.cmd()
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
        self.assertNotIn("--allowedTools", cmd)


class ClaudeLiveEvents(unittest.TestCase):
    """Thinking and the text of a file being written reach the panel while they stream."""

    def stream(self, *events):
        return [{"type": "stream_event", "parent_tool_use_id": None, "event": e} for e in events]

    def test_write_streams_its_content(self):
        root = Path(tempfile.gettempdir()).resolve()
        text = "\\documentclass{amsart}\n\\title{\"Primes\" é 😀}\n\ttab \\u0041\n"
        raw = json.dumps({"file_path": str(root / "sec" / "a.tex"), "content": text})
        # Fragments cut anywhere, through escapes and surrogate pairs.
        frags = [raw[i:i + 3] for i in range(0, len(raw), 3)]
        lines = self.stream(
            {"type": "message_start"},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "Plan the file."}},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "tu1", "name": "Write", "input": {}}},
            *({"type": "content_block_delta", "index": 1,
               "delta": {"type": "input_json_delta", "partial_json": f}} for f in frags))
        j, st = job_for(root=root), {}
        cc = ClaudeCode("claude", {"bin": "claude"})
        for d in lines:
            cc.handle(d, j, st)
        ev = j.events
        self.assertEqual(ev[1:3], [{"t": "thinking_start"}, {"t": "thinking", "text": "Plan the file."}])
        self.assertEqual(ev[3], {"t": "tool_start", "id": "tu1", "name": "Write"})
        live = [e for e in ev if e["t"] == "tool_live"]
        self.assertEqual([e["path"] for e in live if "path" in e], ["sec/a.tex"])
        self.assertEqual("".join(e.get("text", "") for e in live), text)

    def test_edit_streams_old_and_new_text(self):
        root = Path(tempfile.gettempdir()).resolve()
        raw = json.dumps({"file_path": str(root / "main.tex"), "old_string": "a \[x\]",
                          "new_string": "a \begin{equation}x\end{equation}"})
        lines = self.stream(
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "e1", "name": "Edit", "input": {}}},
            *({"type": "content_block_delta", "index": 0,
               "delta": {"type": "input_json_delta", "partial_json": raw[i:i + 4]}}
              for i in range(0, len(raw), 4)))
        j, st = job_for(root=root), {}
        cc = ClaudeCode("claude", {"bin": "claude"})
        for d in lines:
            cc.handle(d, j, st)
        live = [e for e in j.events if e["t"] == "tool_live"]
        self.assertEqual("".join(e.get("old", "") for e in live), "a \[x\]")
        self.assertEqual("".join(e.get("text", "") for e in live), "a \begin{equation}x\end{equation}")
        done = next(i for i, e in enumerate(live) if e.get("old_done"))
        self.assertFalse(any("text" in e for e in live[:done]), "the old text is complete first")

    def test_tools_say_where_they_work(self):
        root = Path(tempfile.gettempdir()).resolve()
        j, st = job_for(root=root), {}
        cc = ClaudeCode("claude", {"bin": "claude"})
        cc.handle({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "tool_use", "id": "r1", "name": "Read",
             "input": {"file_path": str(root / "sec" / "a.tex"), "offset": 20, "limit": 11}},
            {"type": "tool_use", "id": "r2", "name": "Read", "input": {"file_path": str(root / "b.tex")}},
            {"type": "tool_use", "id": "g1", "name": "Grep", "input": {"pattern": "x"}}]}}, j, st)
        r1, r2, g1 = j.events
        self.assertEqual((r1["path"], r1["lines"]), ("sec/a.tex", [20, 30]))
        self.assertEqual(r2["path"], "b.tex")
        self.assertNotIn("lines", r2, "a whole file is not a range to mark")
        self.assertNotIn("path", g1)

    def test_other_tools_stream_nothing(self):
        j, st = job_for(), {}
        cc = ClaudeCode("claude", {"bin": "claude"})
        for d in self.stream(
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "tool_use", "id": "b", "name": "Bash", "input": {}}},
                {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls"}'}}):
            cc.handle(d, j, st)
        self.assertEqual([e["t"] for e in j.events], ["tool_start"])


class ClaudeShell(unittest.TestCase):
    """A turn here cannot approve a shell command: without allow rules there is no shell."""

    def setUp(self):
        self.cfg = tmpdir()                     # an empty user config, not the real one
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        self.addCleanup(lambda: os.environ.pop("CLAUDE_CONFIG_DIR", None) if old is None
                        else os.environ.__setitem__("CLAUDE_CONFIG_DIR", old))
        from unittest import mock
        patcher = mock.patch("backend_claude.use_config_dir")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = tmpdir()
        self.claude = ClaudeCode("claude", {"bin": "claude"})

    def disallowed(self):
        cmd, _ = self.claude.command(job_for(mode="edit", root=self.root))
        if "--disallowedTools" not in cmd:
            return []
        i = cmd.index("--disallowedTools") + 1
        return cmd[i:i + len(backend_claude.SHELL_TOOLS)]

    def test_no_rules_no_shell(self):
        self.assertEqual(self.disallowed(), ["Bash", "PowerShell", "Monitor"])

    def test_allowed_commands_keep_the_shell(self):
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["Bash(latexmk:*)"]}}), encoding="utf-8")
        self.assertEqual(self.disallowed(), [])

    def test_refused_steps_are_named(self):
        j, st = job_for(root=self.root), {}
        self.claude.handle({"type": "result", "permission_denials": [
            {"tool_name": "Bash", "tool_input": {"command": "latexmk -pdf main.tex"}}]}, j, st)
        self.assertEqual(st["denied"], [{"tool": "Bash", "what": "latexmk -pdf main.tex"}])


class CompileTool(unittest.TestCase):
    """The agent compiles with the editor's build (mcp_compile.py), not a shell."""

    def test_claude_gets_the_tool(self):
        cc = ClaudeCode("claude", {"bin": "claude"})
        cmd, _ = cc.command(job_for(mode="edit", root=tmpdir(), server_url="http://127.0.0.1:9/"))
        cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])
        args = cfg["mcpServers"]["prism"]["args"]
        self.assertTrue(args[0].endswith("mcp_compile.py"))
        self.assertEqual(args[1:], ["--url", "http://127.0.0.1:9/"])
        self.assertIn(backend_claude.COMPILE_TOOL, cmd[cmd.index("--allowedTools") + 1:])

    def test_protocol(self):
        a = mcp_compile.answer({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-06-18"}}, "u")
        self.assertEqual(a["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(mcp_compile.answer({"jsonrpc": "2.0", "method": "notifications/initialized"}, "u"))
        tools = mcp_compile.answer({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "u")
        self.assertEqual([t["name"] for t in tools["result"]["tools"]], ["compile"])

    def test_report_lists_errors_with_lines(self):
        text, failed = mcp_compile.report({"exit": 1, "engine": "pdflatex", "seconds": 2.1, "diagnostics": [
            {"severity": "error", "file": "main.tex", "line": 12, "message": "Undefined control sequence"},
            {"severity": "warning", "file": "main.tex", "line": 3, "message": "Reference `x' undefined"}]})
        self.assertTrue(failed)
        self.assertIn("1 error(s), 1 warning(s)", text)
        self.assertIn("- error: main.tex:12: Undefined control sequence", text)
        text, failed = mcp_compile.report({"exit": 0, "diagnostics": []})
        self.assertFalse(failed)
        self.assertTrue(text.startswith("Build OK."))
        text, _ = mcp_compile.report({"exit": 0, "diagnostics": [], "main": "notes/standalone.tex"})
        self.assertIn("Document: notes/standalone.tex.", text)


class ClaudeContext(unittest.TestCase):
    """How full the context window is: each message's usage, the window from modelUsage."""

    def test_used_and_window(self):
        cc, j, st = ClaudeCode("claude", {"bin": "claude"}), job_for(), {}
        msg = lambda inp, read, out: {"type": "assistant", "parent_tool_use_id": None, "message": {  # noqa: E731
            "content": [], "usage": {"input_tokens": inp, "cache_creation_input_tokens": 0,
                                     "cache_read_input_tokens": read, "output_tokens": out}}}
        cc.handle({"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"}, j, st)
        cc.handle(msg(10, 1000, 50), j, st)
        self.assertEqual(j.events[-1], {"t": "context", "used": 1060, "window": None})
        cc.handle({"type": "result", "usage": {}, "modelUsage": {
            "claude-haiku-4-5": {"contextWindow": 200000, "inputTokens": 5},
            "claude-sonnet-5-5": {"contextWindow": 1000000, "inputTokens": 10}}}, j, st)
        self.assertEqual(st["context"], {"used": 1060, "window": 1000000})
        # The next turn knows the window from its first message on.
        j2, st2 = job_for(), {}
        cc.handle({"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"}, j2, st2)
        cc.handle(msg(5, 2000, 10), j2, st2)
        self.assertEqual(j2.events[-1], {"t": "context", "used": 2015, "window": 1000000})


class ClaudeBilling(unittest.TestCase):
    """total_cost_usd is a list price: it is a real cost only with an API key."""

    def result(self, key_source):
        cc, j, st = ClaudeCode("claude", {"bin": "claude"}), job_for(), {}
        cc.handle({"type": "system", "subtype": "init", "session_id": "s",
                   "apiKeySource": key_source}, j, st)
        cc.handle({"type": "result", "subtype": "success", "total_cost_usd": 5.353,
                   "usage": {"input_tokens": 4, "cache_creation_input_tokens": 100,
                             "cache_read_input_tokens": 900, "output_tokens": 50}}, j, st)
        return st

    def test_subscription_shows_no_price(self):
        st = self.result("none")
        self.assertIsNone(st["cost"])
        self.assertEqual(st["billing"], "subscription")
        self.assertEqual(st["usage"], {"in": 1004, "out": 50})

    def test_api_key_shows_the_price(self):
        st = self.result("ANTHROPIC_API_KEY")
        self.assertEqual(st["cost"], 5.353)
        self.assertEqual(st["billing"], "api")


class CodexBackend(unittest.TestCase):
    def setUp(self):
        self.codex = Codex("codex", {"bin": "codex"})

    def test_native_file_tools_in_both_modes(self):
        # Codex reads files through its shell, not Claude's Read/Grep tools. A
        # blanket shell ban leaves it querying MCP resources and changing nothing.
        for mode in ("ask", "edit"):
            with self.subTest(mode=mode):
                cmd, prompt = self.codex.command(job_for(mode=mode, prompt="Read defs.tex"))
                self.assertNotIn("not shell commands such as", prompt)
                self.assertIn("read-only shell commands", prompt)
                self.assertIn("apply_patch", prompt)
                self.assertIn("Never commit, push", prompt)
                self.assertNotIn("--sandbox", cmd)
                self.assertIn('default_permissions="prism-editor"', cmd)

    def test_resumed_turn_refreshes_tool_rules_and_scope(self):
        message = "Read defs.tex\n\n[Scope for this turn] You may change ONLY main.tex."
        cmd, prompt = self.codex.command(job_for(mode="edit", prompt=message, session_id="t-old"))
        self.assertEqual(cmd[-3:], ["resume", "t-old", "-"])
        self.assertIn("replace earlier editor instructions", prompt)
        self.assertIn("read-only shell commands", prompt)
        self.assertNotIn("not shell commands such as", prompt)
        self.assertTrue(prompt.endswith(message), "the current scope must remain last")

    def test_command(self):
        cmd, prompt = self.codex.command(job_for(mode="ask", prompt="hi", model="gpt-5-codex",
                                                 effort="high"))
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertNotIn("--sandbox", cmd)
        self.assertIn('default_permissions="prism-editor"', cmd)
        self.assertIn("model_reasoning_effort=high", cmd)
        self.assertEqual(cmd[-1], "-", "the prompt comes on stdin")
        self.assertIn("prism-local", prompt, "a new conversation carries the editor's rules")
        cmd, prompt = self.codex.command(job_for(mode="edit", prompt="more", session_id="t-1"))
        self.assertNotIn("--sandbox", cmd)
        self.assertEqual(cmd[-3:], ["resume", "t-1", "-"])
        self.assertTrue(prompt.endswith("[Message]\nmore"))

    def test_events(self):
        root = Path(tempfile.gettempdir()).resolve()
        lines = [
            {"type": "thread.started", "thread_id": "t-9"},
            {"type": "turn.started"},
            {"type": "item.started", "item": {"id": "i1", "type": "command_execution",
                                              "command": "ls", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                                "command": "ls", "exit_code": 1,
                                                "aggregated_output": "boom", "status": "failed"}},
            {"type": "item.completed", "item": {"id": "i2", "type": "file_change",
                                                "changes": [{"path": str(root / "main.tex"),
                                                             "kind": "update"}],
                                                "status": "completed"}},
            {"type": "item.completed", "item": {"id": "i3", "type": "agent_message",
                                                "text": "Done."}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 3}},
        ]
        j, st = job_for(root=root), {}
        for d in lines:
            self.codex.handle(d, j, st)
        ev = j.events
        self.assertEqual(ev[0], {"t": "init", "session_id": "t-9", "model": None})
        self.assertEqual([e["t"] for e in ev[1:]],
                         ["tool", "tool_result", "tool", "tool_result", "message_start", "text"])
        self.assertTrue(ev[2]["error"])
        self.assertEqual(ev[3]["summary"], "main.tex")
        self.assertEqual(st["session_id"], "t-9")
        self.assertEqual(st["usage"], {"in": 10, "out": 3})


class DeepCodeBackend(unittest.TestCase):
    """Deep Code against a stand-in (tests/fake_deepcode.py) that keeps its sessions as the
    real CLI does; ~ is a temporary folder."""

    def setUp(self):
        from unittest import mock
        self.home = tmpdir()
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home), "DEEPSEEK_API_KEY": "sk-test"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("DEEPCODE_API_KEY", None)
        self.root = tmpdir()
        (self.root / "main.tex").write_text("original\n", encoding="utf-8")
        from backend_deepcode import DeepCode
        fake = Path(__file__).resolve().parent / "fake_deepcode.py"
        self.m = manager(self.root, lambda: ["main.tex"], deepcode=DeepCode("deepcode", {"bin": str(fake)}))

    def call(self):
        return json.loads((self.home / "call.json").read_text(encoding="utf-8"))

    def test_settings_a_turn_could_not_run_with_are_refused_before_it_starts(self):
        """Not a bare JSONDecodeError once the turn has started: the file, and what to do."""
        settings = self.root / ".deepcode" / "settings.json"
        settings.parent.mkdir()
        for text, said in (('{"env": ', "are not valid JSON"), ("[1, 2]", "must hold a JSON object")):
            settings.write_text(text, encoding="utf-8")
            r = self.m.start("go", None, "edit")
            self.assertNotIn("job", r)
            self.assertIn(str(settings), r["error"])
            self.assertIn(said, r["error"])
        settings.unlink()
        user = self.home / ".deepcode" / "settings.json"
        user.parent.mkdir()
        user.write_text(json.dumps({"mcpServers": {"search": {"command": "x"}}}), encoding="utf-8")
        r = self.m.start("go", None, "edit")
        self.assertIn("MCP servers", r["error"])
        self.assertIn("search", r["error"])
        self.assertFalse((self.home / "call.json").exists(), "no turn was started")

    def test_project_code_is_deep_codes(self):
        from backend_deepcode import project_code
        # Values from Deep Code's own getProjectCode (cli.js 0.4.3), run in node.
        if os.name == "nt":
            self.assertEqual(project_code(Path(r"C:\Users\me\paper")), "C-Users-me-paper")
        else:
            self.assertEqual(project_code(Path("/Users/me/paper")), "-Users-me-paper")
        if os.name == "nt":
            self.assertEqual(
                project_code(Path(r"D:\DynNum\paper\Sectorial Lattice Counting via the Non-Spherical Average")),
                "Sectorial-Lattice-Counting-via-the-Non-Spherica-fa3f3bd2796a1a84")

    def test_turns_resume_and_tools(self):
        done = wait_done(self.m.jobs[self.m.start("EDIT the intro", None, "edit")["job"]])
        evs = self.m.jobs[done["turn"]].events
        sid = next(e["session_id"] for e in evs if e["t"] == "init")
        self.assertEqual(done["session_id"], sid)
        self.assertIn({"t": "tool", "id": "call_1", "name": "write_file", "summary": "main.tex",
                       "path": "main.tex"}, evs)
        self.assertIn({"t": "text", "text": "Changed main.tex."}, evs)
        self.assertEqual([c["path"] for c in done["changed"]], ["main.tex"])
        c = self.call()
        self.assertIn("--exec", c["args"])
        self.assertIn("EDIT the intro", c["message"])
        self.assertIn("[Instructions from the editor]", c["message"], "the first message carries them")
        self.assertEqual(c["key"], "sk-test", "DEEPSEEK_API_KEY is passed on")
        # The next message continues that session.
        done = wait_done(self.m.jobs[self.m.start("and the abstract", sid, "edit", model="deepseek-v4-pro",
                                                  effort="max")["job"]])
        c = self.call()
        self.assertEqual(c["args"][-2:], ["--resume", sid])
        self.assertIn("[Instructions from the editor]", c["message"])
        self.assertEqual((c["model"], c["effort"]), ("deepseek-v4-pro", "max"))
        self.assertEqual(done["session_id"], sid)

    def test_tools_are_shown_before_the_cli_finishes_without_duplicates(self):
        # A just-finished chat is still in the session index while the new CLI starts.
        wait_done(self.m.jobs[self.m.start("previous chat", None, "ask")["job"]])
        release = self.root / "release-cli"
        with mock.patch.dict(os.environ, {"PRISM_TEST_DEEPCODE_RELEASE": str(release),
                                          "PRISM_TEST_DEEPCODE_DELAY": ".8"}):
            job = self.m.jobs[self.m.start("look at the paper", None, "ask")["job"]]
            try:
                deadline = time.monotonic() + 3
                while not any(e["t"] == "tool" for e in job.events) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertFalse(job.done)
                self.assertTrue(any(e["t"] == "tool" for e in job.events), "tools arrive while the CLI is still running")
                time.sleep(.7)                   # another session probe must not repeat them
                self.assertEqual(len([e for e in job.events if e["t"] == "tool"]), 1)
            finally:
                release.write_text("go")
                wait_done(job)
        self.assertEqual(len([e for e in job.events if e["t"] == "tool"]), 1)
        self.assertEqual(len([e for e in job.events if e["t"] == "tool_result"]), 1)

    def test_ask_is_read_only(self):
        done = wait_done(self.m.jobs[self.m.start("EDIT nothing, just explain", None, "ask")["job"]])
        self.assertEqual(done["reverted"], ["main.tex"])
        self.assertEqual(done["changed"], [])
        self.assertEqual((self.root / "main.tex").read_text(encoding="utf-8"), "original\n")
        self.assertIn("[Ask mode]", self.call()["message"])

    def test_unavailable_says_how_to_install(self):
        from backend_deepcode import DeepCode
        from unittest import mock
        with mock.patch("backend_deepcode.deepcode_bin", return_value=None):
            self.assertIn("npm install -g @vegamo/deepcode-cli", DeepCode("deepcode").unavailable())


class ActiveTurn(unittest.TestCase):
    def test_a_page_loaded_during_a_turn_learns_which_one_runs(self):
        """A page reloaded mid-turn follows the turn's events and can stop it."""
        with tempfile.TemporaryDirectory() as d:
            release = threading.Event()

            class Slow(backends.Backend):
                def run(self, job):
                    release.wait(10)
                    return {"exit": 0}

            m = manager(Path(d), slow=Slow("slow"))
            self.assertIsNone(m.info()["active"])
            job = m.start("go", None, "edit")["job"]
            self.assertEqual(m.info()["active"], {"job": job, "mode": "edit", "provider": "slow"})
            release.set()
            wait_done(m.jobs[job])
            self.assertIsNone(m.info()["active"])

    def test_shutdown_lets_the_stopped_turn_finish(self):
        """The server exits mid-turn: the turn is stopped and finishes (its changes recorded
        as the agent's) before the last commit, which would otherwise take them as yours."""
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.tex").write_text("a\n", encoding="utf-8")

            class Busy(backends.Backend):
                def run(self, job):
                    job.cancel.wait(10)
                    (job.root / "a.tex").write_text("half done\n", encoding="utf-8")
                    time.sleep(0.5)                 # winding down after Stop
                    return {"exit": 1}

            m = manager(Path(d), busy=Busy("busy"))
            recorded = []
            m.on_changed = lambda paths, why: recorded.append(paths)
            job = m.jobs[m.start("go", None, "edit")["job"]]
            m.shutdown()
            self.assertTrue(job.done)
            self.assertEqual(recorded, [["a.tex"]])


class BuildOutput(unittest.TestCase):
    def test_a_build_still_running_when_a_turn_ends_is_not_the_turns(self):
        """You compile during a turn, and the turn ends while the engine still writes the
        output folder: its files are neither the turn's changes nor reverted under it."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.tex").write_text("a\n", encoding="utf-8")
            (root / "build").mkdir()
            (root / "build/main.pdf").write_bytes(b"old pdf")

            class Wild(backends.Backend):         # like Deep Code: an Ask turn may write
                read_only_ask = False

                def run(self, job):
                    (job.root / "build/main.pdf").write_bytes(b"new pdf")   # the build's
                    (job.root / "build/main.aux").write_text("aux")
                    return {"exit": 0}

            for outdir, kept in (("build", True), (".", False)):
                with self.subTest(outdir=outdir):
                    (root / "build/main.pdf").write_bytes(b"old pdf")
                    m = agent.AgentManager(lambda: root, lambda: ["a.tex"],
                                           backends=({"wild": Wild("wild")}, "wild", None),
                                           output_fn=lambda: outdir)
                    done = wait_done(m.jobs[m.start("go", None, "ask")["job"]])
                    if kept:
                        self.assertEqual((done["changed"], done["reverted"]), ([], []))
                        self.assertEqual((root / "build/main.pdf").read_bytes(), b"new pdf")
                    else:       # output in the project itself: nothing is left out
                        self.assertIn("build/main.pdf", done["reverted"])


class RevertOutsideScope(unittest.TestCase):
    """A backend that cannot enforce the scope has its writes outside it undone."""

    def test_revert(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.tex").write_text("a\n", encoding="utf-8")
            (root / "b.tex").write_text("b\n", encoding="utf-8")

            class Wild(backends.Backend):
                enforces_scope = False

                def run(self, job):
                    (job.root / "a.tex").write_text("A\n", encoding="utf-8")
                    (job.root / "b.tex").write_text("B\n", encoding="utf-8")
                    return {"exit": 0}

            m = manager(root, lambda: ["a.tex", "b.tex"], wild=Wild("wild"))
            r = m.start("go", None, "edit", scope=["a.tex"])
            done = wait_done(m.jobs[r["job"]])
            self.assertEqual(done["reverted"], ["b.tex"])
            self.assertEqual([c["path"] for c in done["changed"]], ["a.tex"])
            self.assertEqual((root / "b.tex").read_text(encoding="utf-8"), "b\n")
            self.assertEqual((root / "a.tex").read_text(encoding="utf-8"), "A\n")

    def test_metadata_new_this_turn_is_named_and_left_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "a.tex").write_text("a\n", encoding="utf-8")
            (root / "old/.git").mkdir(parents=True)

            class Plant(backends.Backend):
                def run(self, job):
                    for rel in ("ch1/.git/config", "ch1/.Agents/skill.md", "old/.git/HEAD", "a.tex"):
                        (job.root / rel).parent.mkdir(parents=True, exist_ok=True)
                        (job.root / rel).write_text("x\n", encoding="utf-8")
                    return {"exit": 0}

            m = manager(root, lambda: ["a.tex"], plant=Plant("plant"))
            done = wait_done(m.jobs[m.start("go", None, "edit")["job"]])
            # Only what was not there before; nothing inside existing metadata.
            self.assertEqual(done["new_metadata"], ["ch1/.Agents", "ch1/.git"])
            self.assertTrue((root / "ch1/.git/config").exists())   # reported, not removed

            class Quiet(backends.Backend):
                def run(self, job):
                    return {"exit": 0}

            m = manager(root, lambda: ["a.tex"], quiet=Quiet("quiet"))
            self.assertEqual(wait_done(m.jobs[m.start("go", None, "ask")["job"]])["new_metadata"], [])


class EventBatches(unittest.TestCase):
    def test_a_waiting_page_receives_the_next_small_batch_together(self):
        job = job_for()
        job.events = [{"t": "delta", "text": "first"}]
        waits = []

        class Arriving:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def wait(self, timeout):
                waits.append(timeout)
                job.events.append({"t": "delta", "text": "second"})
                job.done = True

        job.cond = Arriving()
        events, done = job.wait_events(0, 1)
        self.assertEqual([e["text"] for e in events], ["first", "second"])
        self.assertTrue(done)
        self.assertGreater(waits[0], 0)
        self.assertLessEqual(waits[0], .040001)  # monotonic clock subtraction rounds at sub-microsecond scale
        waits.clear()
        job.wait_events(0, 0)
        self.assertEqual(waits, [], "a hidden page's non-waiting poll is immediate")


class Snapshots(unittest.TestCase):
    def test_stderr_tail_keeps_the_exact_end_of_an_oversized_chunk(self):
        tail = backends.Tail(limit=100)
        tail.add("old\n")
        tail.add("x" * 10000 + "last error")
        self.assertEqual("".join(tail), "x" * 90 + "last error")
        tail.add("\nnext")
        self.assertEqual("".join(tail), "x" * 85 + "last error\nnext")
        self.assertEqual(tail.size, 100)

    def test_undo_puts_back_the_exact_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.tex").write_bytes(b"one\r\ntwo\r\n")
            (root / "latin1.tex").write_bytes("caf\xe9\n".encode("latin-1"))   # not UTF-8

            class Edit(backends.Backend):
                def run(self, job):
                    (job.root / "a.tex").write_bytes(b"ONE\ntwo\n")
                    return {"exit": 0}

            m = manager(root, lambda: ["a.tex", "latin1.tex"], edit=Edit("edit"))
            r = m.start("go", None, "edit")
            done = wait_done(m.jobs[r["job"]])
            self.assertEqual([c["path"] for c in done["changed"]], ["a.tex"])
            self.assertEqual(list(m.turns[done["turn"]].before), ["a.tex"],
                             "only the changed files stay in memory")
            self.assertEqual(m.undo(done["turn"])["restored"], ["a.tex"])
            self.assertEqual((root / "a.tex").read_bytes(), b"one\r\ntwo\r\n")

    def test_memory_a_turn_keeps_is_bounded(self):
        """A CLI's stderr is kept to its end; Undo's history to MAX_UNDO_BYTES; and turn
        numbers grow across restarts, so a page never reads another turn's events."""
        tail = backends.Tail(limit=100)
        for i in range(10000):
            tail.add(f"line {i}\n")
        text = "".join(tail)
        self.assertLess(len(text), 200)
        self.assertTrue(text.endswith("line 9999\n"))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            written = itertools.count()

            class Big(backends.Backend):
                def run(self, job):
                    (job.root / f"t{next(written)}.dat").write_bytes(b"x" * 1000)
                    return {"exit": 0}

            m = manager(root, lambda: [], big=Big("big"))
            with mock.patch.object(agent, "MAX_UNDO_BYTES", 2500):
                turns = [wait_done(m.jobs[m.start(f"t{i}", None, "edit")["job"]])["turn"] for i in range(4)]
            self.assertEqual(list(m.turns), turns[-2:], "the oldest went once the history passed its size")
            later = manager(root, lambda: [], big=Big("big"))
            self.assertGreater(next(later.ids), turns[-1], "a restarted server numbers on")

    def test_no_undo_while_another_turn_runs(self):
        """What Undo wrote would be the running turn's change (its diff, its Undo)."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.tex").write_bytes(b"one\n")
            release = threading.Event()

            class Edit(backends.Backend):
                def run(self, job):
                    if job.prompt.startswith("first"):
                        (job.root / "a.tex").write_bytes(b"ONE\n")
                    else:
                        release.wait(10)
                    return {"exit": 0}

            m = manager(root, lambda: ["a.tex"], edit=Edit("edit"))
            first = wait_done(m.jobs[m.start("first", None, "edit")["job"]])
            second = m.jobs[m.start("second", None, "edit")["job"]]
            r = m.undo(first["turn"])
            self.assertTrue(r.get("busy"))
            self.assertIn("once this one has ended", r["error"])
            self.assertEqual((root / "a.tex").read_bytes(), b"ONE\n", "nothing was undone")
            release.set()
            self.assertEqual(wait_done(second)["changed"], [], "the second turn changed nothing")
            self.assertEqual(m.undo(first["turn"])["restored"], ["a.tex"], "after it, Undo works")

    def test_a_past_turn_keeps_only_what_undo_needs(self):
        """A turn's events (a written file streams its whole text) are kept while a page
        may still read them; for Undo, only the files the turn changed, as they were."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)

            class Write(backends.Backend):
                def run(self, job):
                    n = job.prompt.split()[0]
                    job.emit({"t": "tool_live", "id": n, "text": "x" * 100_000})
                    (job.root / f"f{n}.tex").write_text(n, encoding="utf-8")
                    return {"exit": 0}

            m = manager(root, write=Write("write"))
            turns = [wait_done(m.jobs[m.start(str(n), None, "edit")["job"]])["turn"] for n in range(agent.RECENT_JOBS + 3)]
            self.assertLessEqual(len(m.jobs), agent.RECENT_JOBS + 1)
            past = m.turns[turns[0]]
            self.assertFalse(hasattr(past, "events"), "no event log, process or prompt")
            self.assertEqual(list(past.after), ["f0.tex"])
            self.assertEqual(m.undo(turns[0])["restored"], ["f0.tex"])
            self.assertFalse((root / "f0.tex").exists())

    def test_undo_writes_holding_the_save_lock_and_commits_after(self):
        """Undo writes the files while holding the editor's save lock (a sync's merge holds it
        too: a merge that gives up would put back what it saw), and lets go of it before the
        commit, which waits for the sync."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.tex").write_bytes(b"one\n")

            class Edit(backends.Backend):
                def run(self, job):
                    (job.root / "a.tex").write_bytes(b"ONE\n")
                    return {"exit": 0}

            m = manager(root, lambda: ["a.tex"], edit=Edit("edit"))
            done = wait_done(m.jobs[m.start("go", None, "edit")["job"]])
            lock, seen = threading.RLock(), {}
            m.writes = lock
            real = agent.snapshot.restore

            def restore(*args):
                seen["restore"] = lock._is_owned()
                return real(*args)

            def free():                      # as a sync's merge, on its own thread
                seen["commit"] = lock.acquire(timeout=1)
                if seen["commit"]:
                    lock.release()

            def changed(paths, why):
                t = threading.Thread(target=free)
                t.start()
                t.join()

            m.on_changed = changed
            with mock.patch.object(agent.snapshot, "restore", restore):
                self.assertEqual(m.undo(done["turn"])["restored"], ["a.tex"])
            self.assertTrue(seen["restore"], "Undo writes holding the save lock")
            self.assertTrue(seen["commit"], "and lets go of it before the commit")
            self.assertEqual((root / "a.tex").read_bytes(), b"one\n")

    def test_what_the_editor_writes_during_a_turn_is_not_the_turns(self):
        """During an Ask turn you keep editing (saves), and a build writes its output folder:
        none of it is the turn's change. A backend without a read-only Ask mode has every
        write of the turn reverted: your saves and the build stay."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.tex").write_bytes(b"one\n")
            (root / "build").mkdir()
            (root / "build" / "main.pdf").write_bytes(b"%PDF old")
            m = None

            class NoReadOnlyAsk(backends.Backend):
                read_only_ask = False

                def run(self, job):
                    (job.root / "a.tex").write_bytes(b"one\nyours\n")      # you save, meanwhile
                    m.server_wrote(["a.tex"])
                    (job.root / "build" / "main.pdf").write_bytes(b"%PDF new")   # a build
                    (job.root / "build" / "main.log").write_bytes(b"log")
                    m.server_wrote(["build"])
                    (job.root / "notes.tex").write_bytes(b"the agent's\n")  # not allowed in Ask
                    return {"exit": 0}

            m = manager(root, lambda: ["a.tex"], ask=NoReadOnlyAsk("ask"))
            done = wait_done(m.jobs[m.start("go", None, "ask")["job"]])
            self.assertEqual([c["path"] for c in done["changed"]], [], done)
            self.assertEqual(done["reverted"], ["notes.tex"], "only the agent's write")
            self.assertEqual((root / "a.tex").read_bytes(), b"one\nyours\n", "your save stays")
            self.assertEqual((root / "build" / "main.pdf").read_bytes(), b"%PDF new", "the build stays")
            self.assertFalse((root / "notes.tex").exists())

    def test_an_edit_turn_and_the_editor_do_not_write_the_same_files(self):
        """While an Edit turn runs the editor holds your saves (editing()); a turn's Undo
        leaves alone what you saved after it."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.tex").write_bytes(b"one\n")
            seen = {}
            m = None

            class Edit(backends.Backend):
                def run(self, job):
                    seen["editing"] = m.editing()
                    (job.root / "a.tex").write_bytes(b"ONE\n")
                    return {"exit": 0}

            m = manager(root, lambda: ["a.tex"], edit=Edit("edit"))
            done = wait_done(m.jobs[m.start("go", None, "edit")["job"]])
            self.assertTrue(seen["editing"])
            self.assertFalse(m.editing(), "over once the turn is done")
            (root / "a.tex").write_bytes(b"ONE\nthen yours\n")
            self.assertEqual(m.undo(done["turn"])["restored"], [], "your text is kept")
            self.assertEqual((root / "a.tex").read_bytes(), b"ONE\nthen yours\n")

    def test_a_turn_ends_even_when_reporting_it_fails(self):
        """Whatever goes wrong after the backend (here the commit hook raising something
        unexpected, or the snapshot), the turn ends: busy() must not stay true, or sync and
        every later turn would wait for good."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.tex").write_bytes(b"one\n")

            class Edit(backends.Backend):
                def run(self, job):
                    (job.root / "a.tex").write_bytes(b"ONE\n")
                    return {"exit": 0}

            m = manager(root, lambda: ["a.tex"], edit=Edit("edit"))
            with mock.patch.object(agent.snapshot, "changes", side_effect=MemoryError("too big")):
                done = wait_done(m.jobs[m.start("go", None, "edit")["job"]])
            self.assertTrue(done.get("is_error"))
            self.assertFalse(m.busy())

    def cli(self, script: str):
        """A CliBackend running `script` (Python) as its CLI."""
        d = tmpdir()
        (d / "cli.py").write_text(script, encoding="utf-8")

        class Fake(backends.CliBackend):
            def command(self, job):
                return [sys.executable, str(d / "cli.py")], job.prompt

            def handle(self, ev, job, st):
                st.setdefault("seen", []).append(ev)
                if ev.get("unexpected"):
                    raise KeyError("a shape the handler did not expect")

        return Fake("fake"), d

    def test_a_cli_whose_output_breaks_the_handler_is_ended(self):
        """A line the handler does not expect raises: the CLI must not go on writing the
        files after its turn has ended."""
        backend, d = self.cli("import json, time, sys\n"
                              "print(json.dumps({'unexpected': True}), flush=True)\n"
                              "time.sleep(30)\n"
                              "open('late.txt', 'w').write('written after the turn')\n")
        job = job_for(root=d, prompt="go")
        with self.assertRaises(KeyError):
            backend.run(job)
        self.assertIsNotNone(job.proc.poll(), "the CLI is ended")

    def test_a_command_left_holding_the_output_does_not_hold_the_turn(self):
        """The CLI exits, but a command it started (a background shell) still holds its
        output open: the turn ends a few seconds after the CLI, with what it printed."""
        backend, d = self.cli(
            "import json, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "open('child.pid', 'w').write(str(child.pid))\n"
            "print(json.dumps({'type': 'result'}), flush=True)\n")
        self.addCleanup(lambda: (d / "child.pid").exists() and kill_pid(int((d / "child.pid").read_text())))
        job = job_for(root=d, prompt="go")
        t0 = time.time()
        st = backend.run(job)
        self.assertLess(time.time() - t0, backends.OUTPUT_GRACE + 8)
        self.assertEqual((st["exit"], st["seen"]), (0, [{"type": "result"}]))
        self.assertTrue(gone(int((d / "child.pid").read_text())), "the command it left still runs")

    def test_stop_ends_a_turn_whose_cli_has_already_exited(self):
        backend, d = self.cli(
            "import subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "open('child.pid', 'w').write(str(child.pid))\n")
        self.addCleanup(lambda: (d / "child.pid").exists() and kill_pid(int((d / "child.pid").read_text())))
        job = job_for(root=d, prompt="go")
        threading.Timer(1.5, backend.stop, (job,)).start()
        t0 = time.time()
        backend.run(job)
        self.assertLess(time.time() - t0, min(backends.OUTPUT_GRACE, 5) + 3)
        self.assertTrue(gone(int((d / "child.pid").read_text())), "the command it left still runs")

    def test_stop_before_the_cli_starts_starts_none(self):
        backend, d = self.cli("open('started.txt', 'w').write('x')\n")
        job = job_for(root=d, prompt="go")
        backend.stop(job)                       # pressed while the turn was being set up
        self.assertEqual(backend.run(job), {})
        self.assertIsNone(job.proc)
        self.assertFalse((d / "started.txt").exists())

    def turn_of(self, backend, d, stop_after=None, timeout=20):
        """A whole turn of `backend` through the manager: its done event and its events."""
        m = manager(d, lambda: [], **{backend.id: backend})
        jid = m.start("go", None, "ask")["job"]
        if stop_after is not None:
            threading.Timer(stop_after, m.stop, (jid,)).start()
        done = wait_done(m.jobs[jid], timeout)
        return done, m.jobs[jid].events

    def test_how_a_turn_ended_is_decided_once_for_every_backend(self):
        """Stop is "stopped", with no error for what stopping broke; a failure is said
        once, with what the CLI printed; a turn that went well is "ok"."""
        stoppable, d = self.cli("import sys, time\nprint('{}', flush=True)\ntime.sleep(30)\n")
        done, events = self.turn_of(stoppable, d, stop_after=1.5)
        self.assertEqual((done["outcome"], done["error"]), ("stopped", None))
        self.assertFalse([e for e in events if e["t"] == "error"])

        failing, d = self.cli("import sys\nsys.stderr.write('error: not logged in\\n')\nsys.exit(3)\n")
        done, events = self.turn_of(failing, d)
        self.assertEqual(done["outcome"], "failed")
        self.assertIn("exit 3", done["error"])
        self.assertIn("not logged in", done["error"])

        fine, d = self.cli("print('{}')\n")
        self.assertEqual(self.turn_of(fine, d)[0]["outcome"], "ok")

    def test_a_failure_an_error_event_told_is_not_told_again(self):
        class Told(backends.Backend):
            def run(self, job):
                job.emit({"t": "error", "message": "Codex: turn failed"})
                return {"is_error": True, "subtype": "turn failed", "stderr": "the same, again"}

        done, events = self.turn_of(Told("told"), tmpdir())
        self.assertEqual((done["outcome"], done["error"]), ("failed", None))
        self.assertEqual([e["message"] for e in events if e["t"] == "error"], ["Codex: turn failed"])

        class BrokenByStop(backends.Backend):       # the API's reader, its response closed under it
            def run(self, job):
                job.cancel.wait(10)
                job.emit({"t": "error", "message": "AttributeError: 'NoneType' object has no attribute 'peek'"})
                return {"is_error": True, "subtype": "API error"}

        done, events = self.turn_of(BrokenByStop("api"), tmpdir(), stop_after=0.5)
        self.assertEqual((done["outcome"], done["error"]), ("stopped", None))
        self.assertFalse([e for e in events if e["t"] == "error"])

    def test_a_cli_that_exits_at_once_says_why(self):
        """The CLI exits before reading its input (a bad flag, no login): writing the prompt
        to it fails, and what it printed on stderr is what the author needs."""
        backend, d = self.cli("import sys\nsys.stderr.write('error: not logged in\\n')\nsys.exit(3)\n")
        job = job_for(root=d, prompt="x" * 2_000_000)
        st = backend.run(job)
        self.assertEqual(st["exit"], 3)
        self.assertIn("not logged in", st["stderr"])

    def test_stderr_without_newlines_is_drained_in_bounded_chunks(self):
        backend, d = self.cli("import sys\nsys.stderr.write('x' * 100000 + 'last error')\nsys.exit(3)\n")
        lengths = []
        add = backends.Tail.add

        def record(tail, text):
            lengths.append(len(text))
            add(tail, text)

        with mock.patch.object(backends.Tail, "add", record):
            st = backend.run(job_for(root=d, prompt="go"))
        self.assertEqual(st["exit"], 3)
        self.assertTrue(st["stderr"].endswith("last error"))
        self.assertTrue(lengths)
        self.assertLessEqual(max(lengths), 8000)

    def test_tool_events_have_one_form_whatever_the_cli_printed(self):
        """A CLI's output of a shape not expected (a result that is text, not an object; a
        list that is not one) is shown as it can be: the handler raising would end the turn."""
        job, st, codex = job_for(root=Path(".")), {}, Codex("codex")
        for item in ({"type": "mcp_tool_call", "id": 1, "result": "text, not an object"},
                     {"type": "mcp_tool_call", "id": "2", "result": {"content": "not a list"}},
                     {"type": "file_change", "id": "3", "changes": "not a list"},
                     {"type": "command_execution", "id": "4", "command": "ls", "exit_code": 2,
                      "aggregated_output": "x" * 1000}):
            codex.handle({"type": "item.completed", "item": item}, job, st)
        tools = [e for e in job.events if e["t"] == "tool"]
        results = [e for e in job.events if e["t"] == "tool_result"]
        self.assertEqual([e["id"] for e in tools], ["1", "2", "3", "4"])
        self.assertTrue(all(isinstance(e["preview"], str) and len(e["preview"]) <= 300 for e in results))
        self.assertEqual([e["error"] for e in results], [False, False, False, True])

    def test_codex_reasoning_is_shown_as_thinking(self):
        """Long reasoning showed only a static "working…": it is the thinking box now."""
        job, st, codex = job_for(root=Path(".")), {}, Codex("codex")
        codex.handle({"type": "item.started", "item": {"type": "reasoning", "id": "r1"}}, job, st)
        codex.handle({"type": "item.completed", "item": {"type": "reasoning", "id": "r1",
                                                          "text": "First, read main.tex."}}, job, st)
        codex.handle({"type": "item.completed", "item": {"type": "reasoning", "id": "r2",
                                                          "text": "Then the proof."}}, job, st)
        self.assertEqual([(e["t"], e.get("text")) for e in job.events],
                         [("thinking_start", None), ("thinking", "First, read main.tex."),
                          ("thinking_start", None), ("thinking", "Then the proof.")])

    def test_undo_while_git_holds_the_files_says_busy(self):
        """git holding the files long (a repository's slow hook): Undo says so after a moment,
        changes nothing, and works when tried again."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.tex").write_bytes(b"one\n")

            class Edit(backends.Backend):
                def run(self, job):
                    (job.root / "a.tex").write_bytes(b"ONE\n")
                    return {"exit": 0}

            m = manager(root, lambda: ["a.tex"], edit=Edit("edit"))
            done = wait_done(m.jobs[m.start("go", None, "edit")["job"]])
            m.writes, held, done_holding = threading.RLock(), threading.Event(), threading.Event()

            def git_holding():
                with m.writes:
                    held.set()
                    done_holding.wait(10)

            t = threading.Thread(target=git_holding)
            t.start()
            held.wait(5)
            with mock.patch.object(agent, "UNDO_WAIT", 0.2):
                r = m.undo(done["turn"])
            done_holding.set()
            t.join()
            self.assertTrue(r.get("busy"), r)
            self.assertEqual((root / "a.tex").read_bytes(), b"ONE\n", "nothing changed")
            self.assertEqual(m.undo(done["turn"])["restored"], ["a.tex"])
            self.assertEqual((root / "a.tex").read_bytes(), b"one\n")


def wait_done(job, timeout=10):
    """The turn's done event. Waits for new events only (asking from 0 again would return
    at once and spin, taking the time the turn's own thread needs)."""
    end, seen = time.time() + timeout, []
    while time.time() < end:
        evs, done = job.wait_events(len(seen), 0.5)
        seen += evs
        if done:
            return next(e for e in seen if e["t"] == "done")
    raise AssertionError("turn did not finish")


class FakeAPI(ThreadingHTTPServer):
    """Answers /chat/completions with the next scripted list of SSE chunks."""

    def __init__(self, script):
        self.script, self.requests = list(script), []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(h):
                body = json.loads(h.rfile.read(int(h.headers["Content-Length"])))
                self.requests.append({"body": body, "auth": h.headers.get("Authorization")})
                chunks = self.script.pop(0)
                if isinstance(chunks, (int, tuple)):          # a status, or (status, message)
                    code, message = chunks if isinstance(chunks, tuple) else (chunks, "bad key")
                    h.send_response(code)
                    h.end_headers()
                    h.wfile.write(json.dumps({"error": {"message": message}}).encode())
                    return
                h.send_response(200)
                h.send_header("Content-Type", "text/event-stream")
                h.end_headers()
                try:
                    for c in chunks:
                        if isinstance(c, (int, float)):          # a pause in the stream
                            time.sleep(c)
                            continue
                        h.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                        h.wfile.flush()
                    h.wfile.write(b"data: [DONE]\n\n")
                except OSError:                                   # the client went (Stop)
                    pass

        super().__init__(("127.0.0.1", 0), H)
        threading.Thread(target=self.serve_forever, daemon=True).start()


def call_chunks(cid, name, args):
    a = json.dumps(args)
    return [{"choices": [{"delta": {"reasoning_content": "thinking…"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": cid, "function":
                                                    {"name": name, "arguments": a[:5]}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function":
                                                    {"arguments": a[5:]}}]}}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 7}}]


class OpenAICompatible(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "main.tex").write_text("Hello wrold.\n", encoding="utf-8")
        (self.root / "other.tex").write_text("x\n", encoding="utf-8")
        os.environ["PRISM_TEST_KEY"] = "sk-test"

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("PRISM_TEST_KEY", None)

    def run_turn(self, script, prompt="fix the typo", scope=None, mode="edit", session=None,
                 backend=None):
        api = FakeAPI(script)
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        be = backend or OpenAICompat("ds", {"base_url": f"http://127.0.0.1:{api.server_port}",
                                            "api_key_env": "PRISM_TEST_KEY",
                                            "models": ["deepseek-chat"]})
        be.base_url = f"http://127.0.0.1:{api.server_port}"
        m = manager(self.root, lambda: ["main.tex", "other.tex"], ds=be)
        m.writable_fn = lambda rel: None
        r = m.start(prompt, session, mode, scope=scope)
        self.assertIn("job", r, r)
        job = m.jobs[r["job"]]
        return wait_done(job), job.events, api, be

    def test_tool_loop_edits_and_streams(self):
        done, events, api, be = self.run_turn([
            call_chunks("c1", "read_file", {"path": "main.tex"}),
            call_chunks("c2", "edit_file", {"path": "main.tex", "old_string": "wrold",
                                            "new_string": "world"}),
            [{"choices": [{"delta": {"content": "Fixed "}}]},
             {"choices": [{"delta": {"content": "the typo."}}]}],
        ])
        self.assertEqual((self.root / "main.tex").read_text(encoding="utf-8"), "Hello world.\n")
        self.assertEqual([c["path"] for c in done["changed"]], ["main.tex"])
        self.assertEqual("".join(e["text"] for e in events if e["t"] == "delta"), "Fixed the typo.")
        self.assertEqual([e["name"] for e in events if e["t"] == "tool"], ["Read", "Edit"])
        self.assertFalse(any(e["error"] for e in events if e["t"] == "tool_result"))
        self.assertEqual(done["usage"], {"in": 200, "out": 14})
        first, second = api.requests[0], api.requests[1]
        self.assertEqual(first["auth"], "Bearer sk-test")
        self.assertEqual(first["body"]["model"], "deepseek-chat")
        self.assertIn("edit_file", [t["function"]["name"] for t in first["body"]["tools"]])
        # Reasoning goes back while tools are being called, and is dropped afterwards.
        self.assertEqual(second["body"]["messages"][-2]["reasoning_content"], "thinking…")
        sid = done["session_id"]
        self.assertFalse(any("reasoning_content" in m for m in be.sessions[sid]))
        # The next message continues the same conversation.
        done2, _, api2, _ = self.run_turn([[{"choices": [{"delta": {"content": "ok"}}]}]],
                                          prompt="thanks", session=sid, backend=be)
        msgs = api2.requests[0]["body"]["messages"]
        self.assertEqual(msgs[-1]["content"][:6], "thanks")
        self.assertEqual(len(msgs), 8)   # system, user, 2×(assistant, tool), assistant, user

    def test_scope_and_ask_mode_block_writes(self):
        done, events, _, _ = self.run_turn(
            [call_chunks("c1", "write_file", {"path": "other.tex", "content": "y\n"}),
             [{"choices": [{"delta": {"content": "Cannot."}}]}]], scope=["main.tex"])
        self.assertEqual((self.root / "other.tex").read_text(encoding="utf-8"), "x\n")
        self.assertEqual(done["denials"], ["Write"])
        self.assertTrue(next(e for e in events if e["t"] == "tool_result")["error"])
        _, _, api, _ = self.run_turn([[{"choices": [{"delta": {"content": "Read only."}}]}]],
                                     mode="ask")
        self.assertNotIn("write_file", [t["function"]["name"] for t in api.requests[0]["body"]["tools"]])

    def test_paths_stay_in_project(self):
        be = OpenAICompat("ds", {"base_url": "http://x"})
        j = job_for(root=self.root, mode="edit", writable=lambda rel: True)
        for bad in ("../x.tex", "/etc/passwd", "C:/Windows/win.ini", ".git/config"):
            with self.assertRaises(Exception, msg=bad):
                be.tool(j, "read_file", {"path": bad}, [])

    def test_http_error_is_reported(self):
        done, _, _, be = self.run_turn([401])
        self.assertTrue(done["is_error"])
        self.assertIn("HTTP 401", done["stderr"])
        self.assertEqual(be.sessions, {}, "a failed request leaves no half conversation")

    def test_stop_ends_a_streaming_turn_at_once_and_keeps_the_conversation(self):
        """Closing the response under its reader waited for the next chunk and then raised
        in it; the conversation, never saved, was gone for the next message."""
        api = FakeAPI([[{"choices": [{"delta": {"content": "Part "}}]}, 20,
                        {"choices": [{"delta": {"content": "never"}}]}]])
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        be = OpenAICompat("ds", {"base_url": f"http://127.0.0.1:{api.server_port}",
                                 "api_key_env": "PRISM_TEST_KEY", "models": ["deepseek-chat"]})
        m = manager(self.root, lambda: ["main.tex", "other.tex"], ds=be)
        job = m.jobs[m.start("write a lot", None, "ask")["job"]]
        end = time.time() + 10
        while not any(e["t"] == "delta" for e in job.events) and time.time() < end:
            time.sleep(0.05)
        t0 = time.time()
        m.stop(job.id)
        done = wait_done(job, 15)
        self.assertLess(time.time() - t0, 3, "Stop waited for the stream")
        self.assertEqual((done["outcome"], done["error"]), ("stopped", None))
        self.assertFalse([e for e in job.events if e["t"] == "error"])
        self.assertIn(done["session_id"], be.sessions, "the conversation is kept")
        done2, _, api2, _ = self.run_turn([[{"choices": [{"delta": {"content": "again"}}]}]],
                                          prompt="go on", session=done["session_id"], backend=be)
        self.assertEqual(done2["outcome"], "ok")
        self.assertIn("write a lot", json.dumps(api2.requests[0]["body"]["messages"]))

    def test_a_tool_that_raises_is_an_error_for_the_model_not_the_end_of_the_turn(self):
        (self.root / "latin1.tex").write_bytes("caf\xe9\n".encode("latin-1"))   # not UTF-8
        done, events, _, _ = self.run_turn([
            call_chunks("c1", "read_file", {"path": "main.tex", "offset": "not a number"}),
            call_chunks("c2", "edit_file", {"path": "latin1.tex", "old_string": "caf", "new_string": "x"}),
            [{"choices": [{"delta": {"content": "Could not."}}]}]])
        self.assertEqual(done["outcome"], "ok")
        results = [e for e in events if e["t"] == "tool_result"]
        self.assertEqual([r["error"] for r in results], [True, True])
        self.assertIn("ValueError", results[0]["preview"])

    def test_a_conversation_too_long_says_so(self):
        done, _, _, _ = self.run_turn([(400, "This model's maximum context length is 65536 tokens.")])
        self.assertEqual(done["outcome"], "failed")
        self.assertIn("too long for the model: start a new chat", done["error"])

    def test_missing_key(self):
        os.environ.pop("PRISM_TEST_KEY", None)
        be = OpenAICompat("ds", {"base_url": "http://x", "api_key_env": "PRISM_TEST_KEY",
                                 "models": ["m"]})
        self.assertIn("PRISM_TEST_KEY", manager(ds=be).start("hi", None, "ask")["error"])


class Registry(unittest.TestCase):
    def test_presets_and_user_settings(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "agents.json"
            p.write_text(json.dumps({"default": "deepseek", "providers": {
                "deepseek": {"default_model": "deepseek-reasoner"},
                "ollama": {"enabled": False},
                "vllm": {"type": "openai", "label": "My vLLM", "base_url": "http://gpu:8000/v1",
                         "models": ["qwen3-32b"]}}}), encoding="utf-8")
            old = os.environ.pop("PRISM_AGENT", None)
            try:
                bs, default, err = backends.load_backends(p)
            finally:
                if old is not None:
                    os.environ["PRISM_AGENT"] = old
        self.assertIsNone(err)
        self.assertEqual(default, "deepseek")
        self.assertIn("claude", bs)
        self.assertIn("codex", bs)
        self.assertNotIn("ollama", bs)
        self.assertEqual(bs["deepseek"].default_model, "deepseek-reasoner")
        self.assertEqual(bs["deepseek"].base_url, "https://api.deepseek.com")
        self.assertEqual(bs["vllm"].default_model, "qwen3-32b")
        self.assertIsNone(bs["vllm"].unavailable(), "no key needed without api_key_env")

    def test_bad_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "agents.json"
            p.write_text("{nope", encoding="utf-8")
            bs, default, err = backends.load_backends(p)
        self.assertIn("agents.json", err)
        self.assertIn("claude", bs)


class FakeInit:
    """The `claude -p` that commands() starts: its init event, or nothing (`lines` empty)."""

    def __init__(self, lines):
        self.stdin = open(os.devnull, "w")
        self.stdout = iter(lines)


class ClaudeRemembers(unittest.TestCase):
    """The command list and the login are not looked up again while nothing they depend
    on changed (backend_claude)."""
    INIT = json.dumps({"type": "system", "subtype": "init", "skills": ["proof"],
                       "slash_commands": ["review"], "claude_code_version": "2.0"}) + "\n"

    def setUp(self):
        from unittest import mock
        no_account_lock(self)
        self.root, self.cfg = tmpdir(), tmpdir()
        self.exe = self.cfg / "claude"
        self.exe.write_text("")
        for p in (mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.cfg)}),
                  mock.patch.object(backend_claude, "use_config_dir"),
                  mock.patch.object(backend_claude, "kill_tree")):
            p.start()
            self.addCleanup(p.stop)
        self.claude = ClaudeCode("claude", {"bin": str(self.exe)})

    def lookups(self, lines, *calls):
        """How many times `claude` was started for commands(**kw) of each of `calls`."""
        from unittest import mock
        with mock.patch.object(backend_claude, "start", side_effect=lambda *a, **k: FakeInit(lines)) as popen:
            answers = [self.claude.commands(self.root, **kw) for kw in calls]
        return popen.call_count, answers

    def test_the_command_list_is_kept_until_a_skill_folder_changes(self):
        n, answers = self.lookups([self.INIT], {}, {})
        self.assertEqual(n, 1)
        self.assertEqual(answers[1]["skills"], ["proof"])
        (self.root / ".claude" / "skills" / "new").mkdir(parents=True)
        self.assertEqual(self.lookups([self.INIT], {}, {})[0], 1, "a new skill: looked up again")
        (self.cfg / "commands").mkdir()
        self.assertEqual(self.lookups([self.INIT], {}, {"refresh": True})[0], 2,
                         "your own commands too; and refresh always asks")

    def test_a_failed_lookup_is_not_repeated_for_a_while(self):
        n, answers = self.lookups([], {}, {}, {})
        self.assertEqual(n, 1, "every '/' typed asks; claude starts once")
        self.assertEqual(answers, [{"error": "Claude Code did not report its commands"}] * 3)
        key, at, answer = self.claude.catalog_failed
        self.claude.catalog_failed = (key, at - backend_claude.CATALOG_RETRY, answer)
        n, answers = self.lookups([self.INIT], {}, {})
        self.assertEqual((n, answers[1]["commands"]), (1, ["review"]), "a minute later: again")

    def test_the_login_is_asked_again_when_a_login_file_changes(self):
        from unittest import mock
        status = subprocess_done('{"loggedIn": true, "email": "me@uni.example"}')
        with mock.patch.object(backend_claude, "run_bounded", return_value=status) as run:
            for _ in range(2):
                self.assertEqual(self.claude.account(self.root)["account"]["email"], "me@uni.example")
            self.assertEqual(run.call_count, 1)
            (self.cfg / ".credentials.json").write_text("{}")      # a login
            self.claude.account(self.root)
            self.assertEqual(run.call_count, 2)
            self.claude.account(self.root, fresh=True)
            self.assertEqual(run.call_count, 3, "a turn always asks")


def subprocess_done(stdout):
    import subprocess
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


class ClaudeAccountGuard(unittest.TestCase):
    """Only the account allowed in Home -> Settings may run turns (backend_claude)."""
    OK = {"logged_in": True, "email": "me@uni.example", "org": "Lab", "auth_method": "claude.ai",
          "api_provider": "firstParty", "overrides": []}

    def setUp(self):
        import backend_claude
        self.bc = backend_claude

    def test_no_allowed_account_means_no_check(self):
        self.assertIsNone(self.bc.account_problem({"logged_in": False}, ""))

    def test_matching_account_passes_case_insensitively(self):
        self.assertIsNone(self.bc.account_problem(self.OK, "Me@Uni.Example"))

    def test_refusals(self):
        allowed = "me@uni.example"
        cases = {
            "another account": {**self.OK, "email": "other@example.com"},
            "API key": {**self.OK, "overrides": ["the environment variable ANTHROPIC_API_KEY"]},
            "console login": {**self.OK, "auth_method": "console"},
            "bedrock": {**self.OK, "api_provider": "bedrock"},
            "logged out": {"logged_in": False, "overrides": []},
            "check failed": {"logged_in": False, "error": "boom", "overrides": []},
        }
        for name, info in cases.items():
            msg = self.bc.account_problem(info, allowed)
            self.assertTrue(msg, name)
            self.assertIn("me@uni.example", msg, name)

    def test_probes_are_checked_like_turns(self):
        """The usage probe and the command list start claude only for the allowed account."""
        from unittest import mock
        claude = ClaudeCode("claude", {"bin": "claude"})
        m = manager(claude=claude)
        refusal = "Claude Code is logged in as other@example.com, not me@uni.example."
        with mock.patch.object(claude, "preflight", return_value=refusal), \
                mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            self.assertEqual(m.probe_rate("claude")["error"], refusal)
            self.assertEqual(m.commands("claude", refresh=True)["error"], refusal)
            run.assert_not_called()
            popen.assert_not_called()

    def test_project_settings_that_switch_to_an_api_key_are_found(self):
        import json, tempfile
        root = tmpdir()
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.json").write_text(json.dumps({"apiKeyHelper": "get-key.sh"}))
        (root / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "x"}}))
        found = self.bc.auth_overrides(root)
        self.assertTrue(any("apiKeyHelper" in f for f in found))
        self.assertTrue(any("ANTHROPIC_API_KEY" in f and "settings.local.json" in f for f in found))


if __name__ == "__main__":
    unittest.main()
