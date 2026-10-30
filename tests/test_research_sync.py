"""Research HTTP writes through existing GitSync and disposable local remotes.

The real editor routes produce records. GitSync uses its public tick interface
with the existing deterministic clock; busy=True models an active Turn without
starting a model. No GitHub, native AI CLI, or installed-App acceptance runs.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
import test_gitsync as syncbase
from registry import project_key
from research import RECORD
from test_lifecycle import request
from tmpdirs import tmpdir


@unittest.skipUnless(shutil.which("git"), "needs git")
class ResearchSync(unittest.TestCase):
    def setUp(self):
        state = tmpdir()
        environment = mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(state)})
        environment.start()
        self.addCleanup(environment.stop)
        syncbase.GitSyncTest.setUp(self)
        (self.root / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\emph{compact} means a finite covering property.\n$x$\n\\end{document}\n",
            encoding="utf-8")
        self.g.commit_all("Set research source")
        self.g.push(manual=True)
        self.busy = False
        self.g.busy_fn = lambda: self.busy
        for attribute, value in (("GITSYNC", self.g), ("PROJECT", None)):
            patch = mock.patch.object(server, attribute, value)
            patch.start()
            self.addCleanup(patch.stop)
        server.set_root(self.root)
        self.key = project_key(self.root)
        self.srv = server.httpbase.Server(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def ask(self, path, body=None):
        headers = {"Content-Type": "application/json", "X-Prism-Local": "1",
                   "X-Prism-Project": self.key}
        payload = None if body is None else json.dumps({"project_key": self.key, **body}).encode()
        return request(self.url, path, payload, headers)

    def initialize(self):
        code, result = self.ask("/api/research/initialize", {"question": "Can the estimate improve?"})
        self.assertEqual(code, 200, result)
        return result

    def tick(self):
        self.clock.advance(syncbase.gitsync.IDLE + 1)
        self.g.tick()

    def remote_record(self):
        return json.loads(syncbase.git(self.remote, "show", "main:" + RECORD))

    def test_http_initialization_autosaves_record_and_configuration_through_existing_sync(self):
        self.initialize()
        self.tick()
        local = json.loads((self.root / RECORD).read_text(encoding="utf-8"))
        self.assertEqual(self.remote_record(), local)
        self.assertEqual(local["discussion"], {"mode": "markdown"})
        self.assertEqual(local["question"], "Can the estimate improve?")
        self.assertEqual(syncbase.git(self.remote, "rev-parse", "main"),
                         syncbase.git(self.root, "rev-parse", "HEAD"))
        self.assertFalse(self.g.status()["pending"])

    def test_disabled_sync_keeps_http_correction_local_until_reenabled(self):
        self.initialize()
        self.tick()
        before_head = syncbase.git(self.root, "rev-parse", "HEAD")
        before_remote = self.remote_record()
        self.assertEqual(self.ask("/api/git/sync", {"action": "off"})[0], 200)
        code, corrected = self.ask("/api/research/correct", {
            "term_updates": [{"name": "compact", "meaning": "the author corrected definition"}],
            "notes": "Keep this correction local while automatic sync is off."})
        self.assertEqual(code, 200, corrected)
        self.tick()
        self.assertEqual(syncbase.git(self.root, "rev-parse", "HEAD"), before_head)
        self.assertEqual(self.remote_record(), before_remote)
        self.assertEqual(corrected["terms"][0]["meaning"], "the author corrected definition")
        self.assertEqual(self.ask("/api/research")[1]["terms"], corrected["terms"])
        self.assertEqual(self.ask("/api/git/sync", {"action": "on"})[0], 200)
        self.tick()
        self.assertEqual(self.remote_record(), json.loads((self.root / RECORD).read_text(encoding="utf-8")))
        self.assertFalse(self.g.status()["pending"])

    def test_active_turn_defers_merge_without_losing_verified_edit_or_remote_record_change(self):
        self.initialize()
        self.tick()
        colleague = tmpdir()
        syncbase.git(colleague, "clone", "-q", str(self.remote), ".")
        syncbase.identity(colleague)
        record = colleague / RECORD
        received = json.loads(record.read_text(encoding="utf-8"))
        received["question"] = "A remotely updated research question."
        record.write_text(json.dumps(received, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        syncbase.git(colleague, "add", RECORD)
        syncbase.git(colleague, "commit", "-qm", "Update research question")
        syncbase.git(colleague, "push", "-q")
        self.busy = True
        code, source = self.ask("/api/file?path=main.tex")
        self.assertEqual(code, 200, source)
        text = source["content"] + "\nAuthor-supplied $z$.\n"
        code, saved = self.ask("/api/file", {"path": "main.tex", "content": text,
            "base_mtime": source["mtime"], "manual_edit_token": source["manual_edit"]["token"]})
        self.assertEqual(code, 200, saved)
        self.assertNotIn("research_warning", saved)
        local_record = (self.root / RECORD).read_bytes()
        local_head = syncbase.git(self.root, "rev-parse", "HEAD")
        self.tick()
        held = self.g.pull(manual=True)
        self.assertEqual(held.state, "local")
        self.assertIn("agent", held.message.lower())
        self.assertEqual(syncbase.git(self.root, "rev-parse", "HEAD"), local_head)
        self.assertEqual((self.root / RECORD).read_bytes(), local_record)
        self.assertEqual((self.root / "main.tex").read_text(encoding="utf-8"), text)
        self.busy = False
        self.tick()
        code, restored = self.ask("/api/research")
        self.assertEqual(code, 200, restored)
        self.assertEqual(restored["question"], "A remotely updated research question.")
        self.assertIn("z", {s["canonical"] for s in restored["symbols"]})
        self.assertTrue(any(s["evidence"] == "human_edit" for item in restored["symbols"]
                            if item["canonical"] == "z" for s in item["sources"]))
        self.assertEqual(self.remote_record(), json.loads((self.root / RECORD).read_text(encoding="utf-8")))
        self.assertEqual(syncbase.git(self.remote, "show", "main:main.tex"), text.strip())

    def test_rejected_upload_preserves_local_record_and_retries_without_false_success(self):
        self.initialize()
        self.tick()
        old_remote = self.remote_record()
        old_push = self.g.status()["last_push"]
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        code, corrected = self.ask("/api/research/correct", {"notes": "A local correction awaiting upload."})
        self.assertEqual(code, 200, corrected)
        self.tick()
        self.assertEqual(self.remote_record(), old_remote)
        self.assertTrue(self.g.status()["error"])
        self.assertGreater(self.g.status()["ahead"], 0)
        self.assertEqual(self.g.status()["last_push"], old_push)
        self.assertEqual(self.ask("/api/research")[1]["corrections"], corrected["corrections"])
        hook.unlink()
        self.tick()
        self.assertEqual(self.remote_record(), json.loads((self.root / RECORD).read_text(encoding="utf-8")))
        self.assertFalse(self.g.status()["error"])
        self.assertEqual(self.g.status()["ahead"], 0)
