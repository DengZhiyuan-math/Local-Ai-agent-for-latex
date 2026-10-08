"""Snippets in the editor (prism_local/static/snippets.js) and your snippets file."""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/: tmpdirs
from tmpdirs import tmpdir  # noqa: E402

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "prism_local" / "server.py"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class Engine(unittest.TestCase):
    def test_typing_math_tabstops_and_your_file(self):
        # Where math is, what typing expands to, tabstops, auto-fraction, tabout, the file format.
        r = subprocess.run([shutil.which("node"), str(HERE / "snippets_check.js")], capture_output=True,
                           text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class SnippetsFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        root = self.tmp / "paper"
        root.mkdir()
        (root / "main.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
        self.file = self.tmp / "home" / ".prism-local" / "snippets.js"
        ready = self.tmp / "ready.json"
        env = {**os.environ, "PRISM_STATE_DIR": str(self.tmp / "state"), "PRISM_SNIPPETS": str(self.file)}
        self.proc = subprocess.Popen([sys.executable, str(SERVER), str(root), "--port", "0", "--no-browser",
                                      "--ready-file", str(ready)], env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.addCleanup(self.proc.wait, 10)
        self.addCleanup(self.proc.kill)
        for _ in range(150):
            if ready.exists() and ready.stat().st_size:
                break
            time.sleep(0.1)
        self.url = json.loads(ready.read_text(encoding="utf-8"))["url"]

    def call(self, path, body=None):
        req = urllib.request.Request(self.url + path.lstrip("/"))
        if body is not None:
            req.data = json.dumps(body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Prism-Local", "1")
        with HTTP.open(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_saved_in_your_settings_and_read_back(self):
        r = self.call("/api/snippets")
        self.assertIsNone(r["content"], "no file yet: the editor offers a template")
        self.assertEqual(Path(r["path"]), self.file)
        text = '[{trigger: "Rn", replacement: "\\\\mathbb{R}^{$0}", options: "mA"}]\n'
        self.call("/api/snippets", {"content": text})
        self.assertEqual(self.file.read_text(encoding="utf-8"), text)
        self.assertEqual(self.call("/api/snippets")["content"], text)


if __name__ == "__main__":
    unittest.main()
