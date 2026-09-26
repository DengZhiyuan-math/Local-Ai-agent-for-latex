"""Unit tests for the agent manager and its backends (prism_local/agent.py, backend_*.py).

No real CLI or API is started: CLI backends are checked through the command they
would run and the events they make from sample output, and the OpenAI-compatible
backend runs against a fake chat-completions server on 127.0.0.1.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import agent  # noqa: E402
import backends  # noqa: E402
from backend_claude import ClaudeCode  # noqa: E402
from backend_codex import Codex  # noqa: E402
from backend_openai import OpenAICompat  # noqa: E402


def manager(root=Path("."), files=lambda: [], **kinds):
    """An AgentManager whose backends are exactly `kinds` (id -> Backend)."""
    return agent.AgentManager(lambda: Path(root), files, backends=(kinds, next(iter(kinds)), None))


def job_for(**kw):
    j = backends.Job(1)
    for k, v in kw.items():
        setattr(j, k, v)
    return j


class ModelAndEffort(unittest.TestCase):
    def setUp(self):
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


class CodexBackend(unittest.TestCase):
    def setUp(self):
        self.codex = Codex("codex", {"bin": "codex"})

    def test_command(self):
        cmd, prompt = self.codex.command(job_for(mode="ask", prompt="hi", model="gpt-5-codex",
                                                 effort="high"))
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertIn("model_reasoning_effort=high", cmd)
        self.assertEqual(cmd[-1], "-", "the prompt comes on stdin")
        self.assertIn("prism-local", prompt, "a new conversation carries the editor's rules")
        cmd, prompt = self.codex.command(job_for(mode="edit", prompt="more", session_id="t-1"))
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(cmd[-3:], ["resume", "t-1", "-"])
        self.assertEqual(prompt, "more")

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


def wait_done(job, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        evs, done = job.wait_events(0, 0.5)
        if done:
            return next(e for e in evs if e["t"] == "done")
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
                if isinstance(chunks, int):
                    h.send_response(chunks)
                    h.end_headers()
                    h.wfile.write(b'{"error": {"message": "bad key"}}')
                    return
                h.send_response(200)
                h.send_header("Content-Type", "text/event-stream")
                h.end_headers()
                for c in chunks:
                    h.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                h.wfile.write(b"data: [DONE]\n\n")

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
        root = Path(tempfile.mkdtemp())
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.json").write_text(json.dumps({"apiKeyHelper": "get-key.sh"}))
        (root / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "x"}}))
        found = self.bc.auth_overrides(root)
        self.assertTrue(any("apiKeyHelper" in f for f in found))
        self.assertTrue(any("ANTHROPIC_API_KEY" in f and "settings.local.json" in f for f in found))


if __name__ == "__main__":
    unittest.main()
