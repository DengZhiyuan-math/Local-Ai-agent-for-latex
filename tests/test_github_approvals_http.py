"""GitHub human decisions through real editor HTTP and a controlled adapter."""
from datetime import datetime, timedelta, timezone
import json
import threading
import time
import unittest

import test_research_github_http as github
import test_research_guard_http as guard
import test_research_http as base
import test_research_topics_http as topics
from test_agent import call_chunks


class GitHubApprovalsHTTP(unittest.TestCase):
    setUp = github.ResearchGitHubHTTP.setUp
    tearDown = github.ResearchGitHubHTTP.tearDown
    start = github.ResearchGitHubHTTP.start
    stop = github.ResearchGitHubHTTP.stop
    request = github.ResearchGitHubHTTP.request
    initialize = github.ResearchGitHubHTTP.initialize
    events = github.ResearchGitHubHTTP.events
    finish = github.ResearchGitHubHTTP.finish
    requests = github.ResearchGitHubHTTP.requests
    turn = github.ResearchGitHubHTTP.turn
    record = github.ResearchGitHubHTTP.record
    state = github.ResearchGitHubHTTP.state
    configure = github.ResearchGitHubHTTP.configure
    control = github.ResearchGitHubHTTP.control
    remote = github.ResearchGitHubHTTP.remote
    open_page = github.ResearchGitHubHTTP.open_page
    close_page = github.ResearchGitHubHTTP.close_page

    def topic(self):
        events = self.record([topics.entry("idea", "Compare the direct estimate.")])
        return next(event["topic_id"] for event in events if event["t"] == "research_record")

    def propose(self, names=("z",), **changes):
        calls = [(name, "propose_notation", {**guard.PROPOSAL, "notation": name, **changes}) for name in names]
        events = self.finish(self.turn([[guard.call(*calls)]], human_text="Discuss candidate names before using them."))
        proposals = [event["proposal"] for event in events if event["t"] == "notation_proposal"]
        self.assertEqual(len(proposals), len(names), events)
        self.assertEqual(next(event for event in events if event["t"] == "done")["changed"], [])
        return proposals

    def share(self, topic_id, proposals):
        subjects = [{key: proposal[key] for key in ("id", "revision", "content_hash")} for proposal in proposals]
        code, state = self.request("/api/research/github/proposals", {
            "project_key": self.key, "topic_id": topic_id, "subjects": subjects})
        self.assertEqual(code, 200, state)
        batches = [batch for batch in state["github_discussion"]["proposal_batches"]
                   if batch["topic_id"] == topic_id and batch["subjects"] == subjects]
        self.assertEqual(len(batches), 1, state["github_discussion"])
        return batches[0]

    def publish(self, names=("z",), approvers=None):
        self.initialize(); self.configure(approvers=approvers); self.open_page()
        if approvers and "coauthor" in approvers:
            self.control("user", login="coauthor", row={"id": 55, "login": "coauthor", "type": "User",
                "html_url": "https://github.com/coauthor"})
        topic_id = self.topic()
        self.control("tick")
        proposals = self.propose(names)
        batch = self.share(topic_id, proposals)
        self.control("tick")
        self.assertEqual(self.state()["github_discussion"]["proposal_batches"][-1]["publication"], "published")
        return topic_id, proposals, batch

    def remote_reply(self, batch, text, *, user=None, marker=True, **options):
        body = ("prism-batch:" + batch["id"] + "\n" if marker else "") + text
        if "created_at" not in options:
            candidates = [row for rows in self.remote()["comments"].values() for row in rows
                          if batch["id"] in row["body"]]
            self.assertTrue(candidates, "a reply follows the actual published candidate comment")
            published = max(datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")) for row in candidates)
            options["created_at"] = (published + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        options.setdefault("updated_at", options["created_at"])
        self.control("comment", body=body,
            user=user or {"id": 42, "login": "author", "type": "User"}, **options)
        return body

    def use_saved_name(self, name="z"):
        original = (self.root / "main.tex").read_text()
        changed = original.replace(r"$\ell+\ell_+$", "$" + name + "$")
        events = self.finish(self.turn([
            call_chunks("use-saved", "write_file", {"path": "main.tex", "content": changed}),
            [base.say("The current estimate is $" + name + "$.")]],
            human_text="Continue the current estimate.", mode="edit"))
        self.assertEqual((self.root / "main.tex").read_text(), changed)
        self.assertIn("$" + name + "$", "".join(event.get("text", "") for event in events if event["t"] == "delta"))
        self.assertFalse(any(event["t"] == "notation_proposal" for event in events), events)

    def test_pending_proposal_is_shared_on_the_existing_topic_issue(self):
        self.initialize(); self.configure(); self.open_page()
        topic_id = self.topic()
        self.control("tick")
        proposals = self.propose()
        requests = len(self.requests())
        batch = self.share(topic_id, proposals)
        self.assertEqual(batch["publication"], "pending")
        self.assertEqual(batch["topic_id"], topic_id)
        self.assertEqual(self.state()["proposals"][0]["status"], "pending")
        self.control("tick")
        batch = self.state()["github_discussion"]["proposal_batches"][0]
        self.assertEqual(batch["publication"], "published")
        self.assertTrue(batch["comment_url"].startswith("https://github.com/fixture/paper/issues/1#"))
        remote = self.remote()
        self.assertEqual(len(remote["issues"]), 1)
        self.assertEqual(len(remote["comments"]["1"]), 1)
        self.assertIn("z", remote["comments"]["1"][0]["body"])
        self.assertEqual(len(self.requests()), requests)

    def test_one_designated_human_reply_registers_before_neutral_source_and_chat_use(self):
        _topic, proposals, batch = self.publish(approvers=["author", "coauthor"])
        before = (self.root / "main.tex").read_bytes()
        requests = len(self.requests())
        body = self.remote_reply(batch, "Yes, I approve.", id=6101,
            user={"id": 55, "login": "coauthor", "type": "User"})
        self.control("tick")
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "approved")
        self.assertEqual((self.root / "main.tex").read_bytes(), before)
        self.assertEqual(len(self.requests()), requests)
        self.assertEqual(len(self.remote()["issues"]), 1)
        self.assertEqual(len(state["github_decisions"]), 1)
        evidence = state["github_decisions"][0]
        self.assertEqual(evidence["channel"], "github_comment")
        self.assertEqual(evidence["human_text"], body)
        self.assertEqual(evidence["author"]["login"], "coauthor")
        self.assertEqual(evidence["comment_id"], 6101)
        self.assertEqual(evidence["proposal_id"], proposals[0]["id"])
        self.assertEqual(evidence["content_hash"], proposals[0]["content_hash"])
        self.use_saved_name()

    def test_fresh_named_rejection_revokes_a_published_approval_without_replaying_old_comments(self):
        topic_id, proposals, batch = self.publish()
        original = (self.root / "main.tex").read_text()
        changed = original.replace(r"$\ell+\ell_+$", "$z$")
        approval_body = self.remote_reply(batch, "Approve $z$.", id=6201)
        self.control("tick")
        approved = self.state()
        self.assertEqual(approved["proposals"][0]["status"], "approved")
        approval = approved["github_decisions"][0]
        approval_receipt = next(row for row in approved["decisions"] if row["id"] == approval["id"])
        self.assertEqual(approval["human_text"], approval_body)
        old = next(row for row in self.remote()["comments"]["1"] if row["id"] == 6201)
        edited_at = (datetime.fromisoformat(old["updated_at"].replace("Z", "+00:00"))
                     + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
        self.remote_reply(batch, "Reject $z$.", id=6201,
                          created_at=old["created_at"], updated_at=edited_at)
        self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "approved")
        self.assertEqual(self.state()["github_decisions"], [approval])
        refusal_body = self.remote_reply(batch, "Reject $z$.", id=6202)
        self.control("tick")
        events = self.finish(self.turn([
            call_chunks("refused-published", "write_file", {"path": "main.tex", "content": changed}),
            [base.say("The estimate uses $z$.")]], human_text="Continue the current estimate.", mode="edit"))
        self.assertEqual((self.root / "main.tex").read_text(), original)
        self.assertNotIn("$z$", "".join(event.get("text", "") for event in events if event["t"] == "delta"))
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "rejected")
        self.assertEqual([row["canonical"] for row in state["rejections"]], ["z"])
        self.assertEqual(state["decisions"][0], approval_receipt)
        self.assertEqual(len(state["decisions"]), 2)
        self.assertEqual(state["github_decisions"][-1]["human_text"], refusal_body)
        self.assertEqual(state["github_decisions"][-1]["comment_id"], 6202)
        self.assertEqual(state["github_decisions"][-1]["proposal_id"], proposals[0]["id"])
        decisions = state["decisions"]
        self.control("tick"); self.control("tick")
        self.stop(); self.start(); self.open_page(); self.control("tick")
        self.assertEqual(self.state()["decisions"], decisions)
        events = self.finish(self.turn([
            call_chunks("refused-after-restart", "write_file", {"path": "main.tex", "content": changed}),
            [base.say("The estimate uses $z$.")]], human_text="Continue the current estimate.", mode="edit"))
        self.assertEqual((self.root / "main.tex").read_text(), original)
        self.assertNotIn("$z$", "".join(event.get("text", "") for event in events if event["t"] == "delta"))
        self.remote_reply(batch, "Yes", id=6203)
        self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "rejected")
        self.assertEqual(self.state()["decisions"], decisions)
        self.remote_reply(batch, "Reopen $z$.", id=6204)
        self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "pending")
        events = self.finish(self.turn([
            call_chunks("reopened-published", "write_file", {"path": "main.tex", "content": changed}),
            [base.say("The estimate uses $z$.")]], human_text="Continue the current estimate.", mode="edit"))
        self.assertEqual((self.root / "main.tex").read_text(), original)
        self.assertNotIn("$z$", "".join(event.get("text", "") for event in events if event["t"] == "delta"))
        revised = self.propose(reason="The author requested a fresh review of the estimate.")
        self.assertGreater(revised[0]["revision"], proposals[0]["revision"])
        fresh = self.share(topic_id, revised)
        self.control("tick")
        self.remote_reply(batch, "Yes", id=6210)
        self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "pending")
        self.remote_reply(fresh, "Yes", id=6211)
        self.control("tick")
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "approved")
        self.assertEqual(state["decisions"][0], approval_receipt)
        self.assertEqual(state["github_decisions"][-1]["comment_id"], 6211)
        self.assertEqual(state["github_decisions"][-1]["revision"], revised[0]["revision"])
        self.use_saved_name()

    def test_historical_rejection_replay_cannot_withdraw_a_later_editor_approval(self):
        topic_id, _proposals, batch = self.publish()
        self.finish(self.turn([[base.say("The current author choice is saved.")]], human_text="Approve $z$."))
        marker = self.work / "fail-github-reply-ack"
        marker.touch()
        self.remote_reply(batch, "Reject $z$.", id=6252,
                          created_at=datetime.now(timezone.utc).isoformat())
        self.control("tick")
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "rejected")
        self.assertEqual(len(state["decisions"]), 2)
        journal = self.work / "state/research-github" / (self.key + ".json")
        progress = json.loads(journal.read_text())["repositories"]["1"]["topics"][topic_id]
        self.assertFalse(any(row.get("decisions") == [state["decisions"][-1]["id"]]
                             for row in progress.get("proposal_replies", {}).values()))
        self.close_page(); marker.unlink()
        original = (self.root / "main.tex").read_text()
        changed = original.replace(r"$\ell+\ell_+$", "$z$")
        events = self.finish(self.turn([
            call_chunks("reopen-before-replay", "write_file", {"path": "main.tex", "content": changed})],
            human_text="Reopen $z$.", mode="edit"))
        self.assertEqual((self.root / "main.tex").read_text(), original)
        self.assertEqual(self.state()["proposals"][0]["status"], "pending")
        events = self.finish(self.turn([call_chunks("review-before-replay", "propose_notation", guard.PROPOSAL)]))
        session = next(event["session_id"] for event in events if event["t"] == "init")
        self.assertEqual(self.state()["proposals"][0]["revision"], _proposals[0]["revision"])
        self.finish(self.turn([[base.say("The current author choice is saved.")]], human_text="Yes", session=session))
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "approved")
        receipts = state["decisions"]
        self.assertEqual(len(receipts), 4)
        self.open_page(); self.control("tick"); self.control("tick")
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "approved")
        self.assertEqual(state["decisions"], receipts)
        self.stop(); self.start(); self.open_page(); self.control("tick")
        self.assertEqual(self.state()["decisions"], receipts)
        self.use_saved_name()

    def test_configured_login_keeps_its_pinned_account_id_for_later_batches(self):
        topic_id, _proposals, first = self.publish()
        self.remote_reply(first, "Yes, I approve.", id=6301,
            user={"id": 999, "login": "author", "type": "User"})
        self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "pending")
        self.assertEqual(self.state()["github_decisions"], [])
        lookups = [call for call in self.remote()["calls"] if call["method"] == "user"]
        self.assertEqual(len(lookups), 1)
        self.control("user", login="author", row={"id": 999, "login": "author", "type": "User",
            "html_url": "https://github.com/author"})
        second_proposals = self.propose(("w",))
        second = self.share(topic_id, second_proposals)
        self.control("tick")
        self.remote_reply(second, "Yes, I approve.", id=6303,
            user={"id": 999, "login": "author", "type": "User"})
        requests = len(self.requests())
        self.control("tick")
        state = self.state()
        self.assertEqual({row["canonical"]: row["status"] for row in state["proposals"]},
                         {"z": "pending", "w": "pending"})
        self.assertEqual(state["github_decisions"], [])
        self.assertEqual([call for call in self.remote()["calls"] if call["method"] == "user"], lookups)
        self.assertEqual(len(self.remote()["issues"]), 1)
        self.assertEqual(len(self.requests()), requests)

    def test_untrusted_accounts_bots_quotes_and_old_comments_cannot_approve(self):
        _topic, _proposals, batch = self.publish()
        before = (self.root / "main.tex").read_bytes()
        requests = len(self.requests())
        author = {"id": 42, "login": "author", "type": "User"}
        cases = [
            ("Yes, I approve.", {"id": 88, "login": "visitor", "type": "User"}, {}),
            ("Yes, I approve.", {**author, "type": "Bot"}, {}),
            ("Yes, I approve.", {**author, "type": "Unknown"}, {}),
            ("> Yes, I approve.", author, {}),
            ('The paper says "Yes, I approve."', author, {}),
            ("The author approved $z$.", author, {}),
            ("Maybe approve $z$.", author, {}),
            ("Yes, I approve.", author, {"created_at": "2026-10-28T00:00:00Z"}),
        ]
        for index, (text, user, options) in enumerate(cases):
            with self.subTest(text=text, user=user):
                self.remote_reply(batch, text, user=user, id=6500 + index, **options)
                self.control("tick")
                state = self.state()
                self.assertEqual(state["proposals"][0]["status"], "pending")
                self.assertEqual(state["github_decisions"], [])
                self.assertNotIn("z", {row["canonical"] for row in state["symbols"]})
                self.assertEqual((self.root / "main.tex").read_bytes(), before)
                self.assertEqual(len(self.requests()), requests)
        self.remote_reply(batch, "Yes, I approve.", marker=False, id=6510)
        self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "approved")
        self.assertEqual(len(self.state()["github_decisions"]), 1)

    def test_partial_batch_and_clear_replacement_apply_only_named_subjects(self):
        _topic, _proposals, batch = self.publish(("z", "w", "v"))
        requests = len(self.requests())
        self.remote_reply(batch, "Approve $z$; maybe approve $w$; use $q$ instead of $v$.", id=6600)
        self.control("tick")
        state = self.state()
        self.assertEqual({row["canonical"]: row["status"] for row in state["proposals"]},
                         {"z": "approved", "w": "pending", "v": "rejected"})
        self.assertTrue({"z", "q"} <= {row["canonical"] for row in state["symbols"]})
        self.assertFalse({"w", "v"} & {row["canonical"] for row in state["symbols"]})
        self.assertTrue(state["github_discussion"]["proposal_batches"][0]["clarifications"])
        self.assertEqual(len(self.requests()), requests)
        self.use_saved_name("q")

    def test_designated_quoted_comments_cannot_approve_but_ordinary_reply_can(self):
        _topic, _proposals, batch = self.publish()
        before = (self.root / "main.tex").read_bytes()
        requests = len(self.requests())
        messages = ("~~~text\nApprove $z$.\n~~~", "    Yes",
                    "> Quoted instruction:\nApprove $z$.", "<pre>\nApprove $z$.\n</pre>")
        for index, human in enumerate(messages):
            with self.subTest(human=human):
                body = self.remote_reply(batch, human, id=6650 + index)
                self.control("tick")
                state = self.state()
                self.assertEqual(state["proposals"][0]["status"], "pending")
                self.assertEqual(state["github_decisions"], [])
                self.assertNotIn("z", {row["canonical"] for row in state["symbols"]})
                self.assertEqual((self.root / "main.tex").read_bytes(), before)
                self.assertEqual(len(self.requests()), requests)
                remote = self.remote()["comments"]["1"]
                self.assertEqual(next(row for row in remote if row["id"] == 6650 + index)["body"], body)
        ordinary = self.remote_reply(batch, "Yes", id=6659)
        self.control("tick")
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "approved")
        self.assertEqual([row["human_text"] for row in state["github_decisions"]], [ordinary])
        self.use_saved_name()

    def test_failed_registration_stays_pending_and_retries_without_a_model_call(self):
        _topic, _proposals, batch = self.publish()
        self.remote_reply(batch, "Yes, I approve.", id=6700)
        saved = self.root / "research/knowledge.json"
        before = saved.read_bytes()
        requests = len(self.requests())
        marker = self.work / "fail-persistence"
        marker.touch(); self.control("tick")
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "pending")
        self.assertEqual(state["github_decisions"], [])
        self.assertEqual(saved.read_bytes(), before)
        self.assertEqual(len(self.requests()), requests)
        marker.unlink(); self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "approved")
        self.assertEqual(len(self.state()["github_decisions"]), 1)
        self.assertEqual(len(self.requests()), requests)

    def test_failed_journal_acknowledgement_replays_the_same_saved_decision_once(self):
        topic_id, _proposals, batch = self.publish()
        self.remote_reply(batch, "Yes, I approve.", id=6800)
        marker = self.work / "fail-github-reply-ack"
        marker.touch(); self.control("tick")
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "approved")
        self.assertEqual(len(state["github_decisions"]), 1)
        decision_id = state["github_decisions"][0]["id"]
        saved = self.root / "research/knowledge.json"
        receipts = json.loads(saved.read_text())["decisions"]
        journal = self.work / "state/research-github" / (self.key + ".json")
        progress = json.loads(journal.read_text())
        self.assertEqual(progress["repositories"]["1"]["topics"][topic_id].get("proposal_replies", {}), {})
        requests = len(self.requests())
        self.stop(); marker.unlink(); self.start(); self.open_page(); self.control("tick")
        state = self.state()
        self.assertEqual([row["id"] for row in state["github_decisions"]], [decision_id])
        self.assertEqual(json.loads(saved.read_text())["decisions"], receipts)
        progress = json.loads(journal.read_text())
        self.assertEqual(len(progress["repositories"]["1"]["topics"][topic_id]["proposal_replies"]), 1)
        self.assertEqual(len(self.requests()), requests)
        before = [(path.stat().st_mtime_ns, path.read_bytes()) for path in (saved, journal)]
        self.control("tick"); self.control("tick")
        self.assertEqual([(path.stat().st_mtime_ns, path.read_bytes()) for path in (saved, journal)], before)
        self.assertEqual(len(self.state()["github_decisions"]), 1)

    def test_ambiguous_reply_stays_frozen_after_another_batch_becomes_stale(self):
        topic_id, _proposals, first = self.publish()
        second = self.share(topic_id, self.propose(("w",)))
        self.control("tick")
        self.remote_reply(second, "Yes", marker=False, id=6900)
        self.control("tick")
        self.assertTrue(all(row["status"] == "pending" for row in self.state()["proposals"]))
        self.assertEqual(self.state()["github_decisions"], [])
        self.propose(("z",), definition="A revised current definition of the sum.")
        self.control("tick"); self.control("tick")
        state = self.state()
        self.assertEqual({row["canonical"]: row["status"] for row in state["proposals"]},
                         {"z": "pending", "w": "pending"})
        self.assertEqual(state["github_decisions"], [])
        self.assertEqual(next(row for row in state["github_discussion"]["proposal_batches"]
                              if row["id"] == first["id"])["publication"], "stale")

    def test_editing_an_old_comment_cannot_turn_it_into_a_new_batch_reply(self):
        self.initialize(); self.configure(); self.open_page()
        topic_id = self.topic(); self.control("tick")
        proposals = self.propose()
        batch = self.share(topic_id, proposals)
        self.remote_reply(batch, "Maybe.", id=7000, created_at="2026-10-28T00:00:00Z")
        self.control("tick")
        self.remote_reply(batch, "Yes, I approve.", id=7000, created_at="2026-10-28T00:00:00Z",
                          updated_at="2026-10-30T00:00:00Z")
        self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "pending")
        self.assertEqual(self.state()["github_decisions"], [])
        self.remote_reply(batch, "Yes, I approve.", id=7010)
        self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "approved")
        self.assertEqual(len(self.state()["github_decisions"]), 1)

    def test_stale_proposal_version_needs_a_new_explicit_batch_before_approval(self):
        topic_id, original, batch = self.publish()
        revised = self.propose(("z",), definition="A revised current definition of the sum.")
        self.assertGreater(revised[0]["revision"], original[0]["revision"])
        self.remote_reply(batch, "Yes, I approve.", id=7100)
        self.control("tick")
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "pending")
        self.assertEqual(state["github_decisions"], [])
        self.assertEqual(state["github_discussion"]["proposal_batches"][0]["publication"], "stale")
        new_batch = self.share(topic_id, revised)
        self.control("tick")
        self.remote_reply(new_batch, "Yes, I approve.", id=7110)
        self.control("tick")
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "approved")
        self.assertEqual(state["github_decisions"][0]["revision"], revised[0]["revision"])

    def test_comment_edits_repeated_checks_reversed_fetch_and_restart_keep_one_decision(self):
        _topic, _proposals, batch = self.publish()
        self.remote_reply(batch, "Maybe.", id=7200)
        old = next(row for row in self.remote()["comments"]["1"] if row["id"] == 7200)
        self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "pending")
        updated = (datetime.fromisoformat(old["updated_at"].replace("Z", "+00:00"))
                   + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        body = self.remote_reply(batch, "Yes, I approve.", id=7200,
            created_at=old["created_at"], updated_at=updated)
        self.control("tick"); self.control("tick")
        state = self.state()
        self.assertEqual(state["proposals"][0]["status"], "approved")
        self.assertEqual(len(state["github_decisions"]), 1)
        decision = state["github_decisions"][0]
        self.assertEqual(decision["human_text"], body)
        self.assertEqual(decision["updated_at"], updated)
        self.control("override", method="list_comments", value=list(reversed(self.remote()["comments"]["1"])))
        saved = self.root / "research/knowledge.json"
        journal = self.work / "state/research-github" / (self.key + ".json")
        before = [(path.stat().st_mtime_ns, path.read_bytes()) for path in (saved, journal)]
        requests = len(self.requests())
        self.control("tick"); self.control("tick")
        self.assertEqual([(path.stat().st_mtime_ns, path.read_bytes()) for path in (saved, journal)], before)
        self.stop(); self.start(); self.open_page(); self.control("tick")
        self.assertEqual(self.state()["github_decisions"], [decision])
        self.assertEqual(len(self.requests()), requests)

    def test_last_page_close_pauses_approval_and_reopen_catches_up_without_duplicates(self):
        _topic, _proposals, batch = self.publish()
        self.close_page()
        self.remote_reply(batch, "Yes, I approve.", id=7300)
        saved = self.root / "research/knowledge.json"
        before = saved.stat().st_mtime_ns, saved.read_bytes()
        calls = self.remote()["calls"]
        requests = len(self.requests())
        self.control("tick"); self.control("tick")
        self.assertEqual(self.remote()["calls"], calls)
        self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), before)
        self.assertEqual(self.state()["proposals"][0]["status"], "pending")
        self.open_page(); self.control("tick"); self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "approved")
        self.assertEqual(len(self.state()["github_decisions"]), 1)
        before = saved.stat().st_mtime_ns, saved.read_bytes()
        self.control("tick"); self.control("tick")
        self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), before)
        self.assertEqual(len(self.state()["github_decisions"]), 1)
        self.assertEqual(len(self.requests()), requests)

    def test_late_reply_fetch_cannot_commit_after_binding_change_or_last_page_close(self):
        _topic, _proposals, batch = self.publish()
        self.remote_reply(batch, "Yes, I approve.", id=7400)
        saved = self.root / "research/knowledge.json"
        before = saved.stat().st_mtime_ns, saved.read_bytes()
        for boundary in ("repository", "presence"):
            with self.subTest(boundary=boundary):
                errors = []
                self.control("block", method="list_comments")
                def tick():
                    try:
                        self.control("tick")
                    except BaseException as error:
                        errors.append(error)
                thread = threading.Thread(target=tick)
                thread.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not self.remote()["blocked"]:
                    time.sleep(.01)
                self.assertTrue(self.remote()["blocked"])
                if boundary == "repository":
                    self.control("target", url="https://github.com/fixture/other.git")
                else:
                    self.close_page()
                self.control("release")
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), before)
                self.assertEqual(self.state()["proposals"][0]["status"], "pending")
                self.assertEqual(self.state()["github_decisions"], [])
                if boundary == "repository":
                    self.control("target", url="https://github.com/fixture/paper.git")
        self.open_page(); self.control("tick")
        self.assertEqual(self.state()["proposals"][0]["status"], "approved")

    def test_other_project_batch_and_wrong_issue_reply_cannot_approve_local_subjects(self):
        topic_id, proposals, batch = self.publish()
        other = type(self)("runTest")
        other.work = self.work / "other"
        other.work.mkdir()
        other.process = None
        try:
            other.start(state_dir=self.work / "state", remote_file=self.work / "github-remote.json")
            other.initialize(); other.configure(); other.open_page()
            other_topic = other.topic(); other.control("tick")
            other_proposals = other.propose(("w",))
            other_batch = other.share(other_topic, other_proposals)
            other.control("tick")
            self.assertNotEqual(self.key, other.key)
            code, _result = self.request("/api/research/github/proposals", {
                "project_key": other.key, "topic_id": topic_id,
                "subjects": [{key: proposals[0][key] for key in ("id", "revision", "content_hash")}]})
            self.assertEqual(code, 409)
            requests = len(self.requests()), len(other.requests())
            self.remote_reply(other_batch, "Yes, I approve.", id=7500, issue=1)
            self.remote_reply(batch, "Yes, I approve.", id=7501, issue=2)
            self.control("tick"); other.control("tick")
            self.assertEqual(self.state()["proposals"][0]["status"], "pending")
            self.assertEqual(other.state()["proposals"][0]["status"], "pending")
            self.assertEqual(self.state()["github_decisions"], [])
            self.assertEqual(other.state()["github_decisions"], [])
            self.remote_reply(batch, "Yes, I approve.", id=7510, issue=1)
            self.control("tick"); other.control("tick")
            self.assertEqual(self.state()["proposals"][0]["status"], "approved")
            self.assertEqual(other.state()["proposals"][0]["status"], "pending")
            self.assertEqual(other.state()["github_decisions"], [])
            self.assertEqual((len(self.requests()), len(other.requests())), requests)
            self.assertEqual(len(self.remote()["issues"]), 2)
        finally:
            other.stop()

    def test_unknown_candidate_publication_reconciles_before_its_human_reply_applies(self):
        self.initialize(); self.configure(); self.open_page()
        topic_id = self.topic(); self.control("tick")
        proposals = self.propose()
        batch = self.share(topic_id, proposals)
        self.control("unknown", method="create_comment")
        self.control("tick")
        self.assertEqual(self.state()["github_discussion"]["proposal_batches"][0]["publication"], "unknown")
        self.assertEqual(self.state()["proposals"][0]["status"], "pending")
        self.assertEqual(len(self.remote()["comments"]["1"]), 1)
        self.remote_reply(batch, "Yes, I approve.", id=7600)
        requests = len(self.requests())
        self.stop(); self.start(); self.open_page(); self.control("tick")
        state = self.state()
        self.assertEqual(state["github_discussion"]["proposal_batches"][0]["publication"], "published")
        self.assertEqual(state["proposals"][0]["status"], "approved")
        self.assertEqual(len(state["github_decisions"]), 1)
        self.assertEqual(sum(row["method"] == "create_comment" for row in self.remote()["calls"]), 1)
        self.assertEqual(len(self.requests()), requests)


if __name__ == "__main__":
    unittest.main()
