"""What both servers' requests share (httpbase.py): an error is always an answer."""
import json
import os
import subprocess
import sys
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpbase  # noqa: E402
import registry  # noqa: E402
from presence import Presence  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


class Failing(httpbase.Handler):
    presence = Presence()

    def get(self, path, q):
        if path == "/bug":
            raise RuntimeError("something broke")
        if path == "/slow":
            raise subprocess.TimeoutExpired(["git", "status"], 10)
        if path == "/half":
            self._send(200, b"{}")
            raise RuntimeError("after the answer")
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
        self.assertIn("git did not finish in 10 seconds", r["error"])

    def test_an_answer_already_sent_is_not_followed_by_another(self):
        self.assertEqual(self.ask("/half"), (200, {}))


class ReadyFile(unittest.TestCase):
    def test_written_while_the_home_page_reads_it(self):
        path = tmpdir() / "instances" / "x.json"
        httpbase.write_ready_file(path, {"pid": os.getpid(), "url": "first"})
        reader = open(path, "rb")           # Windows does not replace a file open elsewhere
        timer = threading.Timer(0.3, reader.close)
        timer.start()
        self.addCleanup(timer.join)
        httpbase.write_ready_file(path, {"pid": os.getpid(), "url": "second"})
        info = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(info["url"], "second")
        self.assertEqual([p.name for p in path.parent.iterdir()], ["x.json"], "no temporary file is left")
        if registry.process_started(os.getpid()) is not None:
            self.assertEqual(info["started"], registry.process_started(os.getpid()))
        self.assertTrue(registry.server_alive(info["pid"], info.get("started")))


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
