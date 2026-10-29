"""Witnessed GitHub replies reuse the project's notation decisions."""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "prism_local"), str(Path(__file__).parent)]
from errors import Refused
from registry import project_key
from research import ResearchKnowledge
from test_research import project_with
from tmpdirs import tmpdir


class GitHubDecisions(unittest.TestCase):
    def setUp(self):
        self.state = tmpdir()
        self.enterContext(mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state)}))
        self.project = project_with({"main.tex": "$x$"})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()
        self.key = project_key(self.project.root)
        from research_edits import sign_receipt
        self.roster = sign_receipt({"id": "e" * 32, "kind": "github_approver_roster", "project_key": self.key,
            "configuration_id": "d" * 32, "repository_id": 1,
            "accounts": [{"id": 42, "login": "author"}, {"id": 43, "login": "second-author"}]})

    def propose(self, notation="z", **fields):
        return self.knowledge.propose(notation, "The sum of the two lengths.", "The current estimate.",
                                      "A shorter name.", fields.pop("source_expression", "x+x"), **fields)

    @staticmethod
    def subjects(proposals):
        return [{key: proposal[key] for key in ("id", "revision", "content_hash")} for proposal in proposals]

    def source_data(self, proposals, body="Yes, I approve.", **comment_fields):
        return {"repository": {"id": 1, "full_name": "fixture/paper"},
            "issue": {"id": 1001, "number": 1}, "topic_id": "a" * 24, "batch_id": "b" * 32,
            "publication": {"comment_id": 101, "body_sha256": "c" * 64,
                "created_at": "2026-10-29T10:00:00Z", "subjects": self.subjects(proposals)},
            "comment": {"id": 6000, "body": body, "user": {"id": 42, "login": "author", "type": "User"},
                "created_at": "2026-10-29T12:00:00Z", "updated_at": "2026-10-29T12:00:00Z",
                "html_url": "https://github.com/fixture/paper/issues/1#issuecomment-6000", **comment_fields},
            "approvers": ["author", "second-author"],
            "approver_roster": copy.deepcopy(self.roster),
            "configuration_id": "d" * 32}

    def source(self, proposals, body="Yes, I approve.", **comment_fields):
        from research_decisions import make_github_source
        return make_github_source(self.key, self.source_data(proposals, body, **comment_fields))

    def test_designated_human_reply_is_saved_before_later_use(self):
        proposal = self.propose()
        source = self.source([proposal])
        result = self.knowledge.decide_github_reply(self.subjects([proposal]), source)
        self.assertEqual(len(result["decisions"]), 1)
        receipt = result["decisions"][0]
        self.assertEqual(receipt["channel"], "github_comment")
        self.assertEqual(receipt["human_text"], "Yes, I approve.")
        self.assertEqual(receipt["source"]["comment"]["id"], 6000)
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "approved")
        self.assertEqual(self.knowledge.classify("$z$")["unknown"], [])
        registered = next(item for item in self.knowledge.status()["symbols"] if item["canonical"] == "z")
        self.assertEqual(registered["sources"][0]["evidence"], "github_decision")
        self.assertEqual(registered["sources"][0]["url"], source["comment"]["html_url"])
        saved = ResearchKnowledge(self.project).status()
        self.assertEqual(saved["github_decisions"][0]["author"]["login"], "author")
        self.assertEqual(saved["github_decisions"][0]["comment_url"], source["comment"]["html_url"])

    def test_reply_cannot_adopt_notation_outside_its_published_subject(self):
        proposal = self.propose()
        self.propose("w")
        result = self.knowledge.decide_github_reply(self.subjects([proposal]), self.source([proposal],
            "Use $q$ for $x*x$; approve w"))
        self.assertEqual(result["decisions"], [])
        self.assertTrue(result["clarifications"])
        self.assertEqual({p["status"] for p in self.knowledge.status()["proposals"]}, {"pending"})
        self.assertEqual(self.knowledge.classify("$q+w$")["known"], [])

    def test_only_witnessed_actual_designated_user_evidence_can_be_applied(self):
        from research_decisions import make_github_source, verify_github_source
        from research_edits import KEY_FILE
        proposal = self.propose()
        for user in ({"id": 42, "login": "author", "type": "Bot"},
                     {"id": 43, "login": "outsider", "type": "User"},
                     {"id": 44, "login": "author", "type": "User"},
                     {"id": 0, "login": "author", "type": "User"}):
            data = self.source_data([proposal])
            data["comment"]["user"] = user
            with self.subTest(user=user), self.assertRaises(Refused):
                make_github_source(self.key, data)
        source = self.source([proposal])
        self.assertTrue(verify_github_source(source, self.key))
        forged = copy.deepcopy(source)
        forged["id"] = "forged-source"
        payload = json.dumps({key: value for key, value in forged.items() if key != "signature"},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        forged["signature"] = hmac.new((self.state / KEY_FILE).read_bytes(), payload, hashlib.sha256).hexdigest()
        before = self.knowledge._path().read_bytes()
        for evidence in (forged, {**source, "project_key": "foreign"},
                         {**source, "comment": {**source["comment"], "body": "I approve z"}}):
            with self.subTest(evidence=evidence["id"]), self.assertRaises(Refused):
                self.knowledge.decide_github_reply(self.subjects([proposal]), evidence)
        self.assertEqual(self.knowledge._path().read_bytes(), before)
        self.assertEqual(self.knowledge.status()["github_decisions"], [])

    def test_partial_batch_and_comment_edit_versions_are_idempotent(self):
        proposals = [self.propose(), self.propose("w")]
        raw = "prism-batch:" + "b" * 32 + "\nApprove item 2."
        source = self.source(proposals, raw)
        result = self.knowledge.decide_github_reply(self.subjects(proposals), source)
        self.assertEqual([p["status"] for p in self.knowledge.status()["proposals"]], ["pending", "approved"])
        self.assertEqual(result["decisions"][0]["human_text"], raw)
        record = self.knowledge._path()
        before, stamp = record.read_bytes(), record.stat().st_mtime_ns
        replay = self.source(proposals, raw)
        self.assertNotEqual(source["id"], replay["id"])
        self.assertEqual(ResearchKnowledge(self.project).decide_github_reply(self.subjects(proposals), replay)["decisions"], [])
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), (before, stamp))
        edited = self.source(proposals, "prism-batch:" + "b" * 32 + "\nApprove item 1.",
                             updated_at="2026-10-29T12:01:00Z")
        self.knowledge.decide_github_reply(self.subjects(proposals), edited)
        saved = self.knowledge.status()
        self.assertEqual([p["status"] for p in saved["proposals"]], ["approved", "approved"])
        self.assertEqual(len(saved["github_decisions"]), 2)
        self.assertEqual({d["updated_at"] for d in saved["github_decisions"]},
                         {"2026-10-29T12:00:00Z", "2026-10-29T12:01:00Z"})

    def test_clear_replacement_reuses_direct_instruction_and_rejection(self):
        proposal = self.propose()
        source = self.source([proposal], "Use $q$ instead of item 1.")
        result = self.knowledge.decide_github_reply(self.subjects([proposal]), source)
        self.assertEqual(len(result["decisions"]), 2)
        saved = self.knowledge.status()
        self.assertEqual(saved["proposals"][0]["status"], "rejected")
        self.assertEqual(self.knowledge.classify("$q$")["unknown"], [])
        self.assertEqual(self.knowledge.classify("$z$")["unknown"][0]["canonical"], "z")
        self.assertEqual(len(saved["adoptions"]), 1)
        self.assertEqual(saved["adoptions"][0]["definition"], proposal["definition"])
        self.assertEqual(saved["adoptions"][0]["scope"], proposal["scope"])
        self.assertEqual({record["proposal_id"] for record in saved["github_decisions"]}, {proposal["id"]})

    def test_failed_registration_keeps_pending_and_same_source_retries_once(self):
        proposal = self.propose()
        source = self.source([proposal])
        before = self.knowledge._path().read_bytes()
        failures = [mock.patch.object(self.knowledge, "_write", side_effect=OSError("fixture canonical save failed")),
                    mock.patch("research_edits.registry.write_state", side_effect=OSError("fixture witness save failed"))]
        for failure in failures:
            with failure, self.assertRaises(OSError):
                self.knowledge.decide_github_reply(self.subjects([proposal]), source)
            self.assertEqual(self.knowledge._path().read_bytes(), before)
            self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
            self.assertEqual(self.knowledge.classify("$z$")["unknown"][0]["canonical"], "z")
        self.knowledge.decide_github_reply(self.subjects([proposal]), source)
        saved = self.knowledge._path().read_bytes()
        self.assertEqual(ResearchKnowledge(self.project).decide_github_reply(self.subjects([proposal]), source)["decisions"], [])
        self.assertEqual(self.knowledge._path().read_bytes(), saved)
        self.assertEqual(len(self.knowledge.status()["github_decisions"]), 1)

    def test_stale_published_version_cannot_authorize_a_direct_alternative(self):
        proposal = self.propose()
        source = self.source([proposal], "Use $q$ for $x+x$.")
        current = self.propose(source_expression="x*x")
        self.assertEqual(current["revision"], 2)
        before = self.knowledge._path().read_bytes()
        with self.assertRaises(Refused):
            self.knowledge.decide_github_reply(self.subjects([proposal]), source)
        self.assertEqual(self.knowledge._path().read_bytes(), before)
        self.assertEqual(self.knowledge.classify("$q$")["unknown"][0]["canonical"], "q")

    def test_family_and_expression_refusal_share_existing_policy_state(self):
        proposal = self.propose()
        result = self.knowledge.decide_github_reply(self.subjects([proposal]), self.source([proposal],
            "Use $a_i$ for $x+x$ with i in {1,2}."))
        self.assertEqual(len(result["decisions"]), 1)
        self.assertEqual(self.knowledge.classify("$a_1+a_2$")["unknown"], [])
        self.assertEqual(self.knowledge.classify("$a_3$")["unknown"][0]["canonical"], "a_3")
        result = self.knowledge.decide_github_reply(self.subjects([proposal]), self.source([proposal],
            "Reject item 1 and keep the original expression expanded.", updated_at="2026-10-29T12:01:00Z"))
        self.assertEqual(len(result["decisions"]), 1)
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "rejected")
        self.assertTrue(ResearchKnowledge(self.project).expression_rejected("x+x"))
        with self.assertRaises(Refused):
            self.propose("q")

    def test_quotes_and_conditional_comments_leave_the_batch_pending(self):
        proposal = self.propose()
        before = self.knowledge._path().read_bytes()
        for body in ('"Yes, I approve."', "> Yes, I approve.", "`yes`", "The author said yes.",
                     "Approve item 1 if the calculation is correct."):
            with self.subTest(body=body):
                result = self.knowledge.decide_github_reply(self.subjects([proposal]), self.source([proposal], body))
                self.assertEqual(result["decisions"], [])
                self.assertEqual(self.knowledge._path().read_bytes(), before)
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")

    def test_forged_mirrors_cannot_become_a_verified_decision(self):
        proposal = self.propose()
        source = self.source([proposal])
        data = self.knowledge._load()
        data.setdefault("decisions", []).append({"purpose": "notation_decision", "id": "f" * 32,
            "project_key": self.key, "actor": "human", "channel": "github_comment",
            "proposal_id": proposal["id"], "revision": proposal["revision"], "content_hash": proposal["content_hash"],
            "canonical": proposal["canonical"], "decision": "approved", "source": source,
            "human_text": source["comment"]["body"], "conversation": "github:forged", "at": 1,
            "signature": "0" * 64})
        self.knowledge._path().write_text(json.dumps(data))
        raw = self.knowledge._path().read_bytes()
        self.assertEqual(self.knowledge.status()["github_decisions"], [])
        self.assertEqual(self.knowledge.classify("$z$")["unknown"][0]["canonical"], "z")
        self.assertEqual(self.knowledge._path().read_bytes(), raw)
        data["proposals"][0]["status"] = "approved"
        self.knowledge._path().write_text(json.dumps(data))
        raw = self.knowledge._path().read_bytes()
        state = self.knowledge.status()
        self.assertEqual(state["github_decisions"], [])
        self.assertEqual(state["proposals"][0]["status"], "pending")
        self.assertEqual(self.knowledge.classify("$z$")["known"], [])
        self.assertEqual(self.knowledge._path().read_bytes(), raw)

    def test_source_rejects_wrong_publication_dates_context_and_roster(self):
        from research_decisions import make_github_source
        proposal = self.propose()
        changes = [("comment", "created_at", "2026-10-29T09:00:00Z"),
                   ("comment", "updated_at", "2026-10-29T11:59:59Z"),
                   ("comment", "html_url", "https://github.com/foreign/paper/issues/1#issuecomment-6000"),
                   ("comment", "body_sha256", "0" * 64),
                   ("approver_roster", "configuration_id", "wrong"),
                   ("approver_roster", "repository_id", 99),
                   ("approver_roster", "signature", "0" * 64)]
        for group, key, value in changes:
            data = self.source_data([proposal])
            data[group][key] = value
            with self.subTest(group=group, key=key), self.assertRaises(Refused):
                make_github_source(self.key, data)
        source = self.source([proposal])
        with self.assertRaises(Refused):
            self.knowledge.decide_github_reply([{**self.subjects([proposal])[0], "revision": 2}], source)

    def test_explicit_different_batch_header_cannot_be_ignored(self):
        proposal = self.propose()
        source = self.source([proposal], "prism-batch:" + "a" * 32 + "\nApprove item 1.")
        with self.assertRaises(Refused):
            self.knowledge.decide_github_reply(self.subjects([proposal]), source)
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")

    def test_lifecycle_guard_runs_under_lock_immediately_before_registration(self):
        from repository import FilesLock
        self.project.files_lock = FilesLock(lambda: None)
        proposal = self.propose()
        source = self.source([proposal])
        before = self.knowledge._path().read_bytes()
        calls = []
        def guard():
            self.assertTrue(self.project.files_lock.held())
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("fixture project closed before commit")
        with self.assertRaisesRegex(RuntimeError, "project closed"):
            self.knowledge.decide_github_reply(self.subjects([proposal]), source, guard=guard)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.knowledge._path().read_bytes(), before)
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        self.knowledge.decide_github_reply(self.subjects([proposal]), source)
        self.assertEqual(self.knowledge.classify("$z$")["unknown"], [])

    def test_exact_rejected_subject_requires_explicit_reopen_before_approval(self):
        proposal = self.propose()
        subjects = self.subjects([proposal])
        self.knowledge.decide_github_reply(subjects, self.source([proposal], "Reject item 1."))
        refusal = self.knowledge.decide_github_reply(subjects, self.source([proposal], "Yes, I approve.",
            updated_at="2026-10-29T12:01:00Z"))
        self.assertEqual(refusal["decisions"], [])
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "rejected")
        reopened = self.knowledge.decide_github_reply(subjects, self.source([proposal], "Reopen item 1.",
            updated_at="2026-10-29T12:02:00Z"))
        self.assertEqual(len(reopened["decisions"]), 1)
        self.assertEqual(reopened["decisions"][0]["kind"], "reopen")
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        self.assertEqual(self.knowledge.classify("$z$")["unknown"][0]["canonical"], "z")
        self.knowledge.decide_github_reply(subjects, self.source([proposal], "Yes, I approve.",
            updated_at="2026-10-29T12:03:00Z"))
        self.assertEqual(self.knowledge.classify("$z$")["unknown"], [])

    def test_exact_rejected_subject_can_receive_a_clear_replacement(self):
        proposal = self.propose()
        subjects = self.subjects([proposal])
        self.knowledge.decide_github_reply(subjects, self.source([proposal], "Reject item 1."))
        replacement = self.knowledge.decide_github_reply(subjects, self.source([proposal], "Use $q$ instead of item 1.",
            updated_at="2026-10-29T12:01:00Z"))
        self.assertEqual(len(replacement["decisions"]), 1)
        self.assertEqual(replacement["decisions"][0]["kind"], "adopt")
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "rejected")
        self.assertEqual(self.knowledge.classify("$q$")["unknown"], [])

    def test_reopen_and_replace_cannot_use_an_expired_published_version(self):
        proposal = self.propose()
        subjects = self.subjects([proposal])
        self.knowledge.decide_github_reply(subjects, self.source([proposal], "Reject item 1."))
        self.knowledge.decide_github_reply(subjects, self.source([proposal], "Reopen item 1.",
            updated_at="2026-10-29T12:01:00Z"))
        current = self.propose(source_expression="x*x")
        self.assertEqual(current["revision"], 2)
        before = self.knowledge._path().read_bytes()
        for body in ("Reopen item 1.", "Use $q$ instead of item 1."):
            with self.subTest(body=body), self.assertRaises(Refused):
                self.knowledge.decide_github_reply(subjects, self.source([proposal], body,
                    updated_at="2026-10-29T12:02:00Z"))
        self.assertEqual(self.knowledge._path().read_bytes(), before)
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        self.assertEqual(self.knowledge.classify("$q$")["unknown"][0]["canonical"], "q")

    def test_existing_unpublished_alternative_does_not_cancel_other_batch_decisions(self):
        proposals = [self.propose(), self.propose("v")]
        other = self.propose("q")
        result = self.knowledge.decide_github_reply(self.subjects(proposals), self.source(proposals,
            "Approve item 1; use $q$ instead of item 2."))
        self.assertEqual(len(result["decisions"]), 3)
        self.assertEqual({p["canonical"]: p["status"] for p in self.knowledge.status()["proposals"]},
                         {"z": "approved", "v": "rejected", "q": "pending"})
        self.assertEqual(self.knowledge.classify("$z+q$")["unknown"], [])
        self.assertFalse(any(receipt.get("proposal_id") == other["id"] for receipt in result["decisions"]))

    def test_existing_alternative_keeps_its_unreviewed_definition_pending(self):
        proposal = self.propose()
        other = self.knowledge.propose("q", "A different unreviewed quantity.", "Another estimate.",
                                       "A different candidate.", "x*x")
        self.knowledge.decide_github_reply(self.subjects([proposal]), self.source([proposal],
            "Use $q$ instead of item 1."))
        state = self.knowledge.status()
        pending = next(item for item in state["proposals"] if item["id"] == other["id"])
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["definition"], other["definition"])
        self.assertEqual(pending["revision"], other["revision"])
        self.assertEqual(state["adoptions"][0]["definition"], proposal["definition"])
        self.assertEqual(state["adoptions"][0]["scope"], proposal["scope"])
        self.assertEqual(self.knowledge.classify("$q$")["unknown"], [])

    def test_historical_instruction_keeps_its_published_proposal_version(self):
        proposal = self.propose()
        subjects = self.subjects([proposal])
        self.knowledge.decide_github_reply(subjects, self.source([proposal], "Use $q$ instead of item 1."))
        self.knowledge.decide_github_reply(subjects, self.source([proposal], "Reopen item 1.",
            updated_at="2026-10-29T12:01:00Z"))
        current = self.propose(source_expression="x*x")
        self.assertEqual(current["revision"], 2)
        original = next(record for record in self.knowledge.status()["github_decisions"] if record["kind"] == "adopt")
        self.assertEqual(original["proposal_id"], proposal["id"])
        self.assertEqual(original["revision"], 1)
        self.assertEqual(original["content_hash"], proposal["content_hash"])


if __name__ == "__main__":
    unittest.main()
