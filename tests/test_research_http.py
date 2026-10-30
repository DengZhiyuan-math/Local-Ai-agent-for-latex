"""Research initialization through the real editor HTTP and API Turn interfaces."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agent import call_chunks


def say(text):
    return {"choices": [{"delta": {"content": text}}]}


class ResearchHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.process = None
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stop)
        self.start()

    def tearDown(self):
        self.stop()
        self.temp.cleanup()

    def start(self, empty=False):
        (self.work / "ready.json").unlink(missing_ok=True)
        fixture = Path(__file__).parent / "browser/research_fixture.py"
        self.log = (self.work / "server.log").open("ab")
        self.process = subprocess.Popen([sys.executable, str(fixture), str(self.work)]
                                        + (["--empty"] if empty else []), stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 10
        while not (self.work / "ready.json").exists() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            time.sleep(.02)
        self.assertTrue((self.work / "ready.json").exists(), (self.work / "server.log").read_text())
        ready = json.loads((self.work / "ready.json").read_text())
        self.url, self.root = ready["url"].rstrip("/"), Path(ready["root"])
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        code, info = self.request("/api/agent/info")
        self.assertEqual(code, 200, info)
        self.key = info["project_key"]

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
            self.log.close()

    def request(self, path, body=None, project=None, *, timeout=5):
        headers = {"Content-Type": "application/json", "X-Prism-Local": "1"}
        if project is not None:
            headers["X-Prism-Project"] = project
        req = urllib.request.Request(self.url + path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def initialize(self, **kwargs):
        code, state = self.request("/api/research/initialize", {"project_key": self.key, **kwargs})
        self.assertEqual(code, 200, state)
        self.assertTrue(state["initialized"])
        return state

    def turn(self, script, mode="ask", session=None):
        (self.work / "api-script.json").write_text(json.dumps(script))
        code, reply = self.request("/api/agent", {
            "project_key": self.key, "provider": "fixture", "model": "fixture-model",
            "mode": mode, "prompt": "Read and discuss the current manuscript.",
            "session_id": session, "active_file": "main.tex"})
        self.assertEqual(code, 200, reply)
        return reply["job"]

    def events(self, job, after=0, wait=.2):
        params = urllib.parse.urlencode({"project_key": self.key, "job": job, "after": after, "wait": wait})
        code, result = self.request("/api/agent/events?" + params)
        self.assertEqual(code, 200, result)
        return result

    def finish(self, job):
        events, deadline = [], time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.events(job, len(events))
            events.extend(result["events"])
            if result["done"]:
                return events
        self.fail("research fixture Turn did not finish")

    def requests(self):
        path = self.work / "api-requests.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_reading_and_manual_edit_remain_available_but_api_edits_require_initialization(self):
        self.assertEqual(self.request("/api/research"), (200, {"initialized": False}))
        self.assertFalse((self.root / "research").exists())
        self.assertEqual(self.requests(), [])
        events = self.finish(self.turn([[say("The manuscript quotes $\\ell+\\ell_+$ and discusses its source.")]]))
        self.assertIn("$\\ell+\\ell_+$", "".join(e.get("text", "") for e in events))
        self.assertFalse((self.root / "research").exists())
        old = self.request("/api/file?path=main.tex")[1]
        manual = old["content"] + "\nManual $m$.\n"
        code, result = self.request("/api/file", {"path": "main.tex", "content": manual,
                                   "base_mtime": old["mtime"]})
        self.assertEqual(code, 200, result)
        edited = manual.replace("Existing", "Existing material")
        events = self.finish(self.turn([call_chunks("text", "write_file", {"path": "main.tex", "content": edited}),
                                        [say("The prose is corrected.")]], mode="edit"))
        self.assertEqual((self.root / "main.tex").read_text(), manual)
        self.assertTrue(any(e["t"] == "tool_result" and e["error"] and "Initialize" in e["preview"] for e in events))
        self.assertEqual(self.request("/api/research")[1], {"initialized": False})

    def test_macro_definition_edit_cannot_bypass_initialization(self):
        old = self.request("/api/file?path=main.tex")[1]
        manual = "\\newcommand{\\foo}{x}\n" + old["content"]
        code, result = self.request("/api/file", {"path": "main.tex", "content": manual,
                                   "base_mtime": old["mtime"]})
        self.assertEqual(code, 200, result)
        events = self.finish(self.turn([
            call_chunks("macro", "edit_file", {"path": "main.tex", "old_string": "\\newcommand{\\foo}{x}",
                                                   "new_string": "\\newcommand{\\foo}{y}"}),
            [say("The requested change needs research initialization.")]], mode="edit"))
        self.assertEqual((self.root / "main.tex").read_text(), manual)
        self.assertTrue(any(e["t"] == "tool_result" and e["error"] and "Initialize" in e["preview"] for e in events))
        self.assertFalse((self.root / "research").exists())

    def test_percent_in_chat_cannot_hide_new_mathematics_from_the_display_check(self):
        job = self.turn([[say("50% complete; new math $z"), .35, say("=2$")]])
        during = self.events(job)
        self.assertFalse(any("$z" in e.get("text", "") for e in during["events"]))
        events = self.finish(job)
        visible = "".join(e.get("text", "") for e in events)
        self.assertNotIn("$z", visible)
        self.assertNotIn("50% complete", visible)
        self.assertIn("Initialize", visible)
        self.assertFalse((self.root / "research").exists())

    def test_api_math_write_and_fragmented_chat_are_blocked_before_initialization(self):
        original = (self.root / "main.tex").read_bytes()
        changed = original.decode().replace("$\\ell+\\ell_+$", "$z=\\ell+\\ell_+$")
        job = self.turn([call_chunks("write", "write_file", {"path": "main.tex", "content": changed}),
                         [say("A new derivation $z"), .35, say("=2$.")]], mode="edit")
        during = self.events(job)
        self.assertEqual((self.root / "main.tex").read_bytes(), original)
        self.assertFalse(any("$z" in e.get("text", "") for e in during["events"]))
        events = self.finish(job)
        self.assertEqual((self.root / "main.tex").read_bytes(), original)
        self.assertTrue(any(e["t"] == "tool_result" and e["error"] and "Initialize" in e["preview"] for e in events))
        visible = "".join(e.get("text", "") for e in events)
        self.assertNotIn("$z", visible)
        self.assertIn("Initialize", visible)
        self.assertFalse((self.root / "research").exists())

    def test_explicit_initialization_records_coverage_versions_and_unresolved_definitions(self):
        original = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*.tex")}
        state = self.initialize(question="Study existing notation.")
        self.assertEqual(set(state["coverage"]["read"]), {"main.tex", "sections/intro.tex"})
        self.assertTrue(any("missing" in x["path"] for x in state["coverage"]["unread"]))
        self.assertEqual(state["discussion"], {"mode": "markdown"})
        self.assertEqual(state["question"], "Study existing notation.")
        self.assertTrue({"\\ell", "\\ell_+"} <= {s["notation"] for s in state["symbols"]})
        terms = {t["name"]: t for t in state["terms"]}
        self.assertEqual(terms["compact"]["status"], "conflicting")
        self.assertEqual(len(terms["compact"]["sources"]), 2)
        self.assertEqual(terms["unresolved"]["status"], "unresolved")
        self.assertIsNone(terms["unresolved"]["meaning"])
        symbol = next(s for s in state["symbols"] if s["notation"] == "x")
        self.assertEqual(symbol["status"], "conflicting")
        self.assertEqual(set(symbol["definitions"]), {"a real number", "a complex number"})
        self.assertEqual(len(symbol["definition_sources"]), 2)
        for source in state["sources"]:
            self.assertNotIn("text", source)
            self.assertEqual(source["sha256"], hashlib.sha256(original[source["path"]]).hexdigest())
        self.assertEqual({p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*.tex")}, original)
        self.assertEqual(self.requests(), [], "initialization is an explicit editor operation, not a hidden model task")
        same = self.initialize(question="This must not replace the original baseline.")
        self.assertEqual(same, state)

    def test_corrections_and_source_versions_survive_server_restart(self):
        initial = self.initialize()
        code, corrected = self.request("/api/research/correct", {"project_key": self.key,
            "term_updates": [{"name": "compact", "meaning": "Author-corrected meaning."}],
            "excluded_symbols": ["\\ell_+"], "notes": "Checked the extraction report."})
        self.assertEqual(code, 200, corrected)
        self.assertEqual(next(t for t in corrected["terms"] if t["name"] == "compact")["meaning"], "Author-corrected meaning.")
        self.assertNotIn("\\ell_+", {s["notation"] for s in corrected["symbols"]})
        self.assertEqual(corrected["sources"], initial["sources"])
        self.stop(); self.start()
        self.assertEqual(self.request("/api/research"), (200, corrected))
        self.assertEqual(self.initialize(), corrected)

    def test_symbol_meaning_correction_preserves_evidence_and_survives_restart(self):
        initial = self.initialize()
        symbol = next(s for s in initial["symbols"] if s["notation"] == "x")
        self.assertEqual(symbol["status"], "conflicting")
        update = {"notation": "x", "meaning": "Author-corrected scalar."}
        code, corrected = self.request("/api/research/correct", {
            "project_key": self.key, "symbol_updates": [update]})
        self.assertEqual(code, 200, corrected)
        result = next(s for s in corrected["symbols"] if s["notation"] == "x")
        self.assertEqual(result["meaning"], "Author-corrected scalar.")
        self.assertEqual(result["status"], "corrected")
        self.assertEqual(result["sources"], symbol["sources"])
        self.assertEqual(result["definition_sources"], symbol["definition_sources"])
        self.assertEqual(result["definitions"], symbol["definitions"])
        self.assertEqual({s["canonical"] for s in corrected["symbols"]},
                         {s["canonical"] for s in initial["symbols"]})
        self.assertEqual(corrected["corrections"][-1]["symbol_updates"], [update])
        self.stop()
        self.start()
        self.assertEqual(self.request("/api/research"), (200, corrected))
        self.assertEqual(self.initialize(), corrected)

    def test_symbol_corrections_cannot_create_notation_or_set_permission_fields(self):
        initial = self.initialize()
        before = (self.root / "research/knowledge.json").read_bytes()
        for updates in ([{"notation": "q", "meaning": "Not an existing notation."}],
                        [{"notation": "x", "meaning": "A scalar.", "approved": True}],
                        [{"notation": "x", "meaning": 42}], {"notation": "x"}):
            with self.subTest(updates=updates):
                code, result = self.request("/api/research/correct", {
                    "project_key": self.key, "symbol_updates": updates})
                self.assertGreaterEqual(code, 400, result)
                self.assertIn("error", result)
                self.assertEqual((self.root / "research/knowledge.json").read_bytes(), before)
                self.assertEqual(self.request("/api/research"), (200, initial))

    def test_failed_symbol_correction_keeps_prior_meaning_and_evidence(self):
        initial = self.initialize()
        before = (self.root / "research/knowledge.json").read_bytes()
        (self.work / "fail-persistence").touch()
        code, result = self.request("/api/research/correct", {"project_key": self.key,
            "symbol_updates": [{"notation": "x", "meaning": "Cannot be saved."}]})
        self.assertGreaterEqual(code, 400, result)
        self.assertIn("error", result)
        self.assertEqual((self.root / "research/knowledge.json").read_bytes(), before)
        self.assertEqual(self.request("/api/research"), (200, initial))

    def test_empty_project_with_question_or_empty_libraries_is_valid(self):
        self.stop()
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        self.start(empty=True)
        state = self.initialize(question="Which research direction should we examine?")
        self.assertEqual(state["terms"], [])
        self.assertEqual(state["symbols"], [])
        self.assertEqual(state["sources"], [])
        self.assertTrue(state["initialized"])
        self.assertIn("research", {p.name for p in self.root.iterdir()})

    def test_empty_project_without_supplied_question_or_notation_is_valid(self):
        self.stop()
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        self.start(empty=True)
        state = self.initialize()
        self.assertEqual(state["question"], "")
        self.assertEqual(state["terms"], [])
        self.assertEqual(state["symbols"], [])
        self.assertEqual(state["sources"], [])
        self.assertEqual(self.requests(), [])

    def test_project_identity_and_persistence_failure_do_not_report_success(self):
        for path in ("/api/research/initialize", "/api/research/correct"):
            code, result = self.request(path, {"project_key": "foreign", "notes": "wrong project"})
            self.assertEqual(code, 409, result)
        self.assertEqual(self.request("/api/research", project="foreign")[0], 409)
        (self.work / "fail-persistence").touch()
        code, result = self.request("/api/research/initialize", {"project_key": self.key})
        self.assertGreaterEqual(code, 400, result)
        self.assertIn("error", result)
        self.assertEqual(self.request("/api/research")[1], {"initialized": False})
        (self.work / "fail-persistence").unlink()
        state = self.initialize()
        before = (self.root / "research/knowledge.json").read_bytes()
        (self.work / "fail-persistence").touch()
        code, result = self.request("/api/research/correct", {"project_key": self.key, "notes": "Cannot save this."})
        self.assertGreaterEqual(code, 400, result)
        self.assertEqual((self.root / "research/knowledge.json").read_bytes(), before)
        self.assertEqual(self.request("/api/research")[1], state)

    def test_initialized_api_math_edit_and_resumed_turn_load_current_baseline(self):
        self.initialize()
        new = (self.root / "main.tex").read_text().replace("$\\ell+\\ell_+$", "$x+\\ell=2$")
        events = self.finish(self.turn([call_chunks("write", "write_file", {"path": "main.tex", "content": new}),
                                        [say("The requested mathematics is now $x+\\ell=2$.")]], mode="edit"))
        self.assertEqual((self.root / "main.tex").read_text(), new)
        self.assertIn("$x+\\ell=2$", "".join(e.get("text", "") for e in events))
        session = next(e["session_id"] for e in events if e["t"] == "init")
        code, state = self.request("/api/research/correct", {"project_key": self.key,
            "term_updates": [{"name": "compact", "meaning": "Newest human meaning."}], "notes": "New baseline context."})
        self.assertEqual(code, 200, state)
        self.finish(self.turn([[say("The updated baseline is available.")]], session=session))
        contexts = [m["content"] for m in self.requests()[-1]["body"]["messages"]
                    if m["role"] == "system" and m["content"].startswith("[Prism research baseline]")]
        self.assertEqual(len(contexts), 1)
        self.assertIn("Newest human meaning.", contexts[0])
        providers = self.request("/api/agent/info")[1]["providers"]
        self.assertFalse(providers[0]["notation_protection"], "baseline initialization does not claim later notation protection")

    def test_initialization_waits_for_an_active_turn(self):
        job = self.turn([[say("Discussing the existing material."), .6]])
        code, result = self.request("/api/research/initialize", {"project_key": self.key})
        self.assertEqual(code, 409, result)
        self.assertFalse((self.root / "research/knowledge.json").exists())
        self.finish(job)
        self.initialize()
