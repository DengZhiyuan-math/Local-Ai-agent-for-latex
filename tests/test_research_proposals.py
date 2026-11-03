"""Durable notation requests; these tests do not grant adoption permission."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from errors import Conflict, Refused
from project import Project
from registry import project_key
from research import RECORD, ResearchKnowledge
from test_research import project_with


class NotationProposals(unittest.TestCase):
    def setUp(self):
        self.source = "$x + \\ell$"
        self.project = project_with({"main.tex": self.source})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()
        self.project.on_change = mock.Mock()

    def propose(self, notation="z", **changes):
        request = {"notation": notation, "definition": "The error term.",
                   "scope": "The current proof.", "reason": "Keep the error estimate readable."}
        request.update(changes)
        return self.knowledge.propose(**request)

    def record_bytes(self):
        return (self.project.root / RECORD).read_bytes()

    def saved(self):
        return json.loads(self.record_bytes())

    def test_complete_unknown_request_is_saved_before_return_and_not_known(self):
        result = self.propose(source_expression="$z=x+1$", origin={"channel": "api", "job": "one"})
        self.assertEqual(result, self.saved()["proposals"][0])
        self.assertEqual(result["project_key"], project_key(self.project.root))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["revision"], 1)
        self.assertFalse(result["needs_details"])
        self.assertEqual(result["candidate_kind"], "identifier")
        self.assertEqual(result["canonical"], "z")
        self.assertEqual(result["source_expression"], "$z=x+1$")
        self.assertEqual(result["origin"], {"actor": "agent", "channel": "api", "job": "one"})
        classified = self.knowledge.classify("$x+z$")
        self.assertEqual([s["canonical"] for s in classified["known"]], ["x"])
        self.assertEqual([s["canonical"] for s in classified["unknown"]], ["z"])
        self.assertEqual((self.project.root / "main.tex").read_text(), self.source)
        self.project.on_change.assert_called_once_with()

    def test_known_symbols_operators_layout_and_expressions_do_not_make_requests(self):
        before = self.record_bytes()
        for notation in ("x", r"\ell", "ℓ", r"\sum", r"\sin", r"\quad", r"\frac{x}{y}",
                         r"\InputIfFileExists", r"\RequirePackage", r"\LoadClass", r"\newenvironment", r"\char",
                         "x+z", "z^2", "z_{i+j}", r"\input", r"\newcommand"):
            with self.subTest(notation=notation), self.assertRaises(Refused):
                self.propose(notation)
        self.assertEqual(self.record_bytes(), before)
        self.project.on_change.assert_not_called()

    def test_complete_decorations_and_explicit_unknown_identifier_macro_are_allowed(self):
        first = self.propose(r"$z_{i}$")
        self.assertEqual(first["canonical"], "z_i")
        custom = self.propose(r"\fresh")
        self.assertEqual(custom["canonical"], r"\fresh")
        self.assertEqual(len(self.saved()["proposals"]), 2)
        self.assertEqual(self.knowledge.classify(r"$\fresh$")["known"], [])

    def test_equivalent_retries_are_idempotent_even_with_a_new_job(self):
        first = self.propose("z_i", origin={"channel": "api", "job": "first"})
        before = self.record_bytes()
        self.project.on_change.reset_mock()
        with mock.patch("research.replace_file", side_effect=AssertionError("idempotent request wrote")):
            second = self.propose("z_{i}", origin={"channel": "api", "job": "second"})
        self.assertEqual(second, first)
        self.assertEqual(self.record_bytes(), before)
        self.project.on_change.assert_not_called()

    def test_changed_contents_keep_identity_and_version_the_old_request(self):
        first = self.propose()
        second = self.propose(definition="The accumulated error term.")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["revision"], 2)
        self.assertNotEqual(second["content_hash"], first["content_hash"])
        self.assertEqual(second["history"], [{k: v for k, v in first.items() if k != "history"}])
        self.assertEqual(len(self.saved()["proposals"]), 1)

    def test_raw_hold_records_missing_details_without_inventing_a_definition(self):
        result = self.knowledge.hold_notation([{"notation": "z", "line": 5}], channel="write",
                                              path="main.tex", source_expression="$x+z$",
                                              details="This draft has no verified adoption source.")
        self.assertEqual(len(result), 1)
        held = result[0]
        self.assertTrue(held["needs_details"])
        self.assertEqual(held["status"], "pending")
        self.assertIn("clarification", held["definition"])
        self.assertEqual(held["scope"], "Manuscript file main.tex")
        self.assertIn("no verified adoption source", held["reason"])
        self.assertEqual(held["source_expression"], "$x+z$")
        self.assertEqual(held["origin"], {"actor": "agent", "channel": "write", "path": "main.tex"})
        self.assertEqual(self.saved()["proposals"], result)
        self.assertEqual((self.project.root / "main.tex").read_text(), self.source)

    def test_formal_request_fills_held_candidate_then_later_holds_cannot_erase_it(self):
        held = self.knowledge.hold_notation([{"notation": "z"}], channel="chat")[0]
        complete = self.propose()
        self.assertEqual(complete["id"], held["id"])
        self.assertEqual(complete["revision"], 2)
        self.assertFalse(complete["needs_details"])
        self.assertTrue(complete["history"][0]["needs_details"])
        before = self.record_bytes()
        again = self.knowledge.hold_notation([{"notation": "z"}], channel="write", path="main.tex")
        self.assertEqual(again, [complete])
        self.assertEqual(self.record_bytes(), before)

    def test_hold_handles_expressions_conservatively_and_deduplicates_occurrences(self):
        result = self.knowledge.hold_notation([{"notation": "x+z"}, {"notation": "z"},
                                             {"notation": "x"}, {"notation": r"\sum"}], channel="chat")
        self.assertEqual([p["canonical"] for p in result], ["z"])
        self.assertTrue(result[0]["needs_details"])
        self.assertEqual(self.knowledge.hold_notation([{"notation": "x+x"}], channel="chat"), [])

    def test_unresolved_fragment_is_labeled_and_cannot_be_a_formal_identifier(self):
        result = self.knowledge.hold_notation([{"notation": "z_{i+j}"}], channel="chat")
        fragment = next(p for p in result if p["canonical"] == "z_{i+j}")
        self.assertEqual(fragment["candidate_kind"], "unresolved_fragment")
        self.assertTrue(fragment["needs_details"])
        with self.assertRaises(Refused):
            self.propose("z_{i+j}")
        self.assertEqual(self.knowledge.classify("$z_{i+j}$")["known"], [])

    def test_different_unresolved_sources_keep_history_without_granting_permission(self):
        first = self.knowledge.hold_notation([{"notation": "z"}], channel="chat", source_expression="$z=1$")[0]
        later = self.knowledge.hold_notation([{"notation": "z"}], channel="write", path="main.tex",
                                             source_expression="$z=2$")[0]
        self.assertEqual(later["id"], first["id"])
        self.assertEqual(later["revision"], 2)
        self.assertEqual(later["history"][0]["source_expression"], "$z=1$")
        self.assertEqual(self.knowledge.classify("$z$")["known"], [])

    def test_origin_cannot_claim_human_authority_or_approval(self):
        proposal = self.propose(origin={"actor": "human", "status": "approved", "approved": True,
                                        "channel": "api", "path": "main.tex"})
        self.assertEqual(proposal["origin"], {"actor": "agent", "channel": "api", "path": "main.tex"})
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(self.knowledge.classify("$z$")["known"], [])

    def test_active_turn_can_persist_internal_record_and_receives_server_write(self):
        wrote = mock.Mock()
        self.project.turn = SimpleNamespace(busy=lambda: True, editing=lambda _path: False, server_wrote=wrote)
        proposal = self.propose()
        self.assertEqual(proposal["status"], "pending")
        wrote.assert_called_once_with([RECORD])
        self.assertEqual((self.project.root / "main.tex").read_text(), self.source)

    def test_restart_restores_pending_requests_and_does_not_reinitialize(self):
        first = self.propose()
        raw = self.record_bytes()
        reopened = ResearchKnowledge(Project(self.project.root))
        self.assertEqual(reopened.status()["proposals"], [first])
        self.assertEqual(reopened.initialize()["proposals"], [first])
        self.assertEqual(self.record_bytes(), raw)

    def test_legacy_record_without_optional_field_is_reported_without_rewrite(self):
        record = self.saved()
        del record["proposals"]
        (self.project.root / RECORD).write_text(json.dumps(record))
        raw = self.record_bytes()
        self.assertEqual(self.knowledge.status()["proposals"], [])
        self.assertEqual(self.record_bytes(), raw)
        self.propose()
        self.assertEqual(len(self.saved()["proposals"]), 1)

    def test_failed_save_preserves_previous_record_and_source_without_success_notice(self):
        held = self.knowledge.hold_notation([{"notation": "z"}], channel="chat")[0]
        raw = self.record_bytes()
        self.project.on_change.reset_mock()
        with mock.patch("research.replace_file", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.propose()
        self.assertEqual(self.record_bytes(), raw)
        self.assertEqual(self.knowledge.status()["proposals"], [held])
        self.assertEqual((self.project.root / "main.tex").read_text(), self.source)
        self.project.on_change.assert_not_called()

    def test_hold_batch_is_atomic_when_saving_fails(self):
        raw = self.record_bytes()
        with mock.patch("research.replace_file", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.knowledge.hold_notation([{"notation": "z"}, {"notation": "w"}], channel="chat")
        self.assertEqual(self.record_bytes(), raw)
        self.project.on_change.assert_not_called()

    def test_unavailable_lock_does_not_attempt_storage(self):
        self.project.files_lock = SimpleNamespace(acquire=lambda **kw: False, release=mock.Mock())
        raw = self.record_bytes()
        with self.assertRaises(Conflict):
            self.propose()
        with self.assertRaises(Conflict):
            self.knowledge.hold_notation([{"notation": "z"}], channel="chat")
        self.assertEqual(self.record_bytes(), raw)
        self.project.files_lock.release.assert_not_called()

    def test_invalid_request_fields_do_not_create_a_record(self):
        raw = self.record_bytes()
        for change in ({"definition": " "}, {"scope": None}, {"reason": ""},
                       {"source_expression": []}, {"notation": ""}, {"notation": 1},
                       {"origin": "human"}, {"origin": {"path": "../outside.tex"}}):
            with self.subTest(change=change), self.assertRaises(Refused):
                self.propose(**change)
        self.assertEqual(self.record_bytes(), raw)
        self.project.on_change.assert_not_called()

    def test_uninitialized_project_is_not_silently_initialized(self):
        project = project_with({"main.tex": "$x$"})
        knowledge = ResearchKnowledge(project)
        with self.assertRaises(Refused):
            knowledge.propose("z", "An error.", "The proof.", "Readability.")
        with self.assertRaises(Refused):
            knowledge.hold_notation([{"notation": "z"}], channel="chat")
        self.assertFalse((project.root / RECORD).exists())

    def test_copied_proposal_identity_and_edited_status_fail_closed(self):
        self.propose()
        record = self.saved()
        for change in ({"project_key": "other-project"}, {"status": "approved"},
                       {"definition": "Edited without a new revision."}):
            altered = copy.deepcopy(record)
            altered["proposals"][0].update(change)
            (self.project.root / RECORD).write_text(json.dumps(altered))
            raw = self.record_bytes()
            with self.subTest(change=change):
                self.assertEqual(self.knowledge.classify("$z$")["known"], [])
                self.assertEqual(self.knowledge.status()["proposals"], record["proposals"])
                self.assertEqual(self.record_bytes(), raw)

    def test_a_record_copied_to_another_project_remains_an_unverified_reference(self):
        self.propose()
        other = project_with({"main.tex": self.source})
        (other.root / "research").mkdir()
        (other.root / RECORD).write_bytes(self.record_bytes())
        raw = (other.root / RECORD).read_bytes()
        knowledge = ResearchKnowledge(other)
        state = knowledge.status()
        self.assertFalse(state["initialized"])
        self.assertEqual(state["provenance"]["record_state"], "foreign")
        self.assertEqual(state["proposals"], [])
        self.assertEqual(knowledge.classify("$z$")["known"], [])
        self.assertEqual((other.root / RECORD).read_bytes(), raw)
        self.assertEqual((other.root / "main.tex").read_text(), self.source)


if __name__ == "__main__":
    unittest.main()
