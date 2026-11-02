"""Model discovery and request capability regressions; no paid inference calls."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from backends import Backend
from backend_openai import OpenAICompat
from backend_codex import Codex


class ConfiguredCapabilities(unittest.TestCase):
    def test_effort_is_checked_against_selected_model(self):
        backend = Backend("configured", {"models": ["fast", "deep"],
            "efforts": ["low", "high"], "model_efforts": {"fast": [], "deep": ["high"]}})
        self.assertIsNotNone(backend.check("fast", "high"))
        self.assertIsNone(backend.check("deep", "high"))
        self.assertIsNotNone(backend.check("deep", "low"))
        self.assertEqual(backend.model_catalog(Path("."))["models"], [
            {"id": "fast", "label": "fast", "efforts": []},
            {"id": "deep", "label": "deep", "efforts": ["high"]}])


class ApiDiscovery(unittest.TestCase):
    def setUp(self):
        self.count = 0
        self.completions = []
        self.failed = False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.count += 1
                if owner.failed:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"unavailable")
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"data": [
                    {"id": "fast", "supported_reasoning_efforts": []},
                    {"id": "deep", "supported_reasoning_efforts": ["high"]},
                    {"id": "unknown"}]}).encode())
            def do_POST(self):
                owner.completions.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\ndata: [DONE]\n\n')
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.backend = OpenAICompat("api", {"base_url": "http://127.0.0.1:%s" % self.server.server_port})

    def test_discovery_refresh_bypasses_cache_and_failure_retains_stale_catalog(self):
        first = self.backend.model_catalog(Path("."))
        self.assertEqual([m["id"] for m in first["models"]], ["fast", "deep", "unknown"])
        self.assertEqual(first["models"][1]["efforts"], ["high"])
        self.assertEqual(first["models"][2]["efforts"], [])
        self.assertIsNotNone(self.backend.check("fast", "high"))
        self.assertIsNone(self.backend.check("deep", "high"))
        self.backend.model_catalog(Path("."))
        self.assertEqual(self.count, 1)
        self.backend.model_catalog(Path("."), refresh=True)
        self.assertEqual(self.count, 2)
        self.failed = True
        failed = self.backend.model_catalog(Path("."), refresh=True)
        self.assertEqual(self.count, 3)
        self.assertTrue(failed["stale"])
        self.assertTrue(failed["error"])
        self.assertEqual(failed["models"], first["models"])
        self.assertTrue(self.backend.model_catalog(Path("."))["stale"])

    def test_changed_credentials_never_reuse_previous_accounts_models(self):
        self.backend.api_key_env = "PRISM_TEST_MODEL_KEY"
        with mock.patch.dict("os.environ", {"PRISM_TEST_MODEL_KEY": "account-a"}):
            self.assertTrue(self.backend.model_catalog(Path("."))["models"])
        self.failed = True
        with mock.patch.dict("os.environ", {"PRISM_TEST_MODEL_KEY": "account-b"}):
            result = self.backend.model_catalog(Path("."))
        self.assertTrue(result["error"])
        self.assertEqual(result["models"], [])
        self.assertIsNone(result["at"])

    def test_switch_keeps_history_and_applies_new_model_and_effort(self):
        from backends import Job
        with tempfile.TemporaryDirectory() as tmp:
            first = Job(1)
            first.root, first.prompt, first.model = Path(tmp), "question one", "fast"
            result = self.backend.run(first)
            second = Job(2)
            second.root, second.prompt = Path(tmp), "question two"
            second.session_id, second.model, second.effort = result["session_id"], "deep", "high"
            resumed = self.backend.run(second)
            self.assertEqual(resumed["session_id"], result["session_id"])
            self.assertEqual(self.completions[0]["model"], "fast")
            self.assertNotIn("reasoning_effort", self.completions[0])
            self.assertEqual(self.completions[1]["model"], "deep")
            self.assertEqual(self.completions[1]["reasoning_effort"], "high")
            messages = self.completions[1]["messages"]
            self.assertEqual([m["content"] for m in messages[1:]], ["question one", "answer", "question two"])


class CodexDiscovery(unittest.TestCase):
    def test_app_server_handshake_pages_and_model_specific_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / "fake_codex.py"
            cli.write_text("""import json, sys
initialized = False
for line in sys.stdin:
    d = json.loads(line)
    if d['method'] == 'initialize':
        result = {}
    elif d['method'] == 'initialized':
        initialized = True
        continue
    elif d['method'] == 'model/list':
        assert initialized
        cursor = d['params'].get('cursor')
        result = {'data': [{'model': 'deep' if cursor else 'fast', 'displayName': 'Deep' if cursor else 'Fast',
            'supportedReasoningEfforts': [{'reasoningEffort': 'high'}] if cursor else [],
            'isDefault': bool(cursor), 'defaultReasoningEffort': 'high' if cursor else 'none'}], 'nextCursor': None if cursor else 'page2'}
    else:
        raise AssertionError(d)
    print(json.dumps({'id': d['id'], 'result': result}), flush=True)
""")
            backend = Codex("codex", {"bin": "unused"})
            import backend_codex
            real_start = backend_codex.start
            def launch(cmd, **kwargs):
                return real_start([sys.executable, str(cli)], **kwargs)
            with mock.patch("backend_codex.start", side_effect=launch):
                catalog = backend.model_catalog(root, refresh=True)
            self.assertNotIn("error", catalog)
            self.assertEqual([m["id"] for m in catalog["models"]], ["fast", "deep"])
            self.assertEqual(catalog["models"][1]["default_effort"], "high")
            self.assertEqual(catalog["default_model"], "deep")
            self.assertIsNone(backend.check(None, "high"))
            self.assertIsNotNone(backend.check("unknown", "high"))
            self.assertIsNone(backend.check("deep", "high"))
            self.assertIsNotNone(backend.check("deep", "low"))
            self.assertIsNotNone(backend.check("fast", "high"))
            with mock.patch("backend_codex.start", side_effect=OSError("unavailable")):
                failed = backend.model_catalog(root, refresh=True)
            self.assertTrue(failed["stale"])
            self.assertEqual(failed["models"], catalog["models"])
            self.assertEqual(failed["default_model"], "deep")


if __name__ == "__main__":
    unittest.main()
