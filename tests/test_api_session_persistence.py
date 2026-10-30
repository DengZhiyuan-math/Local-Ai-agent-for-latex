"""API conversations survive backend recreation in private local state."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend_openai import OpenAICompat
from test_agent import FakeAPI, job_for


def say(text):
    return {"choices": [{"delta": {"content": text}}]}


class APISessionPersistence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.root = self.work / "project"
        self.root.mkdir()
        self.env = mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.work / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = FakeAPI([[say("The first answer.")], [say("The second answer.")]])
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        self.spec = {"base_url": f"http://127.0.0.1:{self.api.server_port}",
                     "models": ["first", "second"], "default_model": "first"}

    def backend(self, **updates):
        return OpenAICompat("fixture", {**self.spec, **updates})

    def run_turn(self, backend, prompt, session=None, model="first"):
        return backend.run(job_for(root=self.root, prompt=prompt, session_id=session,
            mode="ask", model=model, research_access=None, files=lambda: []))

    def test_recreated_backend_restores_conversation_and_model_change_keeps_history(self):
        first = self.backend()
        result = self.run_turn(first, "The original human message.")
        session = result["session_id"]
        restored = self.backend()
        self.assertIsNone(restored.check_session(self.root, session))
        self.run_turn(restored, "The next human message.", session=session, model="second")
        request = self.api.requests[-1]["body"]
        self.assertEqual(request["model"], "second")
        self.assertIn({"role": "user", "content": "The original human message."}, request["messages"])
        self.assertIn({"role": "assistant", "content": "The first answer."}, request["messages"])

    def test_project_provider_and_endpoint_must_match_before_restoring(self):
        session = self.run_turn(self.backend(), "Remember this project.")["session_id"]
        other = self.work / "other"
        other.mkdir()
        self.assertIsNotNone(self.backend().check_session(other, session))
        self.assertIsNotNone(self.backend(base_url="http://127.0.0.1:1").check_session(self.root, session))
        alternate = OpenAICompat("another-provider", self.spec)
        self.assertIsNotNone(alternate.check_session(self.root, session))
        self.assertIsNone(self.backend().check_session(self.root, session))

    def test_credential_binding_does_not_store_credentials(self):
        with mock.patch.dict(os.environ, {"PRISM_SESSION_TEST_KEY": "secret-api-key"}):
            backend = self.backend(api_key_env="PRISM_SESSION_TEST_KEY", headers={"X-Secret": "secret-header"})
            session = self.run_turn(backend, "Remember the safe message.")["session_id"]
            saved = "\n".join(p.read_text() for p in (self.work / "state").rglob("*.json"))
            self.assertNotIn("secret-api-key", saved)
            self.assertNotIn("secret-header", saved)
            with mock.patch.dict(os.environ, {"PRISM_SESSION_TEST_KEY": "another-key"}):
                self.assertIsNotNone(self.backend(api_key_env="PRISM_SESSION_TEST_KEY",
                    headers={"X-Secret": "secret-header"}).check_session(self.root, session))

    def test_damaged_record_or_invalid_session_id_cannot_restore(self):
        session = self.run_turn(self.backend(), "Remember the prior message.")["session_id"]
        path = next((self.work / "state").rglob("*.json"))
        record = json.loads(path.read_text())
        path.write_text("broken JSON")
        self.assertIsNotNone(self.backend().check_session(self.root, session))
        record["messages"] = [{"role": "administrator", "content": "A forged role."}]
        path.write_text(json.dumps(record))
        self.assertIsNotNone(self.backend().check_session(self.root, session))
        self.assertIsNotNone(self.backend().check_session(self.root, "../outside"))
        self.assertIsNotNone(self.backend().check_session(self.root, {"forged": "identity"}))

    def test_a_linked_private_record_is_not_loaded(self):
        session = self.run_turn(self.backend(), "Remember the prior message.")["session_id"]
        path = next((self.work / "state").rglob("*.json"))
        target = self.work / "copied-record.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("symbolic links unavailable")
        self.assertIsNotNone(self.backend().check_session(self.root, session))

    def test_save_failure_is_visible_and_keeps_only_in_memory_session(self):
        backend = self.backend()
        job = job_for(root=self.root, prompt="A message.", mode="ask", model="first",
                      research_access=None, files=lambda: [])
        with mock.patch("registry.write_state", side_effect=OSError("private state unavailable")):
            result = backend.run(job)
        self.assertFalse(result["session_persisted"])
        self.assertIn("remains in memory", result["session_warning"])
        self.assertTrue(any(e["t"] == "error" and "saved for restart" in e["message"] for e in job.events))
        self.assertIsNone(backend.check_session(self.root, result["session_id"]))
        self.assertIsNotNone(self.backend().check_session(self.root, result["session_id"]))


if __name__ == "__main__":
    unittest.main()
