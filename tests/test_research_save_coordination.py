"""Observed-source coordination through real editor HTTP and controlled backends."""
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

import test_research_http as base
from test_agent import FakeAPI, call_chunks, manager, wait_done
from backend_openai import OpenAICompat
from project import Project
from research import ResearchKnowledge
from research_access import ResearchAccess


class SourceCoordinationHTTP(unittest.TestCase):
    tearDown = base.ResearchHTTP.tearDown
    stop = base.ResearchHTTP.stop
    request = base.ResearchHTTP.request
    initialize = base.ResearchHTTP.initialize
    events = base.ResearchHTTP.events
    finish = base.ResearchHTTP.finish
    provider = "claude"

    def setUp(self):
        if self.provider != "fixture" and (sys.platform != "darwin"
                or not Path('/usr/bin/sandbox-exec').is_file()):
            self.skipTest('Controlled native CLI HTTP fixtures require macOS sandbox-exec.')
        base.ResearchHTTP.setUp(self)

    def start(self):
        (self.work / "ready.json").unlink(missing_ok=True)
        filename = "research_fixture.py" if self.provider == "fixture" else self.provider + "_research_fixture.py"
        fixture = Path(__file__).parent / "browser" / filename
        self.log = (self.work / "server.log").open("ab")
        self.process = subprocess.Popen([sys.executable, str(fixture), str(self.work)],
                                        stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 10
        while not (self.work / "ready.json").exists() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            time.sleep(.01)
        self.assertTrue((self.work / "ready.json").exists(), (self.work / "server.log").read_text())
        ready = json.loads((self.work / "ready.json").read_text())
        self.url, self.root = ready["url"].rstrip("/"), Path(ready["root"])
        self.profile = Path(ready.get("profile", self.work))
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        code, info = self.request("/api/agent/info")
        self.assertEqual(code, 200, info)
        self.key = info["project_key"]

    def submit(self, script):
        filename = "api-script.json" if self.provider == "fixture" else "cli-script.json"
        (self.profile / filename).write_text(json.dumps(script))
        code, result = self.request("/api/agent", {"project_key": self.key, "provider": self.provider,
            "model": "fixture-model" if self.provider == "fixture" else "default", "mode": "edit",
            "prompt": "Update the original proof description.", "human_text": "Update the original proof description.",
            "active_file": "main.tex", "scope": ["main.tex"]})
        self.assertEqual(code, 200, result)
        return result["job"]

    def wait_for_read(self):
        log = self.work / "api-requests.jsonl" if self.provider == "fixture" else self.profile / "cli-requests.jsonl"
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            rows = []
            for line in log.read_text().splitlines() if log.exists() else []:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
            if self.provider == "fixture" and len(rows) >= 2:
                return
            if any(row.get("kind") == "mcp_result" and row.get("name") == "read_file" for row in rows):
                return
            time.sleep(.005)
        self.fail("The backend did not receive its source read before the manual save")

    def script(self, *operations):
        if self.provider == "fixture":
            result = [call_chunks(identity, name, arguments) for identity, name, arguments in operations]
            result[1].insert(0, 2)
            return result + [[base.say("The requested update was checked.")]]
        result = [{"type": "mcp", "name": name, "arguments": arguments}
                  for _, name, arguments in operations]
        result.insert(1, {"type": "sleep", "seconds": 2})
        return result + [{"type": "text", "text": "The requested update was checked."}]

    def external_save_during(self, script, original):
        source = self.root / "main.tex"
        job = self.submit(script)
        self.wait_for_read()
        code, current = self.request("/api/file?path=main.tex")
        self.assertEqual(code, 200, current)
        human = original + "\nA human paragraph saved after the source read.\n"
        code, saved = self.request("/api/file", {"path": "main.tex", "content": human,
                                               "base_mtime": current["mtime"]})
        self.assertEqual(code, 503, saved)
        # The editor queues its own edits during this Turn. Another program can
        # still save a file, so the model's earlier read must not overwrite it.
        source.write_text(human)
        samples, stop = [], threading.Event()
        def observe():
            while not stop.wait(.001):
                samples.append(source.read_bytes())
        thread = threading.Thread(target=observe)
        thread.start()
        try:
            events = self.finish(job)
        finally:
            stop.set()
            thread.join(2)
        self.assertTrue(samples)
        return human, samples, events

    def undo(self, events):
        done = next(event for event in events if event["t"] == "done")
        return self.request("/api/agent/undo", {"project_key": self.key, "turn": done["turn"]})

    def test_stale_whole_file_write_keeps_an_external_save(self):
        self.initialize()
        source = self.root / "main.tex"
        original = source.read_text()
        stale = original.replace("finite covering property", "checked covering property")
        human, samples, events = self.external_save_during(self.script(
            ("read", "read_file", {"path": "main.tex"}),
            ("stale", "write_file", {"path": "main.tex", "content": stale})), original)
        self.assertEqual(source.read_text(), human, "a stale draft must not erase a later external save")
        self.assertTrue(all(value == human.encode() for value in samples), "the later save must never disappear")
        self.assertTrue(any(event["t"] == "tool_result" and event.get("error")
                            and "changed" in event.get("preview", "").lower() for event in events), events)
        self.assertEqual(next(event["changed"] for event in events if event["t"] == "done"), [])
        code, result = self.undo(events)
        self.assertIn(code, (200, 409), result)
        self.assertEqual(source.read_text(), human)

    def test_a_fresh_read_in_the_same_turn_allows_a_corrected_whole_file_write(self):
        self.initialize()
        source = self.root / "main.tex"
        original = source.read_text()
        stale = original.replace("finite covering property", "checked covering property")
        fresh = stale + "\nA human paragraph saved after the source read.\n"
        human, samples, events = self.external_save_during(self.script(
            ("read", "read_file", {"path": "main.tex"}),
            ("stale", "write_file", {"path": "main.tex", "content": stale}),
            ("reread", "read_file", {"path": "main.tex"}),
            ("corrected", "write_file", {"path": "main.tex", "content": fresh})), original)
        self.assertEqual(source.read_text(), fresh)
        self.assertTrue(all(b"A human paragraph saved after the source read." in value for value in samples))
        names = {event["id"]: event["name"] for event in events if event["t"] == "tool"}
        # Deep Code also replays the native transport's carrier messages. The
        # editor's canonical file-tool results describe the actual application.
        results = [event for event in events if event["t"] == "tool_result"
                   and names.get(event["id"]) in ("Read", "Write")]
        self.assertEqual([event.get("error", False) for event in results], [False, True, False, False], events)
        code, result = self.undo(events)
        self.assertEqual(code, 200, result)
        self.assertEqual(source.read_text(), human, "Undo must preserve the external version before the corrected write")

    def test_a_precise_edit_preserves_external_content_in_its_result_and_undo(self):
        self.initialize()
        source = self.root / "main.tex"
        original = source.read_text()
        human, samples, events = self.external_save_during(self.script(
            ("read", "read_file", {"path": "main.tex"}),
            ("precise", "edit_file", {"path": "main.tex", "old_string": "finite covering property",
                                      "new_string": "checked covering property"})), original)
        self.assertEqual(source.read_text(), human.replace("finite covering property", "checked covering property"))
        self.assertTrue(all(b"A human paragraph saved after the source read." in value for value in samples))
        self.assertFalse(any(event.get("error") for event in events if event["t"] == "tool_result"), events)
        code, result = self.undo(events)
        self.assertEqual(code, 200, result)
        self.assertEqual(source.read_text(), human)

    def test_an_unobserved_external_save_survives_finish_and_undo(self):
        self.initialize()
        main = self.root / "main.tex"
        original = main.read_bytes()
        other = self.root / "sections/intro.tex"
        job = self.submit(self.script(
            ("read", "read_file", {"path": "main.tex"}),
            ("precise", "edit_file", {"path": "main.tex", "old_string": "finite covering property",
                                      "new_string": "checked covering property"})))
        self.wait_for_read()
        human = b"Human prose saved in another file during the Agent Turn.\n"
        other.write_bytes(human)
        events = self.finish(job)
        self.assertEqual(other.read_bytes(), human)
        code, result = self.undo(events)
        self.assertEqual(code, 200, result)
        self.assertEqual(other.read_bytes(), human, "Undo must not own an unrelated external save")
        self.assertEqual(main.read_bytes(), original)
        done = next(event for event in events if event["t"] == "done")
        self.assertEqual([item["path"] for item in done["changed"]], ["main.tex"])
        self.assertEqual(done["out_of_scope"], [])


class CodexSourceCoordinationHTTP(SourceCoordinationHTTP):
    provider = "codex"


class APISourceCoordinationHTTP(SourceCoordinationHTTP):
    provider = "fixture"


class DeepCodeSourceCoordinationHTTP(SourceCoordinationHTTP):
    provider = "deepcode"


class SourceCoordinationSchedule(unittest.TestCase):
    """Schedule an external save inside the shared tool's observation window."""

    def setUp(self):
        state = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.dict(os.environ, {"PRISM_STATE_DIR": state}))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.source = self.root / "main.tex"
        self.original = "An original proof description.\n"
        self.source.write_text(self.original)
        self.project = Project(self.root)
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()

    def run_turn(self, operations, schedule):
        api = FakeAPI([call_chunks(identity, name, args) for identity, name, args in operations]
                      + [[base.say("The requested update was checked.")]])
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        backend = OpenAICompat("fixture", {"base_url": f"http://127.0.0.1:{api.server_port}",
                                            "models": ["fixture-model"]})
        turn = manager(self.root, self.project.list_files, fixture=backend)
        turn.research_fn = lambda: self.knowledge
        turn.writes = self.project.files_lock
        self.project.turn = turn
        original_tool = backend._tool
        def scheduled(job, name, args, denials, **kwargs):
            return schedule(lambda: original_tool(job, name, args, denials, **kwargs), name, args)
        with mock.patch.object(backend, "_tool", side_effect=scheduled):
            started = turn.start("Update the original proof description.", None, "edit", scope=["main.tex"])
            self.assertIn("job", started, started)
            job = turn.jobs[started["job"]]
            done = wait_done(job)
        return turn, job, done

    def test_a_save_after_application_is_neither_owned_nor_observed(self):
        applied = self.original.replace("original", "checked")
        human = applied + "A later human paragraph.\n"
        saved = False
        def schedule(operation, name, args):
            nonlocal saved
            result = operation()
            if name == "write_file" and not saved:
                saved = True
                self.source.write_text(human)
            return result
        turn, job, done = self.run_turn([
            ("read", "read_file", {"path": "main.tex"}),
            ("write", "write_file", {"path": "main.tex", "content": applied}),
            ("retry", "write_file", {"path": "main.tex", "content": self.original})], schedule)
        self.assertEqual(self.source.read_text(), human)
        results = [event for event in job.events if event["t"] == "tool_result"]
        self.assertEqual([event.get("error", False) for event in results], [False, True, True])
        self.assertEqual(job.source_versions["main.tex"], hashlib.sha256(applied.encode()).hexdigest())
        self.assertNotIn("main.tex", job.controlled_writes)
        self.assertEqual(done["changed"], [])
        self.assertEqual(turn.undo(done["turn"])["restored"], [])
        self.assertEqual(self.source.read_text(), human)

    def test_a_read_is_bound_to_its_returned_bytes_during_an_external_replace(self):
        def schedule(operation, name, args):
            if name != "read_file":
                return operation()
            self.source.write_text("A transient external replacement.\n")
            result = operation()
            self.source.write_text(self.original)
            return result
        turn, job, done = self.run_turn([("read", "read_file", {"path": "main.tex"})], schedule)
        results = [event for event in job.events if event["t"] == "tool_result"]
        self.assertEqual([event.get("error", False) for event in results], [True])
        self.assertIn("changed", results[0]["preview"].lower())
        self.assertEqual(self.source.read_text(), self.original)
        self.assertEqual(done["changed"], [])
        self.assertEqual(turn.undo(done["turn"])["restored"], [])

    def test_a_whole_file_write_checks_observed_bytes_at_application(self):
        human = self.original + "An external save before the draft application.\n"
        def schedule(operation, name, args):
            if name == "write_file":
                self.source.write_text(human)
            return operation()
        turn, job, done = self.run_turn([
            ("read", "read_file", {"path": "main.tex"}),
            ("write", "write_file", {"path": "main.tex", "content": self.original.replace("original", "checked")})],
            schedule)
        self.assertEqual(self.source.read_text(), human)
        results = [event for event in job.events if event["t"] == "tool_result"]
        self.assertEqual([event.get("error", False) for event in results], [False, True])
        self.assertEqual(done["changed"], [])
        self.assertEqual(turn.undo(done["turn"])["restored"], [])
        self.assertEqual(self.source.read_text(), human)

    def test_a_precise_edit_rebases_its_actual_external_input_without_observing_it(self):
        human = self.original + "An external save before the precise edit.\n"
        def schedule(operation, name, args):
            if name == "edit_file":
                self.source.write_text(human)
            return operation()
        turn, job, done = self.run_turn([
            ("read", "read_file", {"path": "main.tex"}),
            ("edit", "edit_file", {"path": "main.tex", "old_string": "original", "new_string": "checked"}),
            ("replace", "write_file", {"path": "main.tex", "content": self.original})], schedule)
        self.assertEqual(self.source.read_text(), human.replace("original", "checked"))
        results = [event for event in job.events if event["t"] == "tool_result"]
        self.assertEqual([event.get("error", False) for event in results], [False, False, True])
        self.assertEqual(job.source_versions["main.tex"], hashlib.sha256(self.original.encode()).hexdigest())
        self.assertEqual([item["path"] for item in done["changed"]], ["main.tex"])
        self.assertEqual(turn.undo(done["turn"])["restored"], ["main.tex"])
        self.assertEqual(self.source.read_text(), human)

    def test_a_save_during_validation_is_preserved_before_application(self):
        human = self.original + "An external save during source validation.\n"
        original_check = ResearchAccess.check_write
        def check_then_save(access, *args, **kwargs):
            result = original_check(access, *args, **kwargs)
            self.source.write_text(human)
            return result
        def schedule(operation, name, args):
            if name == "write_file":
                with mock.patch.object(ResearchAccess, "check_write", new=check_then_save):
                    return operation()
            return operation()
        turn, job, done = self.run_turn([
            ("read", "read_file", {"path": "main.tex"}),
            ("write", "write_file", {"path": "main.tex", "content": self.original.replace("original", "checked")})],
            schedule)
        self.assertEqual(self.source.read_text(), human)
        results = [event for event in job.events if event["t"] == "tool_result"]
        self.assertEqual([event.get("error", False) for event in results], [False, True])
        self.assertEqual(done["changed"], [])
        self.assertEqual(turn.undo(done["turn"])["restored"], [])
        self.assertEqual(self.source.read_text(), human)


if __name__ == "__main__":
    unittest.main()
