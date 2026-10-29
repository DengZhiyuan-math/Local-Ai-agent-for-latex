"""Version-bound editor decisions and private conversational subjects."""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))


class DecisionEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        patch = mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state)})
        patch.start()
        self.addCleanup(patch.stop)
        self.proposal = {"id": "proposal-one", "project_key": "p", "revision": 1,
            "content_hash": "a" * 64, "notation": "z", "canonical": "z", "status": "pending",
            "candidate_kind": "identifier", "needs_details": False,
            "definition": "The sum of the two lengths.", "scope": "The current estimate.",
            "reason": "A shorter name.", "source_expression": "x+x"}

    def test_decision_binds_raw_message_and_exact_proposal_version(self):
        from research_decisions import make_decision, verify_decision
        raw = "  Yes, I approve.\n"
        receipt = make_decision("p", self.proposal, "approved", raw, "fixture:conversation-one")
        self.assertEqual(receipt["purpose"], "notation_decision")
        self.assertEqual(receipt["human_text"], raw)
        self.assertEqual(receipt["actor"], "human")
        self.assertEqual(receipt["channel"], "editor_chat")
        self.assertEqual(receipt["proposal_id"], "proposal-one")
        self.assertEqual(receipt["conversation"], "fixture:conversation-one")
        self.assertTrue(verify_decision(receipt, "p", self.proposal))
        decided = {**self.proposal, "status": "approved"}
        self.assertTrue(verify_decision(copy.deepcopy(receipt), "p", decided))

    def test_ordinary_replies_are_complete_answers_not_quoted_substrings(self):
        from research_decisions import parse_reply
        for text in ("Yes, I approve.", "yes please", "I approve this proposal.", "Approved.",
                     "Sure.", "Okay.", "Go ahead.", "Yes, that's fine.", "同意。", "我同意这个提案。",
                     "可以，按这个来。", "好的", "批准这个提案"):
            with self.subTest(text=text):
                self.assertEqual(parse_reply(text), "approved")
        for text in ("No, I reject.", "No thanks.", "I reject this proposal.", "Rejected.",
                     "Do not adopt it.", "Don't use this notation.", "不同意。", "我拒绝这个提案。",
                     "不要采用", "不采用这个记号"):
            with self.subTest(text=text):
                self.assertEqual(parse_reply(text), "rejected")
        for text in ('"Yes, I approve."', '`yes`', "> yes", "The author said yes.",
                     "I approve if it is correct.", "yes, the lemma is correct", "yes?",
                     "同意吗？", "如果没错就同意", "也许可以", "yes, use q instead",
                     "no, use q instead", "approve z and q", None, {"human": "yes"}):
            with self.subTest(text=text):
                self.assertIsNone(parse_reply(text))

    def test_subject_survives_reload_and_is_scoped_to_project_and_conversation(self):
        import importlib
        import research_decisions as decisions
        self.assertIsNone(decisions.get_subject("p", "fixture:one"))
        self.assertEqual(list(self.state.iterdir()), [])
        decisions.set_subject("p", "fixture:one", [self.proposal])
        decisions = importlib.reload(decisions)
        self.assertEqual(decisions.get_subject("p", "fixture:one"),
                         {"id": "proposal-one", "revision": 1, "content_hash": "a" * 64})
        self.assertIsNone(decisions.get_subject("other", "fixture:one"))
        self.assertIsNone(decisions.get_subject("p", "fixture:two"))
        decisions.set_subject("p", "fixture:one", [])
        self.assertIsNone(decisions.get_subject("p", "fixture:one"))

    def test_changed_identity_version_purpose_or_raw_message_is_not_a_decision(self):
        from research_decisions import make_decision, verify_decision
        receipt = make_decision("p", self.proposal, "approved", "同意。", "fixture:one")
        for fields in ({"id": "different"}, {"revision": 2}, {"content_hash": "b" * 64},
                       {"canonical": "q"}, {"project_key": "other"}, {"status": "rejected"}):
            with self.subTest(proposal=fields):
                self.assertFalse(verify_decision(receipt, "p", {**self.proposal, **fields}))
        self.assertFalse(verify_decision(receipt, "other", self.proposal))
        for fields in ({"purpose": "manual_edit"}, {"actor": "agent"}, {"channel": "tool"},
                       {"human_text": "No, I reject."}, {"decision": "rejected"},
                       {"conversation": "fixture:other"}, {"signature": "0" * 64}):
            with self.subTest(receipt=fields):
                self.assertFalse(verify_decision({**receipt, **fields}, "p", self.proposal))

    def test_manual_receipts_and_key_only_forgery_cannot_supply_decision_evidence(self):
        from research_decisions import make_decision, verify_decision
        from research_edits import KEY_FILE, WITNESS_FILE, make_receipt
        receipt = make_decision("p", self.proposal, "approved", "yes", "fixture:one")
        manual = make_receipt("p", "main.tex", b"$x$", b"$x+z$")
        self.assertFalse(verify_decision(manual, "p", self.proposal))
        forged = {**receipt, "id": "forged-message", "human_text": "Yes, I approve."}
        payload = json.dumps({k: v for k, v in forged.items() if k != "signature"},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        forged["signature"] = hmac.new((self.state / KEY_FILE).read_bytes(), payload, hashlib.sha256).hexdigest()
        self.assertFalse(verify_decision(forged, "p", self.proposal))
        self.assertTrue(verify_decision(receipt, "p", self.proposal))
        (self.state / WITNESS_FILE).unlink()
        self.assertFalse(verify_decision(receipt, "p", self.proposal))

    def test_only_complete_pending_proposals_and_matching_answers_can_be_signed(self):
        from errors import Refused
        from research_decisions import make_decision
        for fields in ({"needs_details": True}, {"candidate_kind": "unresolved_fragment"},
                       {"status": "approved"}, {"status": "rejected"}, {"definition": ""},
                       {"revision": True}, {"revision": 0}):
            with self.subTest(fields=fields), self.assertRaises(Refused):
                make_decision("p", {**self.proposal, **fields}, "approved", "yes", "fixture:one")
        for reply in ("no", '"yes"', "yes if it is correct", "yes, use q instead"):
            with self.subTest(reply=reply), self.assertRaises(Refused):
                make_decision("p", self.proposal, "approved", reply, "fixture:one")

    def test_incomplete_multiple_or_decided_candidates_clear_the_subject(self):
        from research_decisions import get_subject, set_subject
        for proposals in ([], [self.proposal, {**self.proposal, "id": "two"}],
                          [{**self.proposal, "needs_details": True}],
                          [{**self.proposal, "status": "rejected"}]):
            set_subject("p", "fixture:one", [self.proposal])
            set_subject("p", "fixture:one", proposals)
            with self.subTest(proposals=proposals):
                self.assertIsNone(get_subject("p", "fixture:one"))

    def test_witness_failure_does_not_issue_a_decision(self):
        from research_decisions import make_decision
        with mock.patch("research_edits.registry.write_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                make_decision("p", self.proposal, "approved", "yes", "fixture:one")


if __name__ == "__main__":
    unittest.main()
