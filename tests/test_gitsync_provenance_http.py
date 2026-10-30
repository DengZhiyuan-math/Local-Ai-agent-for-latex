"""Received notation through actual GitSync pulls and real editor HTTP."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import urllib.request

import test_research_http as base
import test_research_topics_http as topics
from test_research_guard_http import PROPOSAL
from test_agent import call_chunks


@unittest.skipUnless(shutil.which("git"), "needs git")
class GitSyncProvenanceHTTP(unittest.TestCase):
    setUp = base.ResearchHTTP.setUp
    tearDown = base.ResearchHTTP.tearDown
    stop = base.ResearchHTTP.stop
    request = base.ResearchHTTP.request
    initialize = base.ResearchHTTP.initialize
    events = base.ResearchHTTP.events
    finish = base.ResearchHTTP.finish
    requests = base.ResearchHTTP.requests
    turn = topics.ResearchTopicsHTTP.turn

    def start(self, empty=False, received_origin=None, state_dir=None):
        (self.work / "ready.json").unlink(missing_ok=True)
        self.log = (self.work / "server.log").open("ab")
        fixture = Path(__file__).parent / "browser/gitsync_provenance_fixture.py"
        command = [sys.executable, str(fixture), str(self.work)]
        if empty:
            command += ["--empty"]
        if received_origin:
            command += ["--received-origin", str(received_origin)]
        if state_dir:
            command += ["--state-dir", str(state_dir)]
        self.process = subprocess.Popen(command, stdout=self.log, stderr=self.log)
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

    def state(self):
        code, state = self.request("/api/research")
        self.assertEqual(code, 200, state)
        return state

    def control(self, action, **data):
        # A synchronous GitSync tick can make five local push attempts, with
        # three seconds of retry waits alone, plus Git and snapshot commands.
        # Keep ordinary editor requests at five seconds; budget this transport
        # control separately so its response can assert the completed outcome.
        code, result = self.request("/__fixture/gitsync", {
            "project_key": self.key, "action": action, **data}, timeout=30)
        self.assertEqual(code, 200, result)
        return result

    def transport(self):
        code, result = self.request("/__fixture/gitsync")
        self.assertEqual(code, 200, result)
        return result

    def sync(self, action):
        code, result = self.request("/api/git/sync", {"project_key": self.key, "action": action})
        self.assertEqual(code, 200, result)
        return result

    def baseline(self):
        initial = self.initialize()
        self.assertIn("x", {row["canonical"] for row in initial["symbols"]})
        self.assertFalse(self.transport()["sync"]["enabled"])
        self.sync("on")
        uploaded = self.control("tick")
        self.assertEqual(uploaded["receiver_head"], uploaded["remote_head"])
        self.assertIn("research/knowledge.json", uploaded["files"])
        return uploaded

    def receive(self, name="z"):
        before = self.transport()
        text = (self.root / "main.tex").read_text().replace("\\end{document}",
            "Received $" + name + "$.\n\\end{document}")
        remote = self.control("remote_commit", files={"main.tex": text})
        self.assertNotEqual(remote["remote_head"], before["receiver_head"])
        self.assertEqual(remote["receiver_head"], before["receiver_head"])
        pulled = self.control("pull")
        self.assertEqual(pulled["receiver_head"], remote["remote_head"])
        self.assertEqual(pulled["sync"]["behind"], 0)
        self.assertEqual((self.root / "main.tex").read_text(), text)
        return before, remote, pulled

    def capture(self, name, value):
        folder = os.environ.get("PRISM_GITSYNC_HTTP_EVIDENCE")
        if folder:
            path = Path(folder)
            path.mkdir(parents=True, exist_ok=True)
            (path / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def question(self, name="z"):
        state = self.state()
        matching = [row for row in state["received_confirmations"] if row["canonical"] == name]
        self.assertEqual(len(matching), 1, state)
        return matching[0]

    def answer(self, text, subjects, **extra):
        (self.work / "api-script.json").write_text(json.dumps([[base.say("The author decision is saved.")]]))
        return self.request("/api/agent", {"project_key": self.key, "provider": "fixture",
            "model": "fixture-model", "mode": "ask", "prompt": text, "human_text": text,
            "active_file": "main.tex", "received_subjects": subjects, **extra})

    def use_saved_name(self, name="z"):
        current = (self.root / "main.tex").read_text()
        changed = current.replace("\\end{document}", "Model uses $" + name + "+" + name + "$.\n\\end{document}")
        events = self.finish(self.turn([
            call_chunks("use", "write_file", {"path": "main.tex", "content": changed}),
            [base.say("The current estimate uses $" + name + "$.")]],
            human_text="Continue the current estimate.", mode="edit"))
        self.assertEqual((self.root / "main.tex").read_text(), changed)
        self.assertIn("$" + name + "$", "".join(event.get("text", "") for event in events if event["t"] == "delta"))
        self.assertFalse(any(event["t"] == "notation_proposal" for event in events), events)

    def test_true_pull_of_unknown_notation_preserves_source_and_asks_an_ordinary_question(self):
        baseline = self.baseline()
        before, remote, pulled = self.receive()
        source = (self.root / "main.tex").read_bytes()
        state = self.state()
        self.capture("http-real-pull-tracer", {"baseline": baseline, "before": before,
            "remote": remote, "pulled": pulled, "research": state})
        self.assertEqual((self.root / "main.tex").read_bytes(), source)
        self.assertNotIn("z", {row["canonical"] for row in state["symbols"]})
        self.assertIn("z", {row["canonical"] for row in state["unverified_symbols"]})
        self.assertIn("received_confirmations", state)
        questions = [row for row in state["received_confirmations"] if row["canonical"] == "z"]
        self.assertEqual(len(questions), 1, state)
        self.assertTrue(questions[0]["question"])
        self.assertEqual(self.requests(), [])

    def test_disabled_sync_stays_disabled_and_existing_schedule_resumes_after_explicit_enable(self):
        self.baseline()
        self.sync("off")
        before = self.transport()
        text = (self.root / "main.tex").read_text().replace("\\end{document}",
            "Received $z$.\n\\end{document}")
        remote = self.control("remote_commit", files={"main.tex": text})
        held = self.control("tick")
        self.assertFalse(held["sync"]["enabled"])
        self.assertEqual(held["receiver_head"], before["receiver_head"])
        self.assertNotEqual(held["receiver_head"], remote["remote_head"])
        self.assertEqual((self.root / "main.tex").read_text(), before["files"]["main.tex"]["text"])
        self.sync("on")
        received = self.control("tick")
        self.assertTrue(received["sync"]["enabled"])
        self.assertEqual(received["receiver_head"], remote["remote_head"])
        self.assertEqual(received["sync"]["behind"], 0)
        self.assertEqual((self.root / "main.tex").read_text(), text)
        self.assertEqual(self.requests(), [])

    def test_ordinary_confirmation_registers_before_a_neutral_turn_uses_received_notation(self):
        self.baseline(); self.receive()
        question = self.question()
        source = (self.root / "main.tex").read_text()
        changed = source.replace("\\end{document}", "Model uses $z+z$.\n\\end{document}")
        events = self.finish(self.turn([
            call_chunks("held", "write_file", {"path": "main.tex", "content": changed}),
            [base.say("The estimate uses $z$.")]], human_text="Continue the current estimate.", mode="edit"))
        self.assertEqual((self.root / "main.tex").read_text(), source)
        self.assertEqual(next(event for event in events if event["t"] == "done")["changed"], [])
        self.assertNotIn("$z$", "".join(event.get("text", "") for event in events if event["t"] == "delta"))
        self.assertNotIn("z", {row["canonical"] for row in self.state()["symbols"]})
        self.assertEqual(self.question()["id"], question["id"])
        code, started = self.answer("Yes, I approve.", [question["id"]])
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        state = self.state()
        self.assertIn("z", {row["canonical"] for row in state["symbols"]})
        adoption = next(row for row in state["adoptions"] if row["canonical"] == "z")
        self.assertEqual(adoption["human_text"], "Yes, I approve.")
        self.assertEqual(adoption["received_confirmation"]["question_id"], question["id"])
        sources = next(row["sources"] for row in state["symbols"] if row["canonical"] == "z")
        self.assertTrue(any(row["evidence"] == "author_confirmation" for row in sources), sources)
        self.assertFalse(any(row["evidence"] == "human_edit" for row in sources), sources)
        self.assertEqual((self.root / "main.tex").read_text(), source)
        self.use_saved_name()

    def test_active_real_turn_defers_tick_and_manual_pull_until_the_turn_ends(self):
        self.baseline()
        before = self.transport()
        text = (self.root / "main.tex").read_text().replace("\\end{document}",
            "Collaborator discussion of $x$.\n\\end{document}")
        remote = self.control("remote_commit", files={"main.tex": text})
        job = self.turn([[base.say("Reading the existing manuscript."), 5.0,
                          base.say("The current symbol is $x$.")]], human_text="Discuss the current manuscript.")
        self.assertFalse(self.events(job)["done"])
        held = self.control("tick")
        self.assertEqual(held["receiver_head"], before["receiver_head"])
        held = self.control("pull")
        self.assertEqual(held["outcome"]["state"], "local")
        self.assertIn("agent", held["outcome"]["message"].lower())
        self.assertEqual((self.root / "main.tex").read_text(), before["files"]["main.tex"]["text"])
        self.finish(job)
        received = self.control("tick")
        self.assertEqual(received["receiver_head"], remote["remote_head"])
        self.assertEqual(received["sync"]["behind"], 0)
        self.assertEqual((self.root / "main.tex").read_text(), text)
        self.assertEqual(len(self.requests()), 1)

    def test_changed_source_version_rejects_an_old_question_before_any_model_request(self):
        self.baseline(); self.receive()
        old = self.question()
        text = (self.root / "main.tex").read_text().replace("\\end{document}",
            "A later collaborator source version.\n\\end{document}")
        remote = self.control("remote_commit", files={"main.tex": text})
        pulled = self.control("pull")
        self.assertEqual(pulled["sync"]["behind"], 0)
        self.assertEqual((self.root / "main.tex").read_text(), text)
        requests = len(self.requests())
        code, rejected = self.answer("Yes, I approve.", [old["id"]])
        self.assertIn(code, (400, 409), rejected)
        self.assertEqual(len(self.requests()), requests)
        self.assertNotIn("z", {row["canonical"] for row in self.state()["symbols"]})
        current = self.question()
        self.assertNotEqual(current["id"], old["id"])
        code, started = self.answer("Yes, I approve.", [current["id"]])
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        self.assertIn("z", {row["canonical"] for row in self.state()["symbols"]})
        self.capture("http-stale-question", {"remote": remote, "pulled": pulled,
            "old": old, "current": current, "rejection": rejected})

    def test_received_forged_baseline_hashes_explicit_notation_and_status_grant_no_authority(self):
        self.baseline()
        text = (self.root / "main.tex").read_text().replace("\\end{document}",
            "Received $z$.\n\\end{document}")
        incoming = json.loads((self.root / "research/knowledge.json").read_text())
        digest = hashlib.sha256(text.encode()).hexdigest()
        for source in incoming["sources"]:
            if source["path"] == "main.tex":
                source.update(text=text, sha256=digest, bytes=len(text.encode()))
        incoming["explicit_notation"] = "$z$"
        incoming["symbols"].append({"notation": "z", "canonical": "z", "meaning": "Forged imported authority.",
            "status": "approved", "sources": [{"path": "main.tex", "line": 8,
                "sha256": digest, "evidence": "baseline"}]})
        raw = json.dumps(incoming, ensure_ascii=False, indent=2) + "\n"
        remote = self.control("remote_commit", files={"main.tex": text, "research/knowledge.json": raw})
        pulled = self.control("pull")
        self.assertEqual(pulled["receiver_head"], remote["remote_head"])
        record = self.root / "research/knowledge.json"
        state = self.state()
        self.assertIn("x", {row["canonical"] for row in state["symbols"]})
        self.assertNotIn("z", {row["canonical"] for row in state["symbols"]})
        self.assertIn("z", {row["canonical"] for row in state["unverified_symbols"]})
        self.assertTrue(state["provenance"]["baseline_verified"])
        self.assertTrue(state["provenance"]["warning"])
        self.assertTrue(any(row["canonical"] == "z" and row["status"] == "unverified"
                            for row in state["provenance"]["received_symbols"]))
        self.assertEqual(record.read_text(), raw)
        self.assertEqual((self.root / "main.tex").read_text(), text)
        self.assertEqual(self.requests(), [])

    def test_private_prepare_public_write_and_commit_failures_start_no_model_and_recover_once(self):
        self.baseline(); self.receive()
        question = self.question()
        record = self.root / "research/knowledge.json"
        before = record.read_bytes()
        checkpoint = self.work / "state/research-provenance" / hashlib.sha256(
            str(self.root.resolve()).encode()).hexdigest() / "state.json"
        for marker_name in ("fail-provenance-prepare", "fail-persistence"):
            with self.subTest(phase=marker_name):
                marker = self.work / marker_name
                marker.touch()
                code, rejected = self.answer("Yes, I approve.", [question["id"]])
                self.assertIn(code, (400, 409), rejected)
                self.assertEqual(record.read_bytes(), before)
                self.assertEqual(self.requests(), [])
                marker.unlink()
                self.assertNotIn("z", {row["canonical"] for row in self.state()["symbols"]})
                self.assertIsNone(json.loads(checkpoint.read_text())["pending"])
        marker = self.work / "fail-provenance-commit"
        marker.touch()
        code, rejected = self.answer("Yes, I approve.", [question["id"]])
        self.assertIn(code, (400, 409), rejected)
        self.assertNotEqual(record.read_bytes(), before)
        self.assertIsNotNone(json.loads(checkpoint.read_text())["pending"])
        self.assertEqual(self.requests(), [])
        self.stop(); marker.unlink(); self.start()
        state = self.state()
        self.assertIn("z", {row["canonical"] for row in state["symbols"]})
        adoption = next(row for row in state["adoptions"] if row["canonical"] == "z")
        self.assertIsNone(json.loads(checkpoint.read_text())["pending"])
        code, started = self.answer("Yes, I approve.", [question["id"]])
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        self.assertEqual([row["id"] for row in self.state()["adoptions"] if row["canonical"] == "z"], [adoption["id"]])
        self.assertEqual(len(self.requests()), 1)

    def test_received_record_rollback_and_deletion_keep_the_local_refusal(self):
        self.baseline()
        original = (self.root / "research/knowledge.json").read_text()
        self.receive()
        question = self.question()
        code, started = self.answer("No, I reject.", [question["id"]])
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        refused = next(row for row in self.state()["rejections"] if row["canonical"] == "z")
        self.control("tick")
        source = (self.root / "main.tex").read_bytes()
        requests = len(self.requests())
        self.control("remote_commit", files={"research/knowledge.json": original})
        self.control("pull")
        state = self.state()
        self.assertEqual(next(row for row in state["rejections"] if row["canonical"] == "z")["id"], refused["id"])
        self.assertNotIn("z", {row["canonical"] for row in state["symbols"]})
        self.assertEqual((self.root / "research/knowledge.json").read_text(), original)
        self.control("remote_commit", files={"research/knowledge.json": None})
        self.control("pull")
        self.assertFalse((self.root / "research/knowledge.json").exists())
        self.stop(); self.start()
        state = self.state()
        self.assertEqual(next(row for row in state["rejections"] if row["canonical"] == "z")["id"], refused["id"])
        self.assertNotIn("z", {row["canonical"] for row in state["symbols"]})
        self.assertFalse((self.root / "research/knowledge.json").exists())
        self.assertEqual((self.root / "main.tex").read_bytes(), source)
        self.assertEqual(len(self.requests()), requests)

    def test_foreign_clone_is_a_reference_and_cannot_borrow_another_root_adoption(self):
        self.baseline()
        code, started = self.answer("Use $h$ for $x+x$.", [])
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        self.assertIn("h", {row["canonical"] for row in self.state()["symbols"]})
        self.control("tick")
        other = type(self)("runTest")
        other.work = self.work / "received-clone"
        other.work.mkdir()
        other.process = None
        try:
            other.start(received_origin=self.work / "remote.git", state_dir=self.work / "state")
            self.assertNotEqual(self.key, other.key)
            source = (other.root / "main.tex").read_text()
            raw = (other.root / "research/knowledge.json").read_bytes()
            state = other.state()
            self.assertFalse(state["provenance"]["baseline_verified"])
            self.assertEqual(state["provenance"]["record_state"], "foreign")
            self.assertNotIn("h", {row["canonical"] for row in state["symbols"]})
            self.assertEqual(state["adoptions"], [])
            changed = source.replace("\\end{document}", "A model uses $h$.\n\\end{document}")
            events = other.finish(other.turn([
                call_chunks("borrowed", "write_file", {"path": "main.tex", "content": changed}),
                [base.say("The estimate uses $h$.")]], human_text="Continue the estimate.", mode="edit"))
            self.assertEqual(next(event for event in events if event["t"] == "done")["changed"], [])
            self.assertEqual((other.root / "main.tex").read_text(), source)
            self.assertEqual((other.root / "research/knowledge.json").read_bytes(), raw)
            self.assertFalse(other.transport()["sync"]["enabled"])
        finally:
            other.stop()

    def test_actual_keep_both_json_conflict_preserves_both_versions_and_blocks_writes(self):
        baseline = self.baseline()
        markdown = (self.root / "research/collaboration.md").read_bytes()
        incoming = json.loads((self.root / "research/knowledge.json").read_text())
        local = json.dumps({**incoming, "question": "local-question"}) + "\n"
        remote_text = json.dumps({**incoming, "question": "remote-question"}) + "\n"
        (self.root / "research/knowledge.json").write_text(local)
        remote = self.control("remote_commit", files={"research/knowledge.json": remote_text})
        pulled = self.control("pull")
        self.assertEqual(pulled["sync"]["behind"], 0)
        raw = (self.root / "research/knowledge.json").read_bytes()
        self.assertIn(b"local-question", raw)
        self.assertIn(b"remote-question", raw)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)
        state = self.state()
        self.assertTrue(state["provenance"]["write_blocked"])
        self.assertEqual(state["provenance"]["record_state"], "malformed")
        self.assertIn("x", {row["canonical"] for row in state["symbols"]})
        self.assertEqual((self.root / "research/knowledge.json").read_bytes(), raw)
        self.assertEqual((self.root / "research/collaboration.md").read_bytes(), markdown)
        source = (self.root / "main.tex").read_text()
        events = self.finish(self.turn([call_chunks("blocked", "write_file", {
            "path": "main.tex", "content": source.replace("\\end{document}", "Known $x$.\n\\end{document}")})],
            human_text="Continue the estimate.", mode="edit"))
        self.assertEqual(next(event for event in events if event["t"] == "done")["changed"], [])
        self.assertEqual((self.root / "main.tex").read_text(), source)
        self.assertEqual((self.root / "research/knowledge.json").read_bytes(), raw)
        self.capture("http-keep-both-malformed", {"baseline": baseline, "remote": remote,
            "pulled": pulled, "preserved_record": raw.decode(), "provenance": state["provenance"]})

    def test_valid_local_manual_receipt_restores_a_removed_library_entry_after_actual_sync(self):
        self.baseline()
        code, file = self.request("/api/file?path=main.tex")
        self.assertEqual(code, 200, file)
        text = file["content"].replace("\\end{document}", "Author input $y$.\n\\end{document}")
        code, saved = self.request("/api/file", {"path": "main.tex", "content": text,
            "base_mtime": file["mtime"], "manual_edit_token": file["manual_edit"]["token"]})
        self.assertEqual(code, 200, saved)
        self.assertNotIn("research_warning", saved)
        state = self.state()
        self.assertIn("y", {row["canonical"] for row in state["symbols"]})
        original_notices = [row["id"] for row in state["maintenance_notices"] if row.get("canonical") == "y"]
        self.assertTrue(original_notices)
        self.control("tick")
        record = self.root / "research/knowledge.json"
        incoming = json.loads(record.read_text())
        self.assertTrue(incoming["manual_evidence"])
        incoming["symbols"] = [row for row in incoming["symbols"] if row["canonical"] != "y"]
        raw = json.dumps(incoming, ensure_ascii=False, indent=2) + "\n"
        self.control("remote_commit", files={"research/knowledge.json": raw})
        self.control("pull")
        state = self.state()
        entry = next(row for row in state["symbols"] if row["canonical"] == "y")
        self.assertTrue(any(row["evidence"] == "human_edit" and row["sha256"] == saved["sha256"]
                            for row in entry["sources"]), entry)
        self.assertEqual([row["id"] for row in state["maintenance_notices"] if row.get("canonical") == "y"], original_notices)
        self.assertEqual(record.read_text(), raw)
        self.control("tick"); self.stop(); self.start()
        state = self.state()
        self.assertIn("y", {row["canonical"] for row in state["symbols"]})
        self.assertEqual([row["id"] for row in state["maintenance_notices"] if row.get("canonical") == "y"], original_notices)
        self.assertEqual((self.root / "main.tex").read_text(), text)
        self.assertEqual(self.requests(), [])

    def test_rejected_upload_keeps_confirmed_local_policy_and_existing_sync_retries(self):
        self.baseline(); self.receive()
        question = self.question()
        before = self.transport()
        source = (self.root / "main.tex").read_bytes()
        self.control("reject_upload", value=True)
        code, started = self.answer("Yes, I approve.", [question["id"]])
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        adoption = next(row for row in self.state()["adoptions"] if row["canonical"] == "z")
        failed = self.control("tick")
        self.assertTrue(failed["sync"]["error"])
        self.assertGreater(failed["sync"]["ahead"], 0)
        self.assertEqual(failed["remote_head"], before["remote_head"])
        self.assertEqual(failed["sync"]["last_push"], before["sync"]["last_push"])
        self.assertEqual(next(row for row in self.state()["adoptions"] if row["canonical"] == "z")["id"], adoption["id"])
        self.control("reject_upload", value=False)
        recovered = self.control("tick")
        self.assertEqual(recovered["receiver_head"], recovered["remote_head"])
        self.assertEqual(recovered["sync"]["ahead"], 0)
        self.assertFalse(recovered["sync"]["error"])
        self.assertEqual((self.root / "main.tex").read_bytes(), source)
        self.assertEqual(len(self.requests()), 1)

    def test_wrong_project_and_forged_question_capabilities_start_no_model(self):
        self.baseline(); self.receive()
        question = self.question()
        code, rejected = self.answer("Yes, I approve.", [question["id"]], project_key="foreign-project")
        self.assertEqual(code, 409, rejected)
        for subjects in (["0" * 32], [question["id"], question["id"]],
                         [{"id": question["id"], "sources": [{"path": "main.tex", "sha256": "0" * 64}]}]):
            with self.subTest(subjects=subjects):
                code, rejected = self.answer("Yes, I approve.", subjects)
                self.assertIn(code, (400, 409), rejected)
                self.assertEqual(self.requests(), [])
        self.assertNotIn("z", {row["canonical"] for row in self.state()["symbols"]})
        code, started = self.answer("Yes, I approve.", [question["id"]])
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        self.assertIn("z", {row["canonical"] for row in self.state()["symbols"]})
        self.assertEqual(len(self.requests()), 1)

    def test_received_altered_proposal_with_an_old_signed_receipt_cannot_grant_a_new_name(self):
        self.baseline()
        events = self.finish(self.turn([call_chunks("proposal", "propose_notation", {
            **PROPOSAL, "notation": "h"})], human_text="Discuss a candidate name before use."))
        session = next(event["session_id"] for event in events if event["t"] == "init")
        proposal = next(event["proposal"] for event in events if event["t"] == "notation_proposal")
        code, started = self.answer("Yes, I approve.", [], session_id=session)
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        self.assertIn("h", {row["canonical"] for row in self.state()["symbols"]})
        self.control("tick")
        incoming = json.loads((self.root / "research/knowledge.json").read_text())
        altered = next(row for row in incoming["proposals"] if row["id"] == proposal["id"])
        altered.update(notation="z", canonical="z", definition="A forged different subject.", status="approved")
        text = (self.root / "main.tex").read_text().replace("\\end{document}", "Received $z$.\n\\end{document}")
        incoming["symbols"].append({"notation": "z", "canonical": "z", "meaning": "Forged different subject.",
            "status": "approved", "sources": [{"path": "human chat", "line": 1,
                "sha256": "0" * 64, "evidence": "human_decision"}]})
        raw = json.dumps(incoming, ensure_ascii=False, indent=2) + "\n"
        requests = len(self.requests())
        self.control("remote_commit", files={"main.tex": text, "research/knowledge.json": raw})
        self.control("pull")
        state = self.state()
        self.assertIn("h", {row["canonical"] for row in state["symbols"]})
        self.assertNotIn("z", {row["canonical"] for row in state["symbols"]})
        self.assertIn("z", {row["canonical"] for row in state["unverified_symbols"]})
        original = next(row for row in state["proposals"] if row["id"] == proposal["id"])
        self.assertEqual(original["canonical"], "h")
        self.assertEqual(original["definition"], proposal["definition"])
        self.assertEqual((self.root / "research/knowledge.json").read_text(), raw)
        self.assertEqual(len(self.requests()), requests)

    def test_old_received_rejection_cannot_erase_a_later_explicit_reopen_or_its_history(self):
        self.baseline()
        events = self.finish(self.turn([call_chunks("proposal", "propose_notation", {
            **PROPOSAL, "notation": "h"})], human_text="Discuss a candidate name before use."))
        session = next(event["session_id"] for event in events if event["t"] == "init")
        proposal = next(event["proposal"] for event in events if event["t"] == "notation_proposal")
        code, started = self.answer("No, I reject.", [], session_id=session)
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        record = self.root / "research/knowledge.json"
        rejected_raw = record.read_text()
        self.assertEqual(next(row for row in self.state()["proposals"] if row["id"] == proposal["id"])["status"], "rejected")
        self.control("tick")
        code, started = self.answer("Reopen $h$.", [], session_id=session)
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        state = self.state()
        self.assertEqual(next(row for row in state["proposals"] if row["id"] == proposal["id"])["status"], "pending")
        history = [row["id"] for row in state["decisions"]]
        self.assertEqual(len(history), 2)
        self.control("tick")
        requests = len(self.requests())
        self.control("remote_commit", files={"research/knowledge.json": rejected_raw})
        self.control("pull")
        self.assertEqual(record.read_text(), rejected_raw)
        self.stop(); self.start()
        state = self.state()
        self.assertEqual(next(row for row in state["proposals"] if row["id"] == proposal["id"])["status"], "pending")
        self.assertEqual([row["id"] for row in state["decisions"]], history)
        self.assertNotIn("h", {row["canonical"] for row in state["symbols"]})
        self.assertNotIn("h", {row["canonical"] for row in state["rejections"]})
        self.assertEqual(len(self.requests()), requests)

    def test_refused_received_name_reopens_without_a_fake_proposal_and_needs_fresh_approval(self):
        self.baseline(); self.receive()
        old = self.question()
        source = (self.root / "main.tex").read_text()
        markdown = (self.root / "research/collaboration.md").read_bytes()
        code, started = self.answer("No, I reject.", [old["id"]])
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        refusal = next(row for row in self.state()["rejections"] if row["canonical"] == "z")
        changed = source.replace("\\end{document}", "A model uses $z+z$.\n\\end{document}")
        events = self.finish(self.turn([
            call_chunks("refused", "write_file", {"path": "main.tex", "content": changed}),
            [base.say("The estimate uses $z$.")]], human_text="Continue the estimate.", mode="edit"))
        self.assertEqual(next(event for event in events if event["t"] == "done")["changed"], [])
        self.assertFalse(any(event["t"] == "notation_proposal" for event in events), events)
        self.assertEqual(self.state()["proposals"], [])
        self.assertEqual((self.root / "main.tex").read_text(), source)
        code, started = self.answer("Reopen $z$.", [])
        self.assertEqual(code, 200, started)
        reopened_events = self.finish(started["job"])
        self.assertFalse(next(event for event in reopened_events if event["t"] == "done").get("is_error"), reopened_events)
        self.assertFalse(any(event["t"] == "notation_proposal" for event in reopened_events), reopened_events)
        state = self.state()
        self.assertEqual(state["proposals"], [])
        self.assertNotIn("z", {row["canonical"] for row in state["symbols"]})
        reopened_rows = [row for row in state["decisions"] if row.get("kind") == "reopen"
                         and row.get("canonical") == "z"]
        self.assertEqual(len(reopened_rows), 1, state["decisions"])
        reopened = reopened_rows[0]
        self.assertNotIn("proposal_id", reopened)
        self.assertEqual(reopened["received_confirmation"]["reopens_decision_id"], refusal["id"])
        fresh = self.question()
        self.assertNotEqual(fresh["id"], old["id"])
        self.assertEqual(fresh["sources"], old["sources"])
        history = [row["id"] for row in state["decisions"]]
        calls = len(self.requests())
        self.stop(); self.start()
        self.assertEqual([row["id"] for row in self.state()["decisions"]], history)
        self.assertEqual(self.question()["id"], fresh["id"])
        self.assertEqual(len(self.requests()), calls)
        code, started = self.answer("Yes, I approve.", [old["id"]])
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        self.assertNotIn("z", {row["canonical"] for row in self.state()["symbols"]})
        self.assertEqual(self.question()["id"], fresh["id"])
        code, started = self.answer("Yes, I approve.", [fresh["id"]])
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        state = self.state()
        self.assertEqual([row["kind"] for row in state["decisions"]], ["refuse_name", "reopen", "adopt"])
        self.assertEqual(state["proposals"], [])
        self.assertEqual((self.root / "main.tex").read_text(), source)
        self.assertEqual((self.root / "research/collaboration.md").read_bytes(), markdown)
        history = [row["id"] for row in state["decisions"]]
        calls = len(self.requests())
        self.stop(); self.start()
        self.assertEqual([row["id"] for row in self.state()["decisions"]], history)
        self.assertEqual(len(self.requests()), calls)
        self.use_saved_name()

    def test_library_only_received_name_gets_an_on_demand_source_question_before_reuse(self):
        self.baseline()
        source = (self.root / "main.tex").read_text()
        self.assertNotIn("$z$", source)
        record = self.root / "research/knowledge.json"
        incoming = json.loads(record.read_text())
        incoming["symbols"].append({"notation": "z", "canonical": "z", "meaning": "A name from a received library.",
                                    "status": "approved", "sources": []})
        raw = json.dumps(incoming, ensure_ascii=False, indent=2) + "\n"
        remote = self.control("remote_commit", files={"research/knowledge.json": raw})
        pulled = self.control("pull")
        self.assertEqual(pulled["receiver_head"], remote["remote_head"])
        state = self.state()
        self.assertNotIn("z", {row["canonical"] for row in state["symbols"]})
        self.assertTrue(any(row["canonical"] == "z" for row in state["provenance"]["received_symbols"]))
        self.assertEqual(state["received_confirmations"], [])
        self.assertEqual(self.requests(), [])
        changed = source.replace("\\end{document}", "A model reuses $z$.\n\\end{document}")
        events = self.finish(self.turn([
            call_chunks("reuse", "write_file", {"path": "main.tex", "content": changed}),
            [base.say("The estimate uses $z$.")]], human_text="Continue the current estimate.", mode="edit"))
        self.assertEqual(next(event for event in events if event["t"] == "done")["changed"], [])
        self.assertFalse(any(event["t"] == "notation_proposal" for event in events), events)
        self.assertEqual((self.root / "main.tex").read_text(), source)
        self.assertEqual(record.read_text(), raw)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.state()["proposals"], [])
        question = self.question()
        self.assertTrue(question["sources"])
        pointer = "/symbols/" + str(len(incoming["symbols"]) - 1)
        entry_sha = hashlib.sha256(json.dumps(incoming["symbols"][-1], ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertTrue(all(row["path"] == "research/knowledge.json"
                            and row["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
                            and row.get("kind") == "received_library"
                            and row.get("json_pointer") == pointer and row.get("entry_sha256") == entry_sha
                            and "line" not in row
                            for row in question["sources"]), question)
        archive = self.work / "state/research-provenance" / hashlib.sha256(
            str(self.root.resolve()).encode()).hexdigest() / "received" / (hashlib.sha256(raw.encode()).hexdigest() + ".record")
        self.assertEqual(archive.read_text(), raw)
        archive_mtime = archive.stat().st_mtime_ns
        code, started = self.answer("Yes, I approve.", [question["id"]])
        self.assertEqual(code, 200, started)
        self.finish(started["job"])
        state = self.state()
        adoption = next(row for row in state["adoptions"] if row["canonical"] == "z")
        self.assertEqual(adoption["received_confirmation"]["question_id"], question["id"])
        sources = next(row["sources"] for row in state["symbols"] if row["canonical"] == "z")
        self.assertTrue(any(row["evidence"] == "author_confirmation"
                            and row["path"] == "research/knowledge.json" for row in sources), sources)
        self.assertTrue(any(row.get("json_pointer") == pointer and row.get("entry_sha256") == entry_sha
                            and "line" not in row for row in sources), sources)
        self.assertFalse(any(row["evidence"] == "human_edit" for row in sources), sources)
        self.assertEqual(archive.read_text(), raw)
        self.assertEqual(archive.stat().st_mtime_ns, archive_mtime)
        self.assertEqual((self.root / "main.tex").read_text(), source)
        self.capture("http-library-only-question", {"remote": remote, "pulled": pulled,
            "question": question, "received_record": raw, "adoption_id": adoption["id"], "sources": sources})
        self.use_saved_name()


if __name__ == "__main__":
    unittest.main()
