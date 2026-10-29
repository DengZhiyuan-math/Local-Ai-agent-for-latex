"""Ordinary author adoption of current, received unverified notation."""
import os
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "prism_local"), str(Path(__file__).parent)]
from test_research import project_with
from research import ResearchKnowledge
from tmpdirs import tmpdir
from errors import Conflict


class ReceivedConfirmation(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(tmpdir())}))
        self.project = project_with({"main.tex": "$x$"})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()

    def receive(self, text="$x+z$"):
        (self.project.root / "main.tex").write_text(text, encoding="utf-8")
        self.knowledge.refresh()

    def test_plain_yes_adopts_the_exact_received_name_without_inventing_a_definition(self):
        import research_confirmation as confirmation
        self.receive()
        before = (self.project.root / "main.tex").read_bytes()
        questions = confirmation.questions(self.knowledge)
        self.assertEqual([q["canonical"] for q in questions], ["z"])
        result = confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [questions[0]["id"]])
        self.assertEqual(len(result["decisions"]), 1)
        self.assertFalse(self.knowledge.classify("$z$")["unknown"])
        state = self.knowledge.status()
        z = next(s for s in state["symbols"] if s["canonical"] == "z")
        self.assertIn(z.get("meaning"), ("", None))
        self.assertEqual(z["status"], "unresolved")
        self.assertEqual((self.project.root / "main.tex").read_bytes(), before)
        self.assertEqual(state["proposals"], [])

    def test_stale_actual_source_is_refused_before_a_decision_is_saved(self):
        import research_confirmation as confirmation
        self.receive()
        question = confirmation.questions(self.knowledge)[0]
        (self.project.root / "main.tex").write_text("\n$x+z$", encoding="utf-8")
        with self.assertRaises(Conflict):
            confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [question["id"]])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        self.assertEqual(self.knowledge.status().get("decisions", []), [])

    def test_batch_plain_reply_is_ambiguous_and_named_parts_are_independent(self):
        import research_confirmation as confirmation
        self.receive("$x+z+w$")
        questions = confirmation.questions(self.knowledge)
        ids = [question["id"] for question in questions]
        ambiguous = confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", ids)
        self.assertEqual(ambiguous["decisions"], [])
        self.assertTrue(ambiguous["clarifications"])
        result = confirmation.decide_reply(self.knowledge, "Approve $z$", "fixture:chat", ids)
        self.assertEqual(len(result["decisions"]), 1)
        self.assertFalse(self.knowledge.classify("$z$")["unknown"])
        self.assertTrue(self.knowledge.classify("$w$")["unknown"])
        w = next(question for question in questions if question["canonical"] == "w")
        result = confirmation.decide_reply(self.knowledge, "No", "fixture:chat", [w["id"]])
        self.assertEqual(result["decisions"][0]["kind"], "refuse_name")
        self.assertTrue(self.knowledge.classify("$w$")["unknown"])
        self.assertEqual(confirmation.questions(self.knowledge), [])

    def test_canonical_failure_retries_and_success_is_idempotent_after_restart(self):
        import research_confirmation as confirmation
        self.receive()
        question = confirmation.questions(self.knowledge)[0]
        with mock.patch.object(self.knowledge, "_write", side_effect=OSError("controlled record failure")):
            with self.assertRaises(OSError):
                confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [question["id"]])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        self.assertEqual(confirmation.questions(self.knowledge)[0]["id"], question["id"])
        confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [question["id"]])
        restored = ResearchKnowledge(self.project)
        repeated = confirmation.decide_reply(restored, "Yes", "fixture:chat", [question["id"]])
        self.assertEqual(repeated["decisions"], [])
        self.assertEqual(len(restored.status()["decisions"]), 1)

    def test_received_drafts_end_with_the_question_without_creating_an_ai_proposal(self):
        from research_access import NotationStop, ResearchAccess
        import research_confirmation as confirmation
        self.receive()
        source = (self.project.root / "main.tex").read_bytes()
        access = ResearchAccess(self.knowledge)
        with self.assertRaises(NotationStop) as stopped:
            access.review_output("$z$")
        self.assertIn("Received notation", stopped.exception.error)
        self.assertEqual(self.knowledge.status()["proposals"], [])
        self.assertEqual(len(confirmation.questions(self.knowledge)), 1)
        self.assertEqual((self.project.root / "main.tex").read_bytes(), source)
        with self.assertRaises(NotationStop):
            access.review_output("$z+w$")
        self.assertEqual([proposal["canonical"] for proposal in self.knowledge.status()["proposals"]], ["w"])

    def test_bare_reply_cannot_choose_between_an_ai_proposal_and_received_question(self):
        from research_decisions import set_subject
        import research_confirmation as confirmation
        self.receive()
        question = confirmation.questions(self.knowledge)[0]
        proposal = self.knowledge.propose("w", "an expression", "project", "a reason")
        set_subject(question["project_key"], "fixture:chat", [proposal])
        result = confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [question["id"]])
        self.assertEqual(result["decisions"], [])
        self.assertTrue(result["clarifications"])
        self.assertTrue(self.knowledge.classify("$z+w$")["unknown"])
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")

    def test_unrelated_topic_invalidates_an_old_plain_reply_but_named_confirmation_is_clear(self):
        import research_confirmation as confirmation
        self.receive()
        question = confirmation.questions(self.knowledge)[0]
        self.assertIsNone(confirmation.decide_reply(self.knowledge, "Discuss the introduction.", "fixture:chat", [question["id"]]))
        ambiguous = confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [question["id"]])
        self.assertEqual(ambiguous["decisions"], [])
        self.assertTrue(ambiguous["clarifications"])
        explicit = confirmation.decide_reply(self.knowledge, "Approve $z$", "fixture:chat", [question["id"]])
        self.assertEqual(len(explicit["decisions"]), 1)

    def test_unrelated_direct_instruction_still_uses_the_existing_chat_path(self):
        import research_confirmation as confirmation
        self.receive()
        question = confirmation.questions(self.knowledge)[0]
        text = "Use $w$ for $x+x$"
        self.assertIsNone(confirmation.decide_reply(self.knowledge, text, "fixture:chat", [question["id"]]))
        self.knowledge.decide_reply(text, "fixture:chat")
        self.assertFalse(self.knowledge.classify("$w$")["unknown"])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])

    def test_private_question_ids_and_actual_project_root_are_required(self):
        import research_confirmation as confirmation
        self.receive()
        with self.assertRaises(Conflict):
            confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", ["f" * 32])
        question = confirmation.questions(self.knowledge)[0]
        other = project_with({"main.tex": "$x+z$"})
        other_knowledge = ResearchKnowledge(other)
        other_knowledge.initialize()
        with self.assertRaises(Conflict):
            confirmation.decide_reply(other_knowledge, "Yes", "fixture:chat", [question["id"]])

    def test_source_race_just_before_persistence_keeps_the_name_unverified(self):
        import research_confirmation as confirmation
        self.receive()
        question = confirmation.questions(self.knowledge)[0]
        original = confirmation.make_instruction
        def mutate_source(*args, **kwargs):
            receipt = original(*args, **kwargs)
            (self.project.root / "main.tex").write_text("$x+z+z$", encoding="utf-8")
            return receipt
        with mock.patch.object(confirmation, "make_instruction", side_effect=mutate_source):
            with self.assertRaises(Conflict):
                confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [question["id"]])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        self.assertEqual(self.knowledge.status().get("decisions", []), [])

    def test_ui_ambiguity_with_no_captured_received_ids_cannot_fall_through(self):
        import research_confirmation as confirmation
        self.receive()
        result = confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [], context_ambiguous=True)
        self.assertEqual(result["decisions"], [])
        self.assertTrue(result["clarifications"])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        self.assertIsNone(confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", []))

    def test_existing_expression_refusal_is_not_overridden_by_plain_confirmation(self):
        import research_confirmation as confirmation
        self.knowledge.decide_reply("Do not abbreviate $x+x$", "fixture:earlier")
        self.receive("Let $z$ be $x+x$.")
        question = confirmation.questions(self.knowledge)[0]
        before = len(self.knowledge.status()["decisions"])
        result = confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [question["id"]])
        self.assertEqual(result["decisions"], [])
        self.assertTrue(result["clarifications"])
        self.assertEqual(len(self.knowledge.status()["decisions"]), before)
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])

    def test_current_family_permission_is_reused_but_does_not_cover_another_index(self):
        import research_confirmation as confirmation
        self.knowledge.decide_reply("Use $u_i$ for $x+x$ with i in {1,2}", "fixture:earlier")
        self.receive("$x+u_1+u_3$")
        questions = confirmation.questions(self.knowledge)
        self.assertEqual([question["canonical"] for question in questions], ["u_3"])
        self.assertFalse(self.knowledge.classify("$u_1$")["unknown"])
        self.assertTrue(self.knowledge.classify("$u_3$")["unknown"])

    def test_exact_bytes_and_unchanged_questions_do_not_reissue_or_rewrite(self):
        import research_confirmation as confirmation
        raw = b"$x+z$\r\n"
        (self.project.root / "main.tex").write_bytes(raw)
        self.knowledge.refresh()
        first = confirmation.questions(self.knowledge)
        self.assertEqual(first[0]["sources"][0]["sha256"], hashlib.sha256(raw).hexdigest())
        path = confirmation._path(self.knowledge)
        before, mtime = path.read_bytes(), path.stat().st_mtime_ns
        with mock.patch.object(confirmation, "sign_receipt", side_effect=AssertionError("must not reissue")), \
                mock.patch.object(confirmation, "replace_file", side_effect=AssertionError("must not rewrite")):
            second = confirmation.questions(self.knowledge)
        self.assertEqual(first, second)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "regular-file boundary needs FIFO support")
    def test_private_question_pipe_is_refused_without_waiting_for_a_writer(self):
        import research_confirmation as confirmation
        self.receive()
        path = confirmation._path(self.knowledge)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            confirmation.questions(self.knowledge)

    def test_received_ordinals_ask_for_the_actual_name_without_approving_either_context(self):
        import research_confirmation as confirmation
        self.receive("$x+z+w$")
        ids = [question["id"] for question in confirmation.questions(self.knowledge)]
        result = confirmation.decide_reply(self.knowledge, "Approve item 1", "fixture:chat", list(reversed(ids)))
        self.assertEqual(result["decisions"], [])
        self.assertTrue(result["clarifications"])
        self.assertTrue(self.knowledge.classify("$z+w$")["unknown"])
        missing = confirmation.decide_reply(self.knowledge, "Approve item 1", "fixture:chat", [], context_ambiguous=True)
        self.assertEqual(missing["decisions"], [])
        self.assertTrue(missing["clarifications"])

    def test_a_named_current_question_resolves_ui_ambiguity(self):
        import research_confirmation as confirmation
        self.receive()
        question = confirmation.questions(self.knowledge)[0]
        result = confirmation.decide_reply(self.knowledge, "Approve $z$", "fixture:chat", [question["id"]], context_ambiguous=True)
        self.assertEqual(len(result["decisions"]), 1)
        self.assertFalse(self.knowledge.classify("$z$")["unknown"])

    def test_confirmation_records_adoption_and_never_claims_a_verified_manual_edit(self):
        import research_confirmation as confirmation
        self.receive()
        question = confirmation.questions(self.knowledge)[0]
        result = confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [question["id"]])
        receipt = result["decisions"][0]
        self.assertEqual(receipt["purpose"], "notation_instruction")
        self.assertEqual(receipt["kind"], "adopt")
        self.assertEqual(receipt["human_text"], "Yes")
        self.assertEqual(receipt["received_confirmation"]["question_id"], question["id"])
        self.assertEqual(receipt["received_confirmation"]["sources"], question["sources"])
        z = next(item for item in self.knowledge.status()["symbols"] if item["canonical"] == "z")
        self.assertFalse(any(source.get("evidence") == "human_edit" for source in z["sources"]))

    def test_explicit_model_proposal_cannot_invent_details_for_a_received_name(self):
        from research_access import NotationStop, ResearchAccess
        self.receive()
        access = ResearchAccess(self.knowledge)
        received = {"notation": "z", "definition": "invented model meaning", "scope": "project", "reason": "a model guess"}
        with self.assertRaises(NotationStop) as stopped:
            access.propose(received)
        self.assertIn("Received notation", stopped.exception.error)
        self.assertEqual(self.knowledge.status()["proposals"], [])
        new = {**received, "notation": "w", "definition": "the intended new expression"}
        proposals = access.propose_many([received, new])
        self.assertEqual([proposal["canonical"] for proposal in proposals], ["w"])

    def test_source_line_numbers_use_editor_newlines_while_hashing_original_bytes(self):
        import research_confirmation as confirmation
        raw = b"$x$\r$z$\r"
        (self.project.root / "main.tex").write_bytes(raw)
        question = confirmation.questions(self.knowledge)[0]
        self.assertEqual(question["sources"][0]["line"], 2)
        self.assertEqual(question["sources"][0]["sha256"], hashlib.sha256(raw).hexdigest())

    def test_explicit_received_reopen_requires_a_fresh_question_and_new_approval(self):
        import research_confirmation as confirmation
        self.receive()
        before = (self.project.root / "main.tex").read_bytes()
        old = confirmation.questions(self.knowledge)[0]
        confirmation.decide_reply(self.knowledge, "No", "fixture:chat", [old["id"]])
        self.assertEqual(confirmation.questions(self.knowledge), [])
        reopened = confirmation.decide_reply(self.knowledge, "Reopen $z$", "fixture:chat", [])
        self.assertEqual(reopened["decisions"][0]["kind"], "reopen")
        self.assertNotIn("proposal_id", reopened["decisions"][0])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        fresh = confirmation.questions(self.knowledge)[0]
        self.assertNotEqual(old["id"], fresh["id"])
        replay = confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [old["id"]])
        self.assertEqual(replay["decisions"], [])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [fresh["id"]])
        restored = ResearchKnowledge(self.project)
        self.assertFalse(restored.classify("$z$")["unknown"])
        self.assertEqual([receipt["kind"] for receipt in restored.status()["decisions"]], ["refuse_name", "reopen", "adopt"])
        self.assertEqual(restored.status()["proposals"], [])
        self.assertEqual((self.project.root / "main.tex").read_bytes(), before)

    def test_received_reopen_uses_current_source_versions_after_the_refusal(self):
        import research_confirmation as confirmation
        self.receive()
        old = confirmation.questions(self.knowledge)[0]
        confirmation.decide_reply(self.knowledge, "No", "fixture:chat", [old["id"]])
        self.receive("$x+z+z$")
        raw = (self.project.root / "main.tex").read_bytes()
        reopened = confirmation.decide_reply(self.knowledge, "Reopen $z$", "fixture:chat", [])
        self.assertEqual(reopened["decisions"][0]["received_confirmation"]["sources"][0]["sha256"], hashlib.sha256(raw).hexdigest())
        fresh = confirmation.questions(self.knowledge)[0]
        self.assertNotEqual(fresh["id"], old["id"])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        confirmation.decide_reply(self.knowledge, "Approve $z$", "fixture:chat", [fresh["id"]])
        self.assertFalse(self.knowledge.classify("$z$")["unknown"])
        self.assertEqual((self.project.root / "main.tex").read_bytes(), raw)

    def test_failed_reopen_persistence_retains_the_refusal_and_can_retry_once(self):
        import research_confirmation as confirmation
        self.receive()
        question = confirmation.questions(self.knowledge)[0]
        confirmation.decide_reply(self.knowledge, "No", "fixture:chat", [question["id"]])
        with mock.patch.object(self.knowledge, "_write", side_effect=OSError("controlled reopen registration failure")):
            with self.assertRaises(OSError):
                confirmation.decide_reply(self.knowledge, "Reopen $z$", "fixture:chat", [])
        self.assertEqual(confirmation.questions(self.knowledge), [])
        self.assertEqual([receipt["kind"] for receipt in self.knowledge.status()["decisions"]], ["refuse_name"])
        confirmation.decide_reply(self.knowledge, "Reopen $z$", "fixture:chat", [])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        self.assertEqual([receipt["kind"] for receipt in self.knowledge.status()["decisions"]], ["refuse_name", "reopen"])
        repeated = confirmation.decide_reply(self.knowledge, "Reopen $z$", "fixture:chat", [])
        self.assertEqual(repeated["decisions"], [])
        self.assertEqual(len(confirmation.questions(self.knowledge)), 1)

    def receive_library(self):
        row = {"notation": "z", "canonical": "z", "status": "approved", "sources": []}
        path = self.knowledge._path()
        data = json.loads(path.read_bytes())
        data["symbols"].append(row)
        raw = (json.dumps(data, indent=2) + "\n").encode()
        path.write_bytes(raw)
        self.knowledge.refresh()
        return raw

    def demand_library(self):
        from research_access import NotationStop, ResearchAccess
        import research_confirmation as confirmation
        with self.assertRaises(NotationStop) as stopped:
            ResearchAccess(self.knowledge).review_output("Use $z$.")
        self.assertIn("Received notation", stopped.exception.error)
        self.assertEqual(self.knowledge.status()["proposals"], [])
        return confirmation.questions(self.knowledge)[0]

    def test_library_only_reference_is_asked_on_reuse_and_never_trusted_from_approved_status(self):
        import research_confirmation as confirmation
        raw = self.receive_library()
        self.assertEqual(confirmation.questions(self.knowledge), [])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        question = self.demand_library()
        source = question["sources"][0]
        self.assertEqual(source["kind"], "received_library")
        self.assertEqual(source["path"], "research/knowledge.json")
        self.assertEqual(source["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertRegex(source["json_pointer"], r"^/symbols/\d+$")
        self.assertNotIn("line", source)
        self.assertEqual(self.knowledge._path().read_bytes(), raw)
        confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [question["id"]])
        self.assertFalse(self.knowledge.classify("$z$")["unknown"])
        self.assertEqual((self.project.root / "main.tex").read_bytes(), b"$x$")

    def test_changed_received_library_entry_invalidates_the_displayed_question(self):
        import research_confirmation as confirmation
        self.receive_library()
        question = self.demand_library()
        data = json.loads(self.knowledge._path().read_bytes())
        data["symbols"][-1]["meaning"] = "a changed unverified claim"
        raw = json.dumps(data).encode()
        self.knowledge._path().write_bytes(raw)
        with self.assertRaises(Conflict):
            confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [question["id"]])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_library_refusal_and_reopen_keep_the_exact_received_archive(self):
        import research_confirmation as confirmation
        raw = self.receive_library()
        question = self.demand_library()
        confirmation.decide_reply(self.knowledge, "No", "fixture:chat", [question["id"]])
        self.assertEqual(confirmation.questions(self.knowledge), [])
        confirmation.decide_reply(self.knowledge, "Reopen $z$", "fixture:chat", [])
        fresh = confirmation.questions(self.knowledge)[0]
        self.assertNotEqual(fresh["id"], question["id"])
        self.assertEqual(fresh["sources"][0]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [fresh["id"]])
        self.assertFalse(self.knowledge.classify("$z$")["unknown"])
        self.assertEqual(self.knowledge.status()["proposals"], [])

    def test_another_local_policy_projection_does_not_erase_the_pending_library_reference(self):
        import research_confirmation as confirmation
        raw = self.receive_library()
        question = self.demand_library()
        self.knowledge.decide_reply("Use $w$ for $x+x$", "fixture:other")
        self.assertNotEqual(self.knowledge._path().read_bytes(), raw)
        current = confirmation.questions(self.knowledge)[0]
        self.assertEqual(current["id"], question["id"])
        self.assertEqual(current["sources"][0]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        confirmation.decide_reply(self.knowledge, "Yes", "fixture:chat", [current["id"]])
        self.assertFalse(self.knowledge.classify("$z$")["unknown"])
