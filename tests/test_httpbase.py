"""What both servers' requests share (httpbase.py): an error is always an answer."""
import json
import os
import subprocess
import sys
import threading
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import errors  # noqa: E402
import httpbase  # noqa: E402
import registry  # noqa: E402
from presence import Presence  # noqa: E402


class Failing(httpbase.Handler):
    presence = Presence()

    def get(self, path, q):
        if path == "/bug":
            raise RuntimeError("something broke")
        if path == "/slow":
            raise subprocess.TimeoutExpired(["git", "status"], 10)
        if path == "/push":
            raise subprocess.TimeoutExpired(["git", "-c", "x=y", "push", "origin", "main"], 60)
        if path == "/half":
            self._send(200, b"{}")
            raise RuntimeError("after the answer")
        if path == "/gone":
            raise errors.NotFound("unknown project")
        if path == "/param":
            return self._json({"path": q["path"]})          # missing: a Refused
        if path == "/keyerror":
            return self._json({}[path])                     # a bug's KeyError
        if path == "/value":
            raise ValueError("not a name")
        if path == "/locked":
            with registry.FileLock(Path(q["lock"]), timeout=0.2):
                return self._json({"ok": True})
        return self._json({"ok": True})

    def post(self, path, body):
        return self._json({"job": int(body["job"])})       # TypeError for None


class Answers(unittest.TestCase):
    def setUp(self):
        self.srv = httpbase.Server(("127.0.0.1", 0), Failing)
        self.srv.handle_error = lambda *a: None                # "/half" re-raises; keep the log quiet
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def ask(self, path, body=None):
        req = urllib.request.Request(self.url + path, None if body is None else json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "X-Prism-Local": "1"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_an_unexpected_error_is_a_json_answer(self):
        self.assertEqual(self.ask("/bug"), (500, {"error": "RuntimeError: something broke"}))
        self.assertEqual(self.ask("/api/x", {"job": None})[0], 500)
        self.assertEqual(self.ask("/ok"), (200, {"ok": True}), "the server goes on")

    def test_a_command_that_timed_out_says_so(self):
        status, r = self.ask("/slow")
        self.assertEqual(status, 504)
        self.assertIn("git did not finish within 10 seconds; try again", r["error"])
        status, r = self.ask("/push")             # a Push or Pull from the menu: as the sync says it
        self.assertEqual(status, 504)
        self.assertIn("GitHub did not answer within 60 seconds", r["error"])
        self.assertNotIn("origin", r["error"])

    def test_a_lock_another_window_holds_is_a_409_that_says_so(self):
        """Not a dropped connection, which the page reads as "server not reachable"."""
        lock = Path(tempfile.mkdtemp()) / "projects.lock"
        with registry.FileLock(lock, timeout=1):
            status, r = self.ask("/locked?lock=" + urllib.parse.quote(str(lock)))
        self.assertEqual(status, 409)
        self.assertIn("busy with projects.lock; try again", r["error"])
        self.assertEqual(self.ask("/locked?lock=" + urllib.parse.quote(str(lock))), (200, {"ok": True}))

    def test_an_answer_already_sent_is_not_followed_by_another(self):
        self.assertEqual(self.ask("/half"), (200, {}))

    def test_errors_become_statuses_in_one_place(self):
        """A Refused says its status; a missing parameter is a 400 that names it; a
        KeyError otherwise is a bug: a 500, with its traceback in the log."""
        with mock.patch.object(httpbase, "log") as log:
            self.assertEqual(self.ask("/gone"), (404, {"error": "unknown project"}))
            self.assertEqual(self.ask("/param"), (400, {"error": "missing path"}))
            self.assertEqual(self.ask("/param?path=a.tex"), (200, {"path": "a.tex"}))
            log.assert_not_called()
            self.assertEqual(self.ask("/keyerror")[0], 500)
            self.assertIn("Traceback", log.call_args.args[0])
            self.assertEqual(self.ask("/value"), (400, {"error": "not a name"}))
            self.assertIn("refused GET /value", log.call_args.args[0])


class CodeStamp(unittest.TestCase):
    def test_the_code_is_looked_at_again_only_every_few_seconds(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(httpbase, "CODE_DIR", Path(tmp)), \
                mock.patch.dict(httpbase._CODE_STAMP, {"at": None, "stamp": 0.0}):
            (Path(tmp) / "server.py").write_text("")
            os.utime(Path(tmp) / "server.py", (1000, 1000))
            self.assertEqual(httpbase.code_stamp(), 1000)
            os.utime(Path(tmp) / "server.py", (2000, 2000))
            self.assertEqual(httpbase.code_stamp(), 1000, "asked again at once: as before")
            httpbase._CODE_STAMP["at"] -= httpbase.CODE_STAMP_EVERY
            self.assertEqual(httpbase.code_stamp(), 2000, "a few seconds later: the update is seen")


if __name__ == "__main__":
    unittest.main()
