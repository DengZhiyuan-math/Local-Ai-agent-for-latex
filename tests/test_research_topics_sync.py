"""Topic HTTP writes use the existing GitSync schedule and disposable remotes."""
import json
from pathlib import Path
import shutil
import sys
import threading
import time
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
from agent import AgentManager
from backend_openai import OpenAICompat
from research import RECORD
import test_gitsync as gitbase
import test_research_http as httpbase
import test_research_sync as syncbase
from test_agent import FakeAPI, call_chunks
from tmpdirs import tmpdir


@unittest.skipUnless(shutil.which("git"), "needs git")
class ResearchTopicsSync(unittest.TestCase):
    ask = syncbase.ResearchSync.ask
    initialize = syncbase.ResearchSync.initialize
    tick = syncbase.ResearchSync.tick
    remote_record = syncbase.ResearchSync.remote_record

    def setUp(self):
        syncbase.ResearchSync.setUp(self)
        self.api = FakeAPI([])
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)
        backend = OpenAICompat("topic-fixture", {
            "base_url": f"http://127.0.0.1:{self.api.server_port}", "models": ["fixture-model"]})
        manager = AgentManager(lambda: server.PROJECT.root, lambda: server.PROJECT.list_files(),
            lambda rel: server.PROJECT.resolve(rel), backends=({backend.id: backend}, backend.id, None))
        manager.research_fn = lambda: server.ResearchKnowledge(server.PROJECT)
        manager.writes = server.SAVE_LOCK
        manager.on_start = self.g.before_turn
        manager.on_changed = self.g.after_turn
        patcher = mock.patch.object(server, "AGENT", manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(manager.shutdown)
        server.set_root(self.root)
        self.g.busy_fn = lambda: self.busy or manager.busy()
        self.initialize()
        self.tick()

    def start_record(self, title="Proof route", text="Compare the direct argument.", release=None):
        def final_chunks():
            if release is not None:
                release.wait(10)
            yield httpbase.say("The idea remains tentative.")

        self.api.script = [call_chunks("topic", "record_research_topic", {
            "title": title, "entries": [{"kind": "idea", "text": text, "source": "assistant"}]}),
            final_chunks()]
        code, result = self.ask("/api/agent", {
            "provider": "topic-fixture", "model": "fixture-model", "mode": "ask",
            "prompt": "Maybe compare the direct argument.", "human_text": "Maybe compare the direct argument."})
        self.assertEqual(code, 200, result)
        return result["job"]

    def finish(self, job):
        events, deadline = [], time.monotonic() + 10
        while time.monotonic() < deadline:
            params = urllib.parse.urlencode({"project_key": self.key, "job": job,
                                            "after": len(events), "wait": .2})
            code, result = self.ask("/api/agent/events?" + params)
            self.assertEqual(code, 200, result)
            events.extend(result["events"])
            if result["done"]:
                return events
        self.fail("topic fixture Turn did not finish")

    def test_generated_topic_and_index_follow_existing_autosave(self):
        events = self.finish(self.start_record())
        notice = next((event for event in events if event["t"] == "research_record"), None)
        self.assertIsNotNone(notice, events)
        self.assertEqual(next(event for event in events if event["t"] == "done")["changed"], [])
        path = notice["path"]
        self.assertIn("Compare the direct argument.", (self.root / path).read_text())
        self.tick()
        self.assertEqual(gitbase.git(self.remote, "show", "main:" + path),
                         (self.root / path).read_text().strip())
        self.assertEqual(self.remote_record(), json.loads((self.root / RECORD).read_text()))
        self.assertFalse(self.g.status()["pending"])

    def test_disabled_sync_keeps_the_generated_topic_local_until_reenabled(self):
        before_head = gitbase.git(self.root, "rev-parse", "HEAD")
        before_remote = self.remote_record()
        self.assertEqual(self.ask("/api/git/sync", {"action": "off"})[0], 200)
        events = self.finish(self.start_record())
        notice = next(event for event in events if event["t"] == "research_record")
        self.tick()
        self.assertEqual(gitbase.git(self.root, "rev-parse", "HEAD"), before_head)
        self.assertEqual(self.remote_record(), before_remote)
        self.assertIn("Compare the direct argument.", (self.root / notice["path"]).read_text())
        self.assertEqual(self.ask("/api/git/sync", {"action": "on"})[0], 200)
        self.tick()
        self.assertEqual(gitbase.git(self.remote, "show", "main:" + notice["path"]),
                         (self.root / notice["path"]).read_text().strip())

    def test_active_turn_defers_commit_and_pull_then_syncs_both_record_and_remote_edit(self):
        colleague = tmpdir()
        gitbase.git(colleague, "clone", "-q", str(self.remote), ".")
        gitbase.identity(colleague)
        (colleague / "sec.tex").write_text("A remotely updated source.\n", encoding="utf-8")
        gitbase.git(colleague, "add", "sec.tex")
        gitbase.git(colleague, "commit", "-qm", "Remote source update")
        gitbase.git(colleague, "push", "-q")
        before = gitbase.git(self.root, "rev-parse", "HEAD")
        release = threading.Event()
        self.addCleanup(release.set)
        job = self.start_record(release=release)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, result = self.ask("/api/agent/events?" + urllib.parse.urlencode({
                "project_key": self.key, "job": job, "after": 0, "wait": .1}))
            self.assertEqual(code, 200, result)
            if any(event["t"] == "tool_result" for event in result["events"]):
                break
        else:
            self.fail("topic draft was not staged")
        self.assertTrue(server.AGENT.busy())
        self.tick()
        self.assertEqual(gitbase.git(self.root, "rev-parse", "HEAD"), before)
        self.assertEqual((self.root / "sec.tex").read_text(), "a\n")
        held = self.g.pull(manual=True)
        self.assertEqual(held.state, "local")
        self.assertIn("agent", held.message.lower())
        release.set()
        events = self.finish(job)
        notice = next(event for event in events if event["t"] == "research_record")
        self.tick()
        self.assertEqual((self.root / "sec.tex").read_text(), "A remotely updated source.\n")
        self.assertEqual(gitbase.git(self.remote, "show", "main:" + notice["path"]),
                         (self.root / notice["path"]).read_text().strip())
        self.assertFalse(self.g.status()["error"])

    def test_rejected_upload_preserves_local_topic_and_existing_sync_retries_it(self):
        before_remote = self.remote_record()
        before_push = self.g.status()["last_push"]
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        events = self.finish(self.start_record())
        notice = next(event for event in events if event["t"] == "research_record")
        saved = (self.root / notice["path"]).read_bytes()
        self.tick()
        self.assertEqual(self.remote_record(), before_remote)
        self.assertTrue(self.g.status()["error"])
        self.assertGreater(self.g.status()["ahead"], 0)
        self.assertEqual(self.g.status()["last_push"], before_push)
        self.assertEqual((self.root / notice["path"]).read_bytes(), saved)
        hook.unlink()
        self.tick()
        self.assertEqual(gitbase.git(self.remote, "show", "main:" + notice["path"]),
                         saved.decode().strip())
        self.assertFalse(self.g.status()["error"])
        self.assertEqual(self.g.status()["ahead"], 0)

    def test_sync_conflict_keeps_remote_topic_text_and_later_maintenance_does_not_overwrite_it(self):
        first = self.finish(self.start_record())
        notice = next(event for event in first if event["t"] == "research_record")
        self.tick()
        colleague = tmpdir()
        gitbase.git(colleague, "clone", "-q", str(self.remote), ".")
        gitbase.identity(colleague)
        received = colleague / notice["path"]
        received.write_text(received.read_text().replace("Compare the direct argument.",
                            "Compare the colleague argument."), encoding="utf-8")
        gitbase.git(colleague, "add", notice["path"])
        gitbase.git(colleague, "commit", "-qm", "Annotate shared topic")
        gitbase.git(colleague, "push", "-q")
        second = self.finish(self.start_record(text="Consider an endpoint split."))
        self.assertTrue(any(event["t"] == "research_record" for event in second), second)
        self.tick()
        merged = (self.root / notice["path"]).read_text()
        self.assertIn("Compare the colleague argument.", merged)
        self.assertIn("Consider an endpoint split.", merged)
        self.assertEqual(gitbase.git(self.remote, "show", "main:" + notice["path"]), merged.strip())
        self.assertFalse(self.g.status()["error"])
        preserved = (self.root / notice["path"]).read_bytes()
        next_update = self.finish(self.start_record(text="Check the remaining endpoint hypothesis."))
        self.assertFalse(any(event["t"] == "research_record" for event in next_update), next_update)
        self.assertTrue(any(event["t"] == "research_record_error" for event in next_update), next_update)
        self.assertEqual((self.root / notice["path"]).read_bytes(), preserved)
        code, status = self.ask("/api/research")
        self.assertEqual(code, 200, status)
        self.assertEqual(status["discussion_records"]["pending_count"], 1)


if __name__ == "__main__":
    unittest.main()
