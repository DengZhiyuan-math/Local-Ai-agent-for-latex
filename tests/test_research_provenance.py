"""A received project mirror cannot rewrite locally established notation authority."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "prism_local"), str(Path(__file__).parent)]
from errors import Refused
from research import ResearchKnowledge
from test_research import project_with
from tmpdirs import tmpdir


class ResearchProvenance(unittest.TestCase):
    def setUp(self):
        self.state = tmpdir()
        self.enterContext(mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state)}))
        self.project = project_with({"main.tex": "$x$"})
        self.knowledge = ResearchKnowledge(self.project)

    def received(self, data):
        path = self.knowledge._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()
        path.write_bytes(raw)
        return raw

    def test_rehashed_received_baseline_and_explicit_notation_do_not_grant_permission(self):
        self.knowledge.initialize()
        data = json.loads(self.knowledge._path().read_text())
        data["sources"][0]["text"] = "$x+z$"
        data["sources"][0]["sha256"] = hashlib.sha256(b"$x+z$").hexdigest()
        data["explicit_notation"] = "q"
        for name in ("z", "q"):
            data["symbols"].append({"notation": name, "canonical": name, "status": "documented", "sources": []})
        raw = self.received(data)
        restored = ResearchKnowledge(self.project)
        self.assertEqual([item["canonical"] for item in restored.classify("$x+z+q$")["known"]], ["x"])
        state = restored.refresh()
        self.assertTrue(state["provenance"]["baseline_verified"])
        self.assertEqual(state["provenance"]["record_state"], "received")
        self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_post_replace_failure_retains_exact_projection_recovery(self):
        import research
        original = research.replace_file
        def durable_then_fail(path, raw, **options):
            original(path, raw, **options)
            raise OSError("fixture lost durable-write acknowledgement")
        with mock.patch("research.replace_file", side_effect=durable_then_fail), self.assertRaises(OSError):
            self.knowledge.initialize()
        raw = self.knowledge._path().read_bytes()
        restored = ResearchKnowledge(self.project)
        self.assertTrue(restored.status()["provenance"]["baseline_verified"])
        self.assertEqual([item["canonical"] for item in restored.classify("$x$")["known"]], ["x"])
        self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_received_rollback_and_missing_record_retain_local_refusal(self):
        self.knowledge.initialize()
        earlier = json.loads(self.knowledge._path().read_text())
        proposal = self.knowledge.propose("z", "The sum of two lengths.", "The estimate.", "A shorter name.", "x+x")
        from research_decisions import set_subject
        from registry import project_key
        set_subject(project_key(self.project.root), "fixture:author", [proposal])
        self.knowledge.decide_reply("Reject item 1 and keep the original expression expanded.", "fixture:author")
        raw = self.received(earlier)
        restored = ResearchKnowledge(self.project)
        self.assertTrue(restored.expression_rejected("x+x"))
        self.assertEqual(restored.status()["proposals"][0]["status"], "rejected")
        restored.refresh()
        self.assertEqual(self.knowledge._path().read_bytes(), raw)
        self.knowledge._path().unlink()
        state = ResearchKnowledge(self.project).status()
        self.assertTrue(state["initialized"])
        self.assertEqual(state["provenance"]["record_state"], "missing")
        self.assertEqual(state["discussion"]["mode"], "markdown")
        self.assertEqual(state["proposals"][0]["status"], "rejected")
        self.assertFalse(self.knowledge._path().exists())

    def test_foreign_snapshot_is_reference_until_explicit_local_initialization(self):
        other = ResearchKnowledge(project_with({"main.tex": "$y$"}))
        other.initialize()
        received = json.loads(other._path().read_text())
        raw = self.received(received)
        state = self.knowledge.status()
        self.assertFalse(state["initialized"])
        self.assertFalse(state["provenance"]["baseline_verified"])
        self.assertEqual(state["provenance"]["record_state"], "foreign")
        self.assertEqual(self.knowledge.classify("$x+y$")["known"], [])
        self.assertEqual(self.knowledge._path().read_bytes(), raw)
        initialized = self.knowledge.initialize()
        self.assertTrue(initialized["provenance"]["baseline_verified"])
        self.assertEqual([item["canonical"] for item in self.knowledge.classify("$x+y$")["known"]], ["x"])
        copies = list((self.state / "research-provenance").rglob("*.record"))
        self.assertIn(raw, [path.read_bytes() for path in copies])

    def test_legacy_current_root_receipt_survives_explicit_initialization(self):
        from registry import project_key
        from research_decisions import make_decision
        self.knowledge.initialize()
        proposal = self.knowledge.propose("z", "The sum of two lengths.", "The estimate.", "A shorter name.", "x+x")
        receipt = make_decision(project_key(self.project.root), proposal, "rejected", "No.", "fixture:author")
        raw = json.loads(self.knowledge._path().read_text())
        raw["decisions"] = [receipt]
        raw["proposals"][0]["status"] = "rejected"
        self.received(raw)
        from research_provenance import Provenance
        from research import RECORD, MAX_RECORD
        store = Provenance(self.project.root, RECORD, MAX_RECORD)
        store.path.unlink()
        self.assertFalse(self.knowledge.status()["initialized"])
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "rejected")
        state = self.knowledge.initialize()
        self.assertTrue(state["provenance"]["baseline_verified"])
        self.assertEqual(state["proposals"][0]["status"], "rejected")
        self.assertEqual([item["id"] for item in state["decisions"]], [receipt["id"]])

    def test_prepare_or_public_failure_does_not_create_initialization_authority(self):
        import research_provenance
        original = research_provenance.replace_file
        def fail_prepare(path, raw, **options):
            if Path(path).name == "state.json" and json.loads(raw).get("pending"):
                raise OSError("fixture private prepare failed")
            return original(path, raw, **options)
        failures = [mock.patch("research_provenance.replace_file", side_effect=fail_prepare),
                    mock.patch("research.replace_file", side_effect=OSError("fixture public replace failed"))]
        for failure in failures:
            with failure, self.assertRaises(OSError):
                self.knowledge.initialize()
            self.assertFalse(self.knowledge._path().exists())
            self.assertEqual(self.knowledge.status(), {"initialized": False})
            self.assertEqual(self.knowledge.classify("$x$")["known"], [])

    def test_commit_failure_is_closed_until_exact_projection_can_be_recovered(self):
        import research_provenance
        original = research_provenance.replace_file
        def fail_commit(path, raw, **options):
            if Path(path).name == "state.json":
                value = json.loads(raw)
                if value.get("pending") is None and value.get("committed"):
                    raise OSError("fixture private commit failed")
            return original(path, raw, **options)
        with mock.patch("research_provenance.replace_file", side_effect=fail_commit):
            with self.assertRaises(OSError):
                self.knowledge.initialize()
            raw = self.knowledge._path().read_bytes()
            with self.assertRaises(OSError):
                self.knowledge.classify("$x$")
        restored = ResearchKnowledge(self.project).status()
        self.assertTrue(restored["provenance"]["baseline_verified"])
        self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_unreadable_post_write_outcome_retains_recovery_intent(self):
        import research
        import research_provenance
        original_write, original_read = research.replace_file, research_provenance.read_plain
        fail_reads = [False]
        def unreadable(path, limit):
            if Path(path) == self.knowledge._path() and fail_reads[0]:
                raise OSError("fixture public outcome unreadable")
            return original_read(path, limit)
        def durable_then_fail(path, raw, **options):
            original_write(path, raw, **options)
            fail_reads[0] = True
            raise OSError("fixture outcome unknown")
        with mock.patch("research.replace_file", side_effect=durable_then_fail), \
                mock.patch("research_provenance.read_plain", side_effect=unreadable), self.assertRaises(OSError):
            self.knowledge.initialize()
        self.assertTrue(ResearchKnowledge(self.project).status()["provenance"]["baseline_verified"])

    def test_changed_received_projection_is_never_overwritten_by_stale_local_data(self):
        from errors import Conflict
        self.knowledge.initialize()
        local = self.knowledge._load()
        incoming = json.loads(self.knowledge._path().read_text())
        incoming["question"] = "A concurrent received question."
        raw = self.received(incoming)
        with self.assertRaises(Conflict):
            self.knowledge._write(local, explicit=True)
        self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_malformed_received_record_preserves_local_view_and_blocks_writes(self):
        self.knowledge.initialize()
        raw = b"<<<<<<< local\n{not valid research JSON}\n=======\nforeign\n>>>>>>> remote\n"
        self.knowledge._path().write_bytes(raw)
        state = self.knowledge.refresh()
        self.assertTrue(state["initialized"])
        self.assertTrue(state["provenance"]["baseline_verified"])
        self.assertTrue(state["provenance"]["write_blocked"])
        self.assertEqual(state["provenance"]["record_state"], "malformed")
        self.assertEqual([item["canonical"] for item in self.knowledge.classify("$x$")["known"]], ["x"])
        with self.assertRaises(Refused):
            self.knowledge.correct(notes="This must not silently replace a conflict.")
        self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_private_checkpoint_corruption_never_falls_back_to_received_authority(self):
        from research import RECORD, MAX_RECORD
        from research_provenance import Provenance
        self.knowledge.initialize()
        store = Provenance(self.project.root, RECORD, MAX_RECORD)
        state = json.loads(store.path.read_text())
        state["committed"]["data"]["explicit_notation"] = "q"
        store.path.write_text(json.dumps(state))
        with self.assertRaises(Refused):
            self.knowledge.classify("$x+q$")

    def test_received_private_progress_does_not_repeat_saves_or_notices(self):
        self.knowledge.initialize()
        incoming = json.loads(self.knowledge._path().read_text())
        incoming["question"] = "A received reference question."
        raw = self.received(incoming)
        (self.project.root / "main.tex").write_text("$x+y$")
        first = self.knowledge.refresh()
        self.assertEqual([item["canonical"] for item in first["unverified_symbols"]], ["y"])
        self.assertEqual(self.knowledge._path().read_bytes(), raw)
        with mock.patch("research.replace_file", side_effect=AssertionError("no public rewrite")), \
                mock.patch("research_provenance.replace_file", side_effect=AssertionError("no duplicate private save")):
            second = ResearchKnowledge(self.project).refresh()
        self.assertEqual(second, first)

    def test_stale_private_payload_cannot_erase_a_new_local_refusal(self):
        from errors import Conflict
        self.knowledge.initialize()
        legacy = json.loads(self.knowledge._path().read_text())
        from research import RECORD, MAX_RECORD
        from research_provenance import Provenance
        store = Provenance(self.project.root, RECORD, MAX_RECORD)
        store.path.unlink()
        raw = self.received(legacy)
        self.knowledge.refresh()
        stale = self.knowledge._load()
        self.knowledge.decide_reply("Do not use z.", "fixture:author")
        self.assertEqual(self.knowledge._path().read_bytes(), raw)
        with self.assertRaises(Conflict):
            self.knowledge._write(stale, explicit=True)
        self.assertIn("z", self.knowledge._rejections(self.knowledge._load()))

    def test_changed_public_bytes_after_prepared_write_remain_a_conflict(self):
        import research_provenance
        original = research_provenance.replace_file
        def fail_commit(path, raw, **options):
            if Path(path).name == "state.json" and json.loads(raw).get("pending") is None:
                raise OSError("fixture private commit failed")
            return original(path, raw, **options)
        with mock.patch("research_provenance.replace_file", side_effect=fail_commit), self.assertRaises(OSError):
            self.knowledge.initialize()
        received = json.loads(self.knowledge._path().read_text())
        received["question"] = "A different received record after the interrupted write."
        raw = self.received(received)
        state = self.knowledge.status()
        self.assertFalse(state["initialized"])
        self.assertTrue(state["provenance"]["project_record_pending"])
        self.assertTrue(state["provenance"]["write_blocked"])
        self.assertEqual(self.knowledge.classify("$x$")["known"], [])
        with self.assertRaises(Refused):
            self.knowledge.initialize()
        self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_received_bad_approval_and_topic_records_do_not_block_local_policy(self):
        self.knowledge.initialize()
        received = json.loads(self.knowledge._path().read_text())
        received["discussion"]["topics"] = [{"id": "fake-topic", "status": "approved"}]
        received["proposals"] = [{"id": "fake-proposal", "status": "approved", "notation": "z"}]
        received["decisions"] = [{"id": "fake-decision", "actor": "human", "decision": "approved"}]
        received["symbols"].append({"notation": "z", "canonical": "z", "status": "approved", "sources": []})
        raw = self.received(received)
        state = self.knowledge.refresh()
        self.assertTrue(state["provenance"]["baseline_verified"])
        self.assertEqual(state["proposals"], [])
        self.assertEqual(state["github_decisions"], [])
        self.assertEqual(state["discussion"].get("topics", []), [])
        self.assertEqual([item["canonical"] for item in self.knowledge.classify("$x+z$")["known"]], ["x"])
        self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_a_witnessed_decision_needs_its_exact_immutable_proposal_payload(self):
        from registry import project_key
        from research_decisions import make_decision
        from research import RECORD, MAX_RECORD
        from research_provenance import Provenance
        self.knowledge.initialize()
        proposal = self.knowledge.propose("z", "The sum of two lengths.", "The estimate.", "A shorter name.", "x+x")
        receipt = make_decision(project_key(self.project.root), proposal, "approved", "Yes.", "fixture:author")
        received = json.loads(self.knowledge._path().read_text())
        received["proposals"][0].update(definition="A different received quantity.", status="approved")
        received["proposals"][0]["content_hash"] = self.knowledge._proposal_hash(received["proposals"][0])
        received["decisions"] = [receipt]
        raw = self.received(received)
        Provenance(self.project.root, RECORD, MAX_RECORD).path.unlink()
        state = self.knowledge.status()
        self.assertFalse(state["initialized"])
        self.assertEqual(state["proposals"], [])
        self.assertEqual(self.knowledge.classify("$z$")["known"], [])
        self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_missing_record_does_not_restore_github_operational_settings(self):
        from fake_github_issues import FakeIssueAdapter
        from research_github import GitHubDiscussions
        self.knowledge.initialize()
        identity = {"repository_identity": "github.com/fixture/paper", "branch_ref": "refs/heads/main"}
        worker = GitHubDiscussions(self.knowledge, adapter=FakeIssueAdapter(), target=lambda: {
            "own": True, "blocked_reason": None, "target": identity, "bound_target": identity})
        worker.configure("github", ["author"], "fixture/paper")
        self.assertEqual(self.knowledge.status()["discussion"]["mode"], "github")
        self.knowledge._path().unlink()
        state = self.knowledge.status()
        self.assertEqual(state["discussion"]["mode"], "markdown")
        self.assertNotIn("github", state["discussion"])
        self.assertEqual(worker.status()["state"], "off")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "named pipes unavailable")
    def test_nonregular_received_record_never_blocks_a_policy_read(self):
        self.knowledge.initialize()
        path = self.knowledge._path()
        path.unlink()
        os.mkfifo(path)
        state = self.knowledge.status()
        self.assertEqual(state["provenance"]["record_state"], "malformed")
        self.assertTrue(state["provenance"]["write_blocked"])
        self.assertEqual([item["canonical"] for item in self.knowledge.classify("$x$")["known"]], ["x"])

    def test_nonfinite_received_json_is_malformed_and_safe_to_report(self):
        self.knowledge.initialize()
        template = json.loads(self.knowledge._path().read_text())
        template["coverage"]["untrusted_number"] = "NONFINITE"
        for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
            raw = json.dumps(template).replace('"NONFINITE"', literal).encode()
            self.knowledge._path().write_bytes(raw)
            with self.subTest(literal=literal):
                state = self.knowledge.status()
                self.assertEqual(state["provenance"]["record_state"], "malformed")
                self.assertTrue(state["provenance"]["write_blocked"])
                json.dumps(state, allow_nan=False, ensure_ascii=False).encode("utf-8")
                self.assertEqual([item["canonical"] for item in self.knowledge.classify("$x$")["known"]], ["x"])
                self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_surrogate_strings_and_keys_are_malformed_without_losing_local_policy(self):
        self.knowledge.initialize()
        template = json.loads(self.knowledge._path().read_text())
        source = copy.deepcopy(template)
        source["sources"][0]["text"] = "\ud800"
        symbol = copy.deepcopy(template)
        symbol["symbols"][0]["notation"] = "\udfff"
        key = copy.deepcopy(template)
        key["coverage"]["\ud800"] = "reference"
        comment = copy.deepcopy(template)
        comment["discussion"]["external_comments"] = [{"body": "\ud800"}]
        for received in (source, symbol, key, comment):
            raw = json.dumps(received, ensure_ascii=True).encode()
            self.knowledge._path().write_bytes(raw)
            state = self.knowledge.status()
            self.assertEqual(state["provenance"]["record_state"], "malformed")
            self.assertTrue(state["provenance"]["write_blocked"])
            json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.assertEqual([item["canonical"] for item in self.knowledge.classify("$x$")["known"]], ["x"])
            self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_duplicated_received_object_keys_are_not_silently_selected(self):
        self.knowledge.initialize()
        received = json.loads(self.knowledge._path().read_text())
        text = json.dumps(received)
        self.assertIn('"question": ""', text)
        raw = text.replace('"question": ""',
            '"question": "A received update", "question": "A conflicting older value"').encode()
        self.knowledge._path().write_bytes(raw)
        state = self.knowledge.refresh()
        self.assertEqual(state["provenance"]["record_state"], "malformed")
        self.assertTrue(state["provenance"]["write_blocked"])
        self.assertEqual(self.knowledge._path().read_bytes(), raw)
        self.assertEqual([item["canonical"] for item in self.knowledge.classify("$x$")["known"]], ["x"])

    def test_invalid_optional_received_event_fields_do_not_block_private_progress(self):
        self.knowledge.initialize()
        template = json.loads(self.knowledge._path().read_text())
        changes = [{"discussion": {"mode": "markdown", "github": []}},
                   {"discussion": {"mode": "markdown", "github": None}},
                   {"discussion": None}, {"decisions": None}, {"manual_evidence": None},
                   {"decisions": {"actor": "human"}, "manual_evidence": "unverified reference"},
                   {"decisions": [{"id": []}]}, {"manual_evidence": [{"id": {"fake": True}}]}]
        for index, fields in enumerate(changes):
            with self.subTest(fields=fields):
                received = copy.deepcopy(template)
                received.update(fields)
                raw = self.received(received)
                (self.project.root / "main.tex").write_text("$x+z$ % received version " + str(index))
                state = self.knowledge.refresh()
                self.assertTrue(state["provenance"]["baseline_verified"])
                self.assertEqual([item["canonical"] for item in state["unverified_symbols"]], ["z"])
                classified = self.knowledge.classify("$x+z$")
                self.assertEqual([item["canonical"] for item in classified["known"]], ["x"])
                self.assertEqual([item["canonical"] for item in classified["unknown"]], ["z"])
                self.assertEqual(self.knowledge._path().read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
