"""GitHub proposal publication and actual-comment routing with no external calls."""
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "prism_local"), str(Path(__file__).parent)]
import test_research_github as discussions


class GitHubApprovals(unittest.TestCase):
    worker = discussions.GitHubDiscussionTests.worker
    topic = discussions.GitHubDiscussionTests.topic

    def setUp(self):
        discussions.GitHubDiscussionTests.setUp(self)
        self.adapter.user = lambda login: {"id": 42, "login": login, "type": "User", "html_url": "https://github.com/" + login}

    def proposal(self, notation="z"):
        return self.knowledge.propose(notation, "the original expression", "this argument", "avoid repetition",
                                      source_expression="x+x")

    @staticmethod
    def subject(proposal):
        return {key: proposal[key] for key in ("id", "revision", "content_hash")}

    def test_shared_pending_proposal_is_a_separate_literal_comment_on_existing_issue(self):
        topic, proposal = self.topic(), self.proposal()
        worker = self.worker()
        worker.configure("github", ["author"])
        worker.share_proposals(topic["id"], [self.subject(proposal)])
        status = worker.status()
        self.assertEqual(status["proposal_batches"][0]["publication"], "pending")
        worker.tick()
        status = worker.status()
        batch = status["proposal_batches"][0]
        self.assertEqual(batch["publication"], "published")
        self.assertEqual(len(self.adapter.rows), 1)
        comments = self.adapter.comments[1]
        self.assertEqual(len(comments), 1)
        self.assertIn("Pending notation proposals", comments[0]["body"])
        self.assertIn(proposal["content_hash"], comments[0]["body"])
        self.assertNotIn(proposal["content_hash"], self.adapter.rows[0]["body"])
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        worker.share_proposals(topic["id"], [self.subject(proposal)])
        self.now[0] += 31
        worker.tick()
        self.assertEqual(len(worker.status()["proposal_batches"]), 1)
        self.assertEqual(len(self.adapter.comments[1]), 1)

    def prepare(self, proposals=None, approvers=None):
        topic = self.topic()
        proposals = proposals or [self.proposal()]
        worker = self.worker()
        worker.configure("github", ["author"] if approvers is None else approvers)
        worker.share_proposals(topic["id"], [self.subject(p) for p in proposals])
        worker.tick()
        return worker, worker.status()["proposal_batches"][0]

    def reply(self, batch, text="approved", *, identity=6000, author="author", kind="User", created="2026-10-29T12:00:00Z", explicit=True):
        row = {"id": identity, "body": (f"prism-batch:{batch['id']}\n" if explicit else "") + text,
               "user": {"id": 42, "login": author, "type": kind},
               "created_at": created, "updated_at": created,
               "issue_url": "https://api.github.com/repos/fixture/paper/issues/1",
               "html_url": f"https://github.com/fixture/paper/issues/1#issuecomment-{identity}"}
        self.adapter.comments[1].append(row)
        return row

    def test_actual_designated_human_reply_registers_before_later_use(self):
        worker, batch = self.prepare()
        self.reply(batch)
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "approved")
        self.assertFalse(self.knowledge.classify("$z$")["unknown"])
        receipts = self.knowledge.status()["github_decisions"]
        self.assertEqual(len(receipts), 1)
        self.assertIn("approved", str(receipts))

    def test_ambiguous_reply_never_changes_meaning_after_one_batch_is_decided(self):
        first = self.proposal("z")
        worker, batch = self.prepare([first])
        second = self.proposal("w")
        topic_id = batch["topic_id"]
        worker.share_proposals(topic_id, [self.subject(second)])
        worker.tick()
        other = next(b for b in worker.status()["proposal_batches"] if b["id"] != batch["id"])
        self.reply(batch, explicit=False)
        self.reply(batch, identity=6001)
        self.now[0] += 31
        worker.tick()
        self.now[0] += 31
        worker.tick()
        self.assertEqual([p["status"] for p in self.knowledge.status()["proposals"]], ["approved", "pending"])
        self.reply(other, identity=6002, created="2026-10-29T13:00:00Z", explicit=False)
        self.now[0] += 31
        worker.tick()
        self.assertEqual([p["status"] for p in self.knowledge.status()["proposals"]], ["approved", "approved"])

    def test_actual_author_type_identity_and_unquoted_decision_are_required(self):
        worker, batch = self.prepare()
        self.reply(batch, author="guest")
        self.reply(batch, identity=6001, kind="Bot")
        self.reply(batch, identity=6002, text="> author said approved")
        self.reply(batch, identity=6003, author="guest", text="I am author. Approved.")
        forged_identity = self.reply(batch, identity=6004)
        forged_identity["user"]["id"] = 99
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        self.assertEqual(self.knowledge.status()["github_decisions"], [])
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        self.reply(batch, identity=6005)
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "approved")

    def test_old_proposal_version_and_old_comment_cannot_approve_a_new_version(self):
        worker, batch = self.prepare()
        replacement = self.knowledge.propose("z", "a changed expression", "this argument", "a revised reason", "x+x+x")
        old = self.reply(batch, explicit=False)
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        self.assertEqual(worker.status()["proposal_batches"][0]["publication"], "stale")
        worker.share_proposals(batch["topic_id"], [self.subject(replacement)])
        worker.tick()
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        new = next(row for row in worker.status()["proposal_batches"] if row["id"] != batch["id"])
        self.reply(new, identity=6001)
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "approved")
        self.assertEqual(old["body"], "approved")

    def test_unknown_publication_recovers_with_a_reply_without_duplicate_post(self):
        topic, proposal = self.topic(), self.proposal()
        worker = self.worker()
        worker.configure("github", ["author"])
        worker.share_proposals(topic["id"], [self.subject(proposal)])
        original = worker._write
        def failed_confirmation(state):
            if any(batch.get("publication") for repo in state["repositories"].values()
                   for record in repo["topics"].values() for batch in record.get("proposal_batches", {}).values()):
                raise OSError("controlled confirmation persistence failure")
            return original(state)
        with mock.patch.object(worker, "_write", side_effect=failed_confirmation):
            worker.tick()
        batch = worker.status()["proposal_batches"][0]
        self.assertEqual(batch["publication"], "unknown")
        self.reply(batch)
        recovered = self.worker()
        recovered.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "approved")
        self.assertEqual(sum("Pending notation proposals" in row["body"] for row in self.adapter.comments[1]), 1)

    def test_canonical_failure_keeps_pending_and_ack_failure_does_not_duplicate_decisions(self):
        worker, batch = self.prepare()
        self.reply(batch)
        self.now[0] += 31
        with mock.patch.object(self.knowledge, "_write", side_effect=OSError("controlled canonical failure")):
            worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        self.assertEqual(self.knowledge.status()["github_decisions"], [])
        self.now[0] += 31
        original = worker._write
        def failed_ack(state):
            if any(record.get("proposal_replies") for repo in state["repositories"].values() for record in repo["topics"].values()):
                raise OSError("controlled reply acknowledgement failure")
            return original(state)
        with mock.patch.object(worker, "_write", side_effect=failed_ack):
            worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "approved")
        self.assertEqual(len(self.knowledge.status()["github_decisions"]), 1)
        recovered = self.worker()
        recovered.tick()
        self.assertEqual(len(self.knowledge.status()["github_decisions"]), 1)

    def test_closing_page_after_api_verification_prevents_the_decision_commit(self):
        import research_github_approvals as approvals
        worker, batch = self.prepare()
        self.reply(batch)
        original = approvals.make_github_source
        def close_before_commit(*args):
            source = original(*args)
            self.open[0] = False
            return source
        self.now[0] += 31
        with mock.patch.object(approvals, "make_github_source", side_effect=close_before_commit):
            worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        self.assertEqual(self.knowledge.status()["github_decisions"], [])
        self.open[0] = True
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "approved")

    def test_unknown_publication_context_defers_a_reply_until_the_original_comment_is_found(self):
        from test_research_github import DeliveryError
        topic, proposal = self.topic(), self.proposal()
        worker = self.worker()
        worker.configure("github", ["author"])
        worker.share_proposals(topic["id"], [self.subject(proposal)])
        original = self.adapter.create_comment
        def lost_response(*args):
            original(*args)
            raise DeliveryError(True)
        with mock.patch.object(self.adapter, "create_comment", side_effect=lost_response):
            worker.tick()
        batch = worker.status()["proposal_batches"][0]
        reply = self.reply(batch)
        with mock.patch.object(self.adapter, "list_comments", return_value=[copy.deepcopy(reply)]):
            self.now[0] += 31
            worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        state = worker._read()
        record = next(iter(state["repositories"]["101"]["topics"].values()))
        self.assertFalse(record.get("proposal_replies"))
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "approved")

    def test_older_comment_edit_cannot_replay_after_a_newer_unrelated_version(self):
        worker, batch = self.prepare()
        row = self.reply(batch, text="A question without a notation decision.")
        row["updated_at"] = "2026-10-29T13:00:00Z"
        self.now[0] += 31
        worker.tick()
        row["body"] = f"prism-batch:{batch['id']}\napproved"
        row["updated_at"] = "2026-10-29T12:00:00Z"
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        row["updated_at"] = "2026-10-29T14:00:00Z"
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "approved")

    def test_config_epoch_change_requires_new_sharing_and_new_account_pins(self):
        worker, batch = self.prepare()
        self.adapter.user = lambda login: {"id": 99, "login": login, "type": "User", "html_url": "https://github.com/" + login}
        worker.configure("markdown", ["author"])
        worker.configure("github", ["author"])
        reply = self.reply(batch)
        reply["user"]["id"] = 99
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        proposal = self.knowledge.status()["proposals"][0]
        worker.share_proposals(batch["topic_id"], [self.subject(proposal)])
        worker.tick()
        new = next(row for row in worker.status()["proposal_batches"] if row["id"] != batch["id"])
        new_reply = self.reply(new, identity=6001)
        new_reply["user"]["id"] = 99
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "approved")

    def test_named_proposal_narrows_distinct_and_overlapping_batches(self):
        first, second = self.proposal("z"), self.proposal("w")
        worker, batch = self.prepare([first])
        worker.share_proposals(batch["topic_id"], [self.subject(first), self.subject(second)])
        worker.tick()
        self.reply(batch, "Approve $z$", explicit=False)
        self.now[0] += 31
        worker.tick()
        self.assertEqual([p["status"] for p in self.knowledge.status()["proposals"]], ["approved", "pending"])
        self.reply(batch, "Approve $w$", identity=6001, explicit=False)
        self.now[0] += 31
        worker.tick()
        self.assertEqual([p["status"] for p in self.knowledge.status()["proposals"]], ["approved", "approved"])

    def test_unseen_old_plain_reply_remains_ambiguous_after_one_proposal_is_revised(self):
        first, second = self.proposal("z"), self.proposal("w")
        worker, batch = self.prepare([first])
        worker.share_proposals(batch["topic_id"], [self.subject(second)])
        worker.tick()
        self.reply(batch, explicit=False)
        self.knowledge.propose("z", "new original expression", "this argument", "revised context", "x+x+x")
        self.now[0] += 31
        worker.tick()
        self.assertEqual([p["status"] for p in self.knowledge.status()["proposals"]], ["pending", "pending"])

    def test_explicit_reopen_keeps_the_rejected_subject_pending_without_adoption(self):
        worker, batch = self.prepare()
        self.reply(batch, "rejected")
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "rejected")
        self.reply(batch, "Reopen item 1", identity=6001, created="2026-10-29T13:00:00Z")
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        self.assertTrue(self.knowledge.classify("$z$")["unknown"])
        self.reply(batch, identity=6002, created="2026-10-29T14:00:00Z")
        self.now[0] += 31
        worker.tick()
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "approved")
