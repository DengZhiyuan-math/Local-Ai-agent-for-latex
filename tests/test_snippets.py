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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/: tmpdirs
from tmpdirs import tmpdir  # noqa: E402

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "prism_local" / "server.py"
sys.path.insert(0, str(HERE.parent / "prism_local"))
import settings  # noqa: E402
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class Engine(unittest.TestCase):
    def test_typing_math_tabstops_and_your_file(self):
        # Where math is, what typing expands to, tabstops, auto-fraction, tabout, the file format.
        r = subprocess.run([shutil.which("node"), str(HERE / "snippets_check.js")], capture_output=True,
                           text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class SnippetsFile(unittest.TestCase):
    """Set in Home → Settings (settings.py: the file, and the switches in settings.json);
    every editor reads both from its own server."""

    def setUp(self):
        self.tmp = tmpdir()
        root = self.tmp / "paper"
        root.mkdir()
        (root / "main.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
        self.file = self.tmp / "home" / ".prism-local" / "snippets.js"
        self.env = {"PRISM_STATE_DIR": str(self.tmp / "state"), "PRISM_SNIPPETS": str(self.file)}
        ready = self.tmp / "ready.json"
        self.proc = subprocess.Popen([sys.executable, str(SERVER), str(root), "--port", "0", "--no-browser",
                                      "--ready-file", str(ready)], env={**os.environ, **self.env},
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.proc.wait, 10)
        self.addCleanup(self.proc.kill)
        with mock.patch.dict(os.environ, self.env):    # settings.py, as the Home page's server runs it
            settings.save({})
        for _ in range(150):
            if ready.exists() and ready.stat().st_size:
                break
            time.sleep(0.1)
        self.url = json.loads(ready.read_text(encoding="utf-8"))["url"]

    def editor(self):
        with HTTP.open(self.url + "api/snippets", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_saved_in_your_settings_and_read_by_the_editor(self):
        r = self.editor()
        self.assertIsInstance(r["content"], str, "without a local file, use the distributed snippets")
        self.assertTrue(r["bundled"])
        self.assertEqual(Path(r["path"]), self.file)
        self.assertEqual((r["on"], r["fraction"], r["matrix"]), (True, True, True), "on until you turn them off")
        text = '[{trigger: "Rn", replacement: "\\\\mathbb{R}^{$0}", options: "mA"}]\n'
        with mock.patch.dict(os.environ, self.env):
            settings.save_snippets(text)
            settings.save({"snippets_fraction": False, "snippets_matrix": 0})
        self.assertEqual(self.file.read_text(encoding="utf-8"), text)
        r = self.editor()
        self.assertEqual(r["content"], text)
        self.assertFalse(r["bundled"], "an existing local file takes priority")
        self.assertEqual((r["on"], r["fraction"], r["matrix"]), (True, False, False))

    def test_a_save_replaces_the_whole_file_and_keeps_its_line_ends(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b"[]\r\n")            # written on Windows by an earlier version
        with mock.patch.dict(os.environ, self.env):
            settings.save_snippets("[\n]\n")
        self.assertEqual(self.file.read_bytes(), b"[\r\n]\r\n")
        self.assertEqual(list(self.file.parent.iterdir()), [self.file], "no temporary file is left")


if __name__ == "__main__":
    unittest.main()
