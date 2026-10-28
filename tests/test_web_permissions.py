"""Real subprocess protocol round trips; no service/model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from backend_claude import ClaudeCode
from backends import Job


FAKE_CLI = '''import json,sys
init=json.loads(sys.stdin.readline())
print(json.dumps({"type":"control_response","response":{"subtype":"success","request_id":init["request_id"],"response":{}}}),flush=True)
user=json.loads(sys.stdin.readline())
assert user["message"]["content"]=="Research this"
print(json.dumps({"type":"system","subtype":"init","session_id":"chat-1"}),flush=True)
for i in range(2):
 print(json.dumps({"type":"control_request","request_id":str(i),"request":{"subtype":"can_use_tool","tool_name":"WebFetch","input":{"url":"https://example.com/page","prompt":"Read"}}}),flush=True)
 response=json.loads(sys.stdin.readline())["response"]
 assert response["request_id"]==str(i)
 assert response["response"]["behavior"]=="allow"
print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"Continued after approval"}]}}),flush=True)
print(json.dumps({"type":"result","session_id":"chat-1","is_error":False}),flush=True)
'''


class PermissionRoundTrip(unittest.TestCase):
    def test_approval_continues_same_subprocess_and_session_grant_avoids_repeat(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"PRISM_STATE_DIR": td}):
            root = Path(td)
            script = root / "fake.py"
            script.write_text(FAKE_CLI)
            cc = ClaudeCode("claude", {"bin": sys.executable})
            cc.command = lambda j: ([sys.executable, str(script)], j.prompt)
            j = Job(7)
            j.root, j.prompt = root, "Research this"
            result = {}
            thread = threading.Thread(target=lambda: result.update(cc.run(j)))
            thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not any(e["t"] == "permission" for e in j.events):
                time.sleep(.01)
            prompts = [e for e in j.events if e["t"] == "permission"]
            try:
                self.assertEqual(len(prompts), 1, j.events)
                self.assertEqual(prompts[0]["target"], "https://example.com")
                self.assertEqual(cc.permissions.resolve(j, "0", "allow", "session"), {"ok": True})
                thread.join(5)
                self.assertFalse(thread.is_alive(), "approval must resume original subprocess")
                self.assertEqual(result["exit"], 0, result)
                self.assertEqual(len([e for e in j.events if e["t"] == "permission"]), 1)
            finally:
                cc.stop(j)
                thread.join(3)


class GrantBoundaries(unittest.TestCase):
    def setUp(self):
        from permissions import WebPermissions
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"PRISM_STATE_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.broker = WebPermissions()
        self.job = Job(1)
        self.job.root = Path(self.tmp.name)

    def start(self, tool="WebSearch", inp=None, session="chat-1", namespace="project-a"):
        self.answer = {}
        inp = inp or {"query": "LaTeX references"}
        thread = threading.Thread(target=lambda: self.answer.update(
            self.broker.request(self.job, "req", tool, inp, session, namespace)))
        thread.start()
        self.addCleanup(lambda: (self.broker.cancel(self.job), thread.join(2)))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not self.job.events and thread.is_alive():
            time.sleep(.01)
        return thread

    def test_once_grant_is_consumed_and_stale_decision_is_rejected(self):
        thread = self.start()
        self.assertEqual(self.broker.resolve(self.job, "req", "allow", "once"), {"ok": True})
        thread.join(2)
        self.assertEqual(self.answer["behavior"], "allow")
        self.assertIn("error", self.broker.resolve(self.job, "req", "allow", "once"))
        self.job.events.clear()
        thread = self.start()
        self.assertTrue(thread.is_alive(), "once must prompt again")
        self.assertEqual(self.broker.resolve(self.job, "req", "deny", "once"), {"ok": True})
        thread.join(2)
        self.assertEqual(self.answer["behavior"], "deny")

    def test_persistent_fetch_grant_survives_broker_restart_but_not_project_or_origin_change(self):
        from permissions import WebPermissions
        inp = {"url": "https://example.com/one", "prompt": "read"}
        thread = self.start("WebFetch", inp)
        self.assertEqual(self.broker.resolve(self.job, "req", "allow", "persistent"), {"ok": True})
        thread.join(2)
        self.broker = WebPermissions()
        answer = self.broker.request(self.job, "next", "WebFetch",
                                     {"url": "https://example.com/two"}, "chat-2", "project-a")
        self.assertEqual(answer["behavior"], "allow")
        for namespace, url in (("project-b", "https://example.com/one"),
                               ("project-a", "https://other.example/one")):
            self.job.events.clear()
            thread = self.start("WebFetch", {"url": url}, namespace=namespace)
            self.assertTrue(thread.is_alive(), "grant must stay within its project and origin")
            self.broker.resolve(self.job, "req", "deny")
            thread.join(2)

    def test_session_grant_does_not_cross_conversation(self):
        thread = self.start()
        self.broker.resolve(self.job, "req", "allow", "session")
        thread.join(2)
        self.job.events.clear()
        thread = self.start(session="chat-2")
        self.assertTrue(thread.is_alive())
        self.broker.resolve(self.job, "req", "deny")
        thread.join(2)

    def test_cancel_releases_waiter_and_rejects_late_approval(self):
        thread = self.start()
        self.broker.cancel(self.job, "req")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.answer["behavior"], "deny")
        self.assertIn("error", self.broker.resolve(self.job, "req", "allow", "persistent"))

    def test_only_web_tools_and_public_http_fetches_can_be_approved(self):
        for tool, inp in (("Bash", {"command": "curl example.com"}),
                          ("WebFetch", {"url": "file:///etc/passwd"}),
                          ("WebFetch", {"url": "http://127.0.0.1/private"}),
                          ("WebFetch", {"url": "http://localhost/private"}),
                          ("WebFetch", {"url": "https://user:pass@example.com/"}),
                          ("WebFetch", {"url": "https://example.com)(*/"}),
                          ("WebSearch", {"query": ""})):
            answer = self.broker.request(self.job, "bad", tool, inp, "chat-1", "project-a")
            self.assertEqual(answer["behavior"], "deny", (tool, inp))
        self.assertEqual(self.job.events, [])

    def test_failed_persistent_save_keeps_prompt_pending_without_granting(self):
        thread = self.start()
        with mock.patch("permissions.registry.write_json", side_effect=OSError("disk full")):
            self.assertIn("error", self.broker.resolve(self.job, "req", "allow", "persistent"))
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.broker.resolve(self.job, "req", "allow", "once"), {"ok": True})
        thread.join(2)
        self.assertEqual(self.answer["behavior"], "allow")

    def test_session_approval_also_releases_already_pending_matching_calls(self):
        first = self.start()
        answer = {}
        second = threading.Thread(target=lambda: answer.update(self.broker.request(
            self.job, "parallel", "WebSearch", {"query": "another query"}, "chat-1", "project-a")))
        second.start()
        self.addCleanup(lambda: (self.broker.cancel(self.job), second.join(2)))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(self.job.events) < 2:
            time.sleep(.01)
        self.broker.resolve(self.job, "req", "allow", "session")
        first.join(2)
        second.join(.2)
        self.assertFalse(second.is_alive(), "already authorized pending call must continue")
        self.assertEqual(answer["behavior"], "allow")

    def test_cancel_arriving_before_worker_starts_cannot_leave_a_prompt_waiting(self):
        self.broker.cancel(self.job, "req")
        thread = self.start()
        thread.join(.2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.answer["behavior"], "deny")

    def test_damaged_grants_do_not_crash_or_auto_approve(self):
        self.broker.path().write_text('{"grants": {"bad": true}}')
        thread = self.start()
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.job.events[0]["t"], "permission")
        self.broker.resolve(self.job, "req", "deny")
        thread.join(2)


class ClaudeModelCatalog(unittest.TestCase):
    def test_initialize_metadata_provides_per_model_efforts_without_user_message(self):
        cc = ClaudeCode("claude", {"bin": "fake-claude"})
        payload = {"models": [
            {"value": "fast", "displayName": "Fast", "supportsEffort": False},
            {"value": "deep", "displayName": "Deep", "supportsEffort": True,
             "supportedEffortLevels": ["low", "high"]}]}
        with mock.patch("backend_claude.control_query", return_value=payload) as query, \
                mock.patch("backend_claude.use_config_dir"):
            answer = cc.model_catalog(Path("."))
            self.assertEqual(answer["source"], "claude-initialize")
            self.assertEqual(answer["models"], [
                {"id": "fast", "label": "Fast", "efforts": []},
                {"id": "deep", "label": "Deep", "efforts": ["low", "high"]}])
            cc.model_catalog(Path("."))
            self.assertEqual(query.call_count, 1)
            self.assertEqual(query.call_args.kwargs["root"], Path("."))
            self.assertIsNotNone(cc.check("fast", "high"))
            self.assertIsNone(cc.check("deep", "high"))
            cc.model_catalog(Path("."), refresh=True)
            self.assertEqual(query.call_count, 2)

    def test_profile_change_and_failed_query_discard_previous_model_capabilities(self):
        cc = ClaudeCode("claude", {"bin": "fake-claude", "efforts": ["low"]})
        payload = {"models": [
            {"value": "default", "displayName": "Profile A default", "supportsEffort": True,
             "supportedEffortLevels": ["high"]},
            {"value": "sonnet", "displayName": "Profile A Sonnet", "supportsEffort": True,
             "supportedEffortLevels": ["high"]}]}
        with tempfile.TemporaryDirectory() as td, \
                mock.patch("backend_claude.use_config_dir"), \
                mock.patch("backend_claude.control_query", side_effect=[payload, ValueError("offline")]):
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path(td) / "profile-a")}):
                self.assertEqual(cc.model_catalog(Path("."))["default_model"], "default")
                self.assertEqual(cc.model_efforts_for(None), ["high"])
                self.assertIsNone(cc.check("sonnet", "high"))
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path(td) / "profile-b")}):
                answer = cc.model_catalog(Path("."))
                self.assertEqual(answer["source"], "configuration")
                self.assertTrue(answer["stale"])
                self.assertEqual(next(m for m in answer["models"] if m["id"] == "sonnet"),
                                 {"id": "sonnet", "label": "sonnet", "efforts": ["low"]})
                self.assertEqual(cc.model_efforts_for(None), ["low"])
                self.assertIsNotNone(cc.check("sonnet", "high"))


if __name__ == "__main__":
    unittest.main()
