"""Agent UI request configuration and permission route boundaries. No live providers."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from agent import AgentManager
from backends import Backend
import settings


class ConfigBackend(Backend):
    models = ["small", "large"]
    efforts = ["low", "high"]

    def unavailable(self):
        return None


class RequestConfiguration(unittest.TestCase):
    def setUp(self):
        self.backend = ConfigBackend("fake", {})
        self.manager = AgentManager(lambda: Path("."), lambda: [],
                                    backends=({"fake": self.backend}, "fake", None))

    def test_user_choice_is_validated_and_saved_as_next_session_default(self):
        with mock.patch.object(settings, "save", return_value={}) as save:
            result = self.manager.configure("fake", "large", "high")
        self.assertTrue(result["ok"])
        save.assert_called_once_with({"agent_defaults": {
            "provider": "fake", "providers": {"fake": {"model": "large", "effort": "high"}}}})
        with mock.patch.object(settings, "save") as save:
            self.assertIn("error", self.manager.configure("fake", "--flag", "high"))
            self.assertIn("error", self.manager.configure("fake", "large", "turbo"))
            save.assert_not_called()

    def test_catalog_is_forwarded_from_backend(self):
        with mock.patch.object(self.backend, "model_catalog", return_value={"models": [{"id": "fresh", "efforts": []}]}):
            self.assertEqual(self.manager.models("fake", True)["models"][0]["id"], "fresh")
        self.assertIn("error", self.manager.models("absent"))

    def test_permission_reply_cannot_target_finished_or_foreign_turn(self):
        self.assertIn("error", self.manager.permission(100, "r1", "allow", "once"))


class CompletionUsage(unittest.TestCase):
    def test_every_completed_request_refreshes_quota_even_when_turn_fails(self):
        import tempfile
        from backends import Job
        class QuotaBackend(ConfigBackend):
            usage_limits = True
            def run(self, job):
                if job.prompt == "fail":
                    raise ValueError("fixture turn failed")
                return {"session_id": "kept"}
            def probe_rate(self, force=True):
                self.calls.append(force)
                return {"rate": {"at": 123, "unifiedWindows": {"five_hour": {"utilization": .2}}},
                        "last_success": 123, "stale": False}
        with tempfile.TemporaryDirectory() as directory:
            backend = QuotaBackend("fake", {})
            backend.calls = []
            manager = AgentManager(lambda: Path(directory), lambda: [],
                                   backends=({"fake": backend}, "fake", None))
            for number, prompt in enumerate(("success", "fail"), 1):
                job = Job(number)
                job.provider, job.root, job.prompt = "fake", Path(directory), prompt
                manager.jobs[number] = job
                manager.active = job
                manager._run(job, backend)
                self.assertTrue(job.done)
                rate = [event for event in job.events if event["t"] == "rate"]
                self.assertEqual(rate[0]["last_success"], 123)
                self.assertFalse(rate[0]["stale"])
            self.assertEqual(backend.calls, [True, True])

    def test_selected_profile_is_applied_before_subscription_auth_guard(self):
        from backend_claude import ClaudeCode
        backend = ClaudeCode("claude", {"bin": "fixture"})
        manager = AgentManager(lambda: Path("."), lambda: [],
                               backends=({"claude": backend}, "claude", None))
        applied = []
        def check(root):
            self.assertTrue(applied)
            return ["API authentication"]
        with mock.patch("backend_claude.use_config_dir", side_effect=lambda: applied.append(True)), \
                mock.patch("backend_claude.auth_overrides", side_effect=check), \
                mock.patch.object(backend, "probe_rate") as query:
            result = manager.probe_rate()
        self.assertTrue(result["stale"])
        self.assertIsNone(result["rate"])
        query.assert_not_called()


class SavedDefaults(unittest.TestCase):
    def test_new_manager_sees_saved_model_and_other_provider_defaults_survive(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"PRISM_STATE_DIR": directory}):
            first = ConfigBackend("first", {})
            second = ConfigBackend("second", {})
            manager = AgentManager(lambda: Path(directory), lambda: [],
                                   backends=({"first": first, "second": second}, "first", None))
            self.assertTrue(manager.configure("first", "large", "high")["ok"])
            self.assertTrue(manager.configure("second", "small", "low")["ok"])
            new = AgentManager(lambda: Path(directory), lambda: [],
                               backends=({"first": first, "second": second}, "first", None))
            self.assertEqual(new.info()["default"], "second")
            self.assertEqual(new.info()["defaults"]["providers"], {
                "first": {"model": "large", "effort": "high"},
                "second": {"model": "small", "effort": "low"}})
