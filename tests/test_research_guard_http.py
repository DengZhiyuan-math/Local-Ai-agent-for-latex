"""Notation review through the real local editor and streamed API interfaces.

Only the isolated localhost FakeAPI runs. The file observer and unchanged file
version check cover the full Turn, including the manager's final snapshot step.
"""
import json
import threading
import time
import unittest

import test_research_http as base
from test_agent import call_chunks


def call(*items, text=""):
    return {"choices": [{"delta": {"content": text, "tool_calls": [
        {"index": index, "id": identity, "function": {
            "name": name, "arguments": json.dumps(args)}}
        for index, (identity, name, args) in enumerate(items)]}}]}


PROPOSAL = {"notation": "z", "definition": "The sum of the two existing lengths.",
            "scope": "The current estimate.", "reason": "Discuss a shorter name before use.",
            "source_expression": r"\ell+\ell_+"}


class ResearchGuardHTTP(unittest.TestCase):
    # Reuse fixture methods, without inheriting the initialization test cases.
    setUp = base.ResearchHTTP.setUp
    tearDown = base.ResearchHTTP.tearDown
    start = base.ResearchHTTP.start
    stop = base.ResearchHTTP.stop
    request = base.ResearchHTTP.request
    initialize = base.ResearchHTTP.initialize
    turn = base.ResearchHTTP.turn
    events = base.ResearchHTTP.events
    finish = base.ResearchHTTP.finish
    requests = base.ResearchHTTP.requests

    def state(self):
        code, state = self.request("/api/research")
        self.assertEqual(code, 200, state)
        return state

    @staticmethod
    def visible(events):
        return "\n".join(e.get("text", "") for e in events
                         if e["t"] in ("delta", "thinking"))

    def assert_not_adopted(self, notation):
        state = self.state()
        self.assertNotIn(notation, {s["canonical"] for s in state["symbols"]})
        return state

    def unchanged_turn(self, script, mode="edit"):
        path = self.root / "main.tex"

        def version():
            stat = path.stat()
            return stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, path.read_bytes()

        before, samples, errors = version(), [], []
        stopped = threading.Event()

        def observe():
            while not stopped.is_set():
                try:
                    samples.append(version())
                except OSError as error:
                    errors.append(error)
                stopped.wait(.002)

        observer = threading.Thread(target=observe)
        observer.start()
        try:
            job = self.turn(script, mode=mode)
            events, deadline = [], time.monotonic() + 10
            while time.monotonic() < deadline:
                result = self.events(job, len(events))
                events.extend(result["events"])
                # A candidate may be displayed only after its durable version
                # exists, not after a later finalization or browser action.
                for event in result["events"]:
                    if event["t"] == "notation_proposal":
                        saved = json.loads((self.root / "research/knowledge.json").read_text())
                        self.assertIn(event["proposal"], saved["proposals"])
                if result["done"]:
                    break
            else:
                self.fail("research fixture Turn did not finish")
            samples.append(version())
        finally:
            stopped.set()
            observer.join(2)
        self.assertEqual(errors, [])
        self.assertTrue(samples)
        self.assertTrue(all(sample == before for sample in samples),
                        "the source content and filesystem version must remain unchanged throughout the Turn")
        return events

    def test_unknown_write_is_held_before_any_source_application_and_stops_the_turn(self):
        self.initialize()
        original = (self.root / "main.tex").read_text()
        changed = original.replace(r"$\ell+\ell_+$", "$z$")
        events = self.unchanged_turn([
            call_chunks("draft", "write_file", {"path": "main.tex", "content": changed}),
            [base.say("The manuscript now uses $z$.")]])
        proposals = [e["proposal"] for e in events if e["t"] == "notation_proposal"]
        self.assertEqual([p["canonical"] for p in proposals], ["z"])
        self.assertEqual(proposals[0]["source_expression"], r"\ell+\ell_+")
        self.assertTrue(proposals[0]["needs_details"])
        self.assertEqual(proposals[0]["origin"]["path"], "main.tex")
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.visible(events), "")
        self.assertEqual(next(e for e in events if e["t"] == "done")["changed"], [])
        self.assert_not_adopted("z")

    def test_fragmented_unknown_chat_stays_private_until_saved_as_a_candidate(self):
        self.initialize()
        job = self.turn([[base.say("Use $"), .4, base.say("z"), .2, base.say("=2$ in the proof.")]])
        during = self.events(job, wait=.05)
        self.assertEqual(self.visible(during["events"]), "")
        self.assertFalse(any(e["t"] == "notation_proposal" for e in during["events"]))
        events = self.finish(job)
        self.assertEqual(self.visible(events), "")
        self.assertEqual([e["proposal"]["canonical"] for e in events if e["t"] == "notation_proposal"], ["z"])
        self.assertEqual(len(self.requests()), 1)
        self.assert_not_adopted("z")

    def test_nested_text_math_is_held_before_source_application(self):
        self.initialize()
        original = (self.root / "main.tex").read_text()
        changed = original.replace(r"$\ell+\ell_+$", r"\[\mbox{$z$}\]")
        events = self.unchanged_turn([
            call_chunks("nested", "write_file", {"path": "main.tex", "content": changed}),
            [base.say("The nested mathematical name is saved.")]])
        self.assertEqual(self.visible(events), "")
        self.assertEqual([e["proposal"]["canonical"] for e in events
                          if e["t"] == "notation_proposal"], ["z"])
        self.assertEqual(len(self.requests()), 1)
        self.assert_not_adopted("z")

    def test_fragmented_nested_text_math_is_held_before_chat_display(self):
        self.initialize()
        for draft in (r"\[\text{$z$}\]", r"$\text{$z$}$", r"\[\mbox{words \ensuremath{z}}\]"):
            with self.subTest(draft=draft):
                split = draft.index("z")
                job = self.turn([[base.say(draft[:split]), .2, base.say(draft[split:])]])
                during = self.events(job, wait=.03)
                self.assertEqual(self.visible(during["events"]), "")
                events = self.finish(job)
                self.assertEqual(self.visible(events), "")
                self.assertEqual([e["proposal"]["canonical"] for e in events
                                  if e["t"] == "notation_proposal"], ["z"])
                self.assertEqual(self.state()["proposals"][0]["status"], "pending")
                self.assert_not_adopted("z")

    def test_latex_comments_in_supported_source_types_do_not_propose_notation(self):
        self.initialize()
        for suffix in (".tikz", ".bbx", ".cbx"):
            with self.subTest(suffix=suffix):
                name, text = "notes" + suffix, "% A comment containing $z$.\n"
                events = self.finish(self.turn([
                    call_chunks("comment", "write_file", {"path": name, "content": text}),
                    [base.say("The comment is saved.")]], mode="edit"))
                self.assertTrue((self.root / name).is_file())
                self.assertEqual((self.root / name).read_text(), text)
                self.assertFalse(any(e["t"] == "notation_proposal" for e in events))
                self.assert_not_adopted("z")

    def test_visible_nested_math_in_supported_source_types_still_requires_review(self):
        self.initialize()
        for suffix in (".tikz", ".bbx", ".cbx", ".md"):
            with self.subTest(suffix=suffix):
                name = "draft" + suffix
                text = r"\[\text{words $z$}\]"
                events = self.finish(self.turn([
                    call_chunks("nested", "write_file", {"path": name, "content": text})], mode="edit"))
                self.assertFalse((self.root / name).exists())
                self.assertEqual([e["proposal"]["canonical"] for e in events
                                  if e["t"] == "notation_proposal"], ["z"])
                self.assert_not_adopted("z")

    def test_latex_comment_naming_and_equations_do_not_request_adoption(self):
        self.initialize()
        text = "% z=x\n% Let z denote x.\n"
        events = self.finish(self.turn([
            call_chunks("comment", "write_file", {"path": "notes.tikz", "content": text}),
            [base.say("The comment is saved.")]], mode="edit"))
        self.assertTrue((self.root / "notes.tikz").is_file())
        self.assertEqual((self.root / "notes.tikz").read_text(), text)
        self.assertFalse(any(e["t"] == "notation_proposal" for e in events))
        self.assert_not_adopted("z")

    def test_formal_proposal_precedes_sibling_write_compile_and_raw_derivation(self):
        self.initialize()
        changed = (self.root / "main.tex").read_text() + "\nKnown $x+x$.\n"
        events = self.unchanged_turn([[call(
            ("write", "write_file", {"path": "main.tex", "content": changed}),
            ("proposal", "propose_notation", {**PROPOSAL, "approved": True}),
            ("compile", "compile", {}), text="Use $z$ and derive $z+z$ now.")],
            [base.say("This completion must not run.")]])
        proposal = next(e["proposal"] for e in events if e["t"] == "notation_proposal")
        self.assertEqual(proposal["status"], "pending")
        self.assertFalse(proposal["needs_details"])
        self.assertEqual(proposal["definition"], PROPOSAL["definition"])
        self.assertEqual(proposal["source_expression"], PROPOSAL["source_expression"])
        self.assertNotIn("approved", proposal)
        self.assertEqual(self.visible(events), "")
        self.assertFalse(any(e["t"] == "permission" for e in events))
        self.assertEqual([e["name"] for e in events if e["t"] == "tool"], ["Propose notation"])
        self.assertEqual(len(self.requests()), 1)
        self.assert_not_adopted("z")

    def test_failed_formal_proposal_has_no_success_event_or_sibling_application(self):
        self.initialize()
        record = self.root / "research/knowledge.json"
        before = record.read_bytes()
        (self.work / "fail-persistence").touch()
        events = self.unchanged_turn([[call(
            ("proposal", "propose_notation", PROPOSAL),
            ("write", "write_file", {"path": "main.tex", "content": "$x+x$"}))]])
        self.assertEqual(record.read_bytes(), before)
        self.assertFalse(any(e["t"] == "notation_proposal" for e in events))
        self.assertTrue(any(e["t"] == "error" and "saved" in e["message"] for e in events))
        self.assertEqual(next(e for e in events if e["t"] == "done")["outcome"], "failed")
        self.assertEqual(len(self.requests()), 1)

    def test_failed_raw_draft_registration_stops_without_display_or_source_write(self):
        self.initialize()
        (self.work / "fail-persistence").touch()
        events = self.unchanged_turn([[call(
            ("write", "write_file", {"path": "main.tex", "content": "$x+x$"}),
            text="Adopt $z$ now.")]])
        self.assertEqual(self.visible(events), "")
        self.assertFalse(any(e["t"] == "notation_proposal" for e in events))
        self.assertEqual(next(e for e in events if e["t"] == "done")["outcome"], "failed")
        self.assertEqual(len(self.requests()), 1)

    def test_pending_identity_survives_restart_and_repeated_request_is_idempotent(self):
        self.initialize()
        events = self.finish(self.turn([call_chunks("proposal", "propose_notation", PROPOSAL)]))
        first = next(e["proposal"] for e in events if e["t"] == "notation_proposal")
        before = (self.root / "research/knowledge.json").read_bytes()
        self.stop()
        self.start()
        self.assertEqual(self.state()["proposals"], [first])
        events = self.finish(self.turn([call_chunks("proposal-again", "propose_notation", PROPOSAL)]))
        self.assertEqual(next(e["proposal"] for e in events if e["t"] == "notation_proposal"), first)
        self.assertEqual((self.root / "research/knowledge.json").read_bytes(), before)
        self.assert_not_adopted("z")

    def test_resumed_api_session_loads_new_verified_evidence_before_known_usage(self):
        self.initialize()
        events = self.finish(self.turn([call_chunks("proposal", "propose_notation", PROPOSAL)]))
        session = next(e["session_id"] for e in events if e["t"] == "init")
        code, source = self.request("/api/file?path=main.tex")
        self.assertEqual(code, 200, source)
        text = source["content"] + "\nAuthor-supplied $z$.\n"
        code, result = self.request("/api/file", {"path": "main.tex", "content": text,
            "base_mtime": source["mtime"], "manual_edit_token": source["manual_edit"]["token"]})
        self.assertEqual(code, 200, result)
        self.assertNotIn("research_warning", result)
        text += "\nKnown $z+z+x$.\n"
        events = self.finish(self.turn([
            call_chunks("known", "write_file", {"path": "main.tex", "content": text}),
            [base.say("The known names can be used as $z+z+x$.")]], mode="edit", session=session))
        self.assertEqual((self.root / "main.tex").read_text(), text)
        self.assertIn("$z+z+x$", self.visible(events))
        self.assertFalse(any(e["t"] in ("permission", "notation_proposal") for e in events))
        contexts = [m["content"] for m in self.requests()[-1]["body"]["messages"]
                    if m["role"] == "system" and m["content"].startswith("[Prism research baseline]")]
        self.assertEqual(len(contexts), 1)
        latest, _ = json.JSONDecoder().raw_decode(contexts[0].split("\n", 1)[1])
        self.assertIn("z", {s["canonical"] for s in latest["symbols"]})
        for message in self.requests()[-1]["body"]["messages"]:
            if message.get("tool_calls"):
                replies = {m.get("tool_call_id") for m in self.requests()[-1]["body"]["messages"]}
                self.assertTrue({c["id"] for c in message["tool_calls"]} <= replies)

    def test_reference_tool_preview_withholds_unverified_math_without_requesting_adoption(self):
        (self.root / "reference.tex").write_text("Reference $q$.\n", encoding="utf-8")
        self.initialize()
        events = self.finish(self.turn([
            call_chunks("reference", "read_file", {"path": "reference.tex"}),
            [base.say("The known $x$ is available.")]]))
        previews = [e["preview"] for e in events if e["t"] == "tool_result"]
        self.assertTrue(previews)
        self.assertFalse(any("$q$" in preview for preview in previews))
        self.assertFalse(any(e["t"] == "notation_proposal" for e in events))
        self.assertEqual(self.state().get("proposals", []), [])
        self.assertIn("$x$", self.visible(events))

    def test_explicit_undelimited_naming_does_not_bypass_the_display_gate(self):
        self.initialize()
        events = self.finish(self.turn([[base.say("Let z denote the known $x+x$.")]]))
        self.assertEqual(self.visible(events), "")
        self.assertTrue(any(e["t"] == "notation_proposal" and e["proposal"]["canonical"] == "z" for e in events))
        self.assert_not_adopted("z")

    def test_stop_during_fragmented_draft_discards_unchecked_output(self):
        self.initialize()
        job = self.turn([[base.say("Use $z"), .8, base.say("=2$ now.")]])
        deadline = time.monotonic() + 2
        while not self.requests() and time.monotonic() < deadline:
            time.sleep(.01)
        code, result = self.request("/api/agent/stop", {"job": job, "project_key": self.key})
        self.assertEqual(code, 200, result)
        events = self.finish(job)
        self.assertEqual(self.visible(events), "")
        self.assertFalse(any(e["t"] == "notation_proposal" for e in events))
        self.assertEqual(next(e for e in events if e["t"] == "done")["outcome"], "stopped")
        self.assertEqual(self.state().get("proposals", []), [])

    def test_capability_reports_single_decisions_without_claiming_cli_support(self):
        self.initialize()
        code, info = self.request("/api/agent/info")
        self.assertEqual(code, 200, info)
        provider = info["providers"][0]
        self.assertEqual(provider["notation_guard"], {
            "application": True, "display": True, "proposals": True, "decisions": True})
        self.assertFalse(provider["notation_protection"])
