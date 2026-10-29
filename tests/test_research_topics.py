"""Shared topic records through initialized project knowledge; no model calls."""
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "prism_local"),
               str(Path(__file__).resolve().parent)]
from backends import Job
from errors import Refused
from research import RECORD, ResearchKnowledge
from research_access import NotationStop, ResearchAccess
from test_research import project_with
from tmpdirs import tmpdir
import research_topics


class TopicRecords(unittest.TestCase):
    def setUp(self):
        self.state = tmpdir()
        self.environment = mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.project = project_with({"main.tex": "$x + \\ell$\n"})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()
        self.access = ResearchAccess(self.knowledge)
        self.changes = []
        self.project.on_change = lambda: self.changes.append(True)

    def job(self, number=1, human_text="Maybe try the direct estimate."):
        job = Job(number)
        job.provider = "fixture"
        job.root = self.project.root
        job.human_text = human_text
        return job

    def draft(self, text="Try the direct estimate.", **kwargs):
        return {"title": "Direct estimate", "entries": [
            {"kind": "idea", "text": text, "source": "human",
             "quote": "Maybe try the direct estimate."}], **kwargs}

    def published(self):
        return self.knowledge.status()["discussion"].get("topics", [])

    def test_a_draft_is_materialized_only_after_success_and_keeps_its_source(self):
        job = self.job()
        queued = research_topics.stage(self.access, job, self.draft())
        self.assertTrue(queued["queued"])
        self.assertEqual(self.published(), [])
        self.assertFalse(list((self.project.root / "research").rglob("*.md")))
        research_topics.finish(self.access, job, success=True, session_id="conversation-one")
        topic, = self.published()
        text = (self.project.root / topic["path"]).read_text()
        self.assertIn("## Ideas", text)
        self.assertIn("Try the direct estimate.", text)
        self.assertIn("Maybe try the direct estimate.", text)
        self.assertNotIn("Try the direct estimate.",
                         text.split("## Confirmed decisions", 1)[1].split("## Open questions", 1)[0])
        self.assertEqual(topic["entries"][0]["provenance"]["conversation"], "conversation-one")
        self.assertEqual([e["t"] for e in job.events], ["research_record"])
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)
        self.assertTrue(self.changes)

    def test_repeated_content_across_conversations_does_not_write_or_notify(self):
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        research_topics.finish(self.access, job, success=True, session_id="first")
        topic, = self.published()
        paths = [self.project.root / RECORD, self.project.root / topic["path"]]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        next_job = self.job(2)
        next_job.session_id = "another-conversation"
        self.changes.clear()
        with mock.patch("research_topics.replace_file", wraps=research_topics.replace_file) as writes:
            queued = research_topics.stage(ResearchAccess(self.knowledge), next_job, self.draft())
            research_topics.finish(self.access, next_job, success=True)
        self.assertFalse(queued["changed"])
        self.assertFalse(queued["queued"])
        writes.assert_not_called()
        self.assertEqual(next_job.events, [])
        self.assertEqual(self.changes, [])
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

    def test_ready_markdown_failure_recovers_after_restart_without_another_draft(self):
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        replace = research_topics.replace_file
        def fail_markdown(path, raw, **options):
            if path.suffix == ".md":
                raise OSError("controlled disk full")
            return replace(path, raw, **options)
        with mock.patch("research_topics.replace_file", side_effect=fail_markdown):
            research_topics.finish(self.access, job, success=True, session_id="first")
        self.assertEqual([event["t"] for event in job.events], ["research_record_error"])
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 1)
        self.assertEqual(self.published(), [])
        reopened = ResearchKnowledge(type(self.project)(self.project.root))
        research_topics.retry(reopened)
        topic, = reopened.status()["discussion"]["topics"]
        self.assertIn("Try the direct estimate.", (self.project.root / topic["path"]).read_text())
        self.assertEqual(research_topics.status(reopened)["pending_count"], 0)
        self.assertEqual(len(list((self.project.root / "research/topics").glob("*.md"))), 1)

    def test_final_index_failure_recovers_without_rewriting_the_markdown(self):
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        with mock.patch.object(self.knowledge, "_write", side_effect=OSError("controlled index failure")):
            research_topics.finish(self.access, job, success=True)
        self.assertEqual(self.published(), [])
        path, = list((self.project.root / "research/topics").glob("*.md"))
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual([event["t"] for event in job.events], ["research_record_error"])
        reopened = ResearchKnowledge(type(self.project)(self.project.root))
        research_topics.retry(reopened)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        topic, = reopened.status()["discussion"]["topics"]
        self.assertEqual(topic["path"], path.relative_to(self.project.root).as_posix())
        self.assertEqual(research_topics.status(reopened)["pending_count"], 0)

    def test_sync_notification_runs_after_releasing_the_project_file_lock(self):
        acquired = []
        def notify():
            def other_thread():
                held = self.project.files_lock.acquire(blocking=False)
                acquired.append(held)
                if held:
                    self.project.files_lock.release()
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join(1)
        self.project.on_change = notify
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        research_topics.finish(self.access, job, success=True)
        self.assertEqual(acquired, [True])

    def test_confirmed_text_cannot_invent_a_different_conclusion_from_a_human_quote(self):
        job = self.job(human_text="We decided to compare the direct and iterative estimates.")
        data = {"title": "Direct estimate", "entries": [{"kind": "decision",
            "text": "The direct estimate is proved optimal.", "source": "human", "quote": job.human_text}]}
        with self.assertRaises(Refused):
            research_topics.stage(self.access, job, data)
        self.assertEqual(self.published(), [])
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)

    def test_a_later_failed_turn_cannot_discard_an_older_ready_update(self):
        first = self.job()
        research_topics.stage(self.access, first, self.draft())
        replace = research_topics.replace_file
        def fail_markdown(path, raw, **options):
            if path.suffix == ".md":
                raise OSError("controlled disk full")
            return replace(path, raw, **options)
        with mock.patch("research_topics.replace_file", side_effect=fail_markdown):
            research_topics.finish(self.access, first, success=True)
        second = self.job(2)
        with self.assertRaisesRegex(Refused, "pending"):
            research_topics.stage(self.access, second, self.draft("Another tentative route."))
        research_topics.finish(self.access, second, success=False)
        research_topics.retry(self.knowledge)
        topic, = self.published()
        self.assertIn("Try the direct estimate.", (self.project.root / topic["path"]).read_text())
        self.assertNotIn("Another tentative route.", (self.project.root / topic["path"]).read_text())

    def test_a_signed_statement_cannot_be_moved_to_another_topic_in_the_index(self):
        first = self.job()
        research_topics.stage(self.access, first, self.draft())
        research_topics.finish(self.access, first, success=True)
        trusted = research_topics.status(self.knowledge)
        path = self.project.root / RECORD
        state = json.loads(path.read_text())
        topic = state["discussion"]["topics"][0]
        topic["id"] = "a" * 24
        topic["path"] = f"research/topics/{topic['id']}.md"
        path.write_text(json.dumps(state))
        raw = path.read_bytes()
        self.assertEqual(research_topics.status(self.knowledge), trusted)
        self.assertEqual(path.read_bytes(), raw)
        self.assertFalse((self.project.root / topic["path"]).exists())

    def test_idle_retry_retains_a_now_withheld_draft_without_breaking_status(self):
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        replace = research_topics.replace_file
        def fail_markdown(path, raw, **options):
            if path.suffix == ".md":
                raise OSError("controlled disk full")
            return replace(path, raw, **options)
        with mock.patch("research_topics.replace_file", side_effect=fail_markdown):
            research_topics.finish(self.access, job, success=True)
        with mock.patch.object(ResearchAccess, "review_output", side_effect=NotationStop(error="withheld")):
            research_topics.retry(self.knowledge)
        state = research_topics.status(self.knowledge)
        self.assertEqual(state["pending_count"], 1)
        self.assertTrue(state["errors"])
        self.assertEqual(self.published(), [])
        self.assertFalse(list((self.project.root / "research").rglob("*.md")))

    def test_an_interrupted_unready_draft_needs_a_successful_reissued_turn(self):
        first = self.job()
        research_topics.stage(self.access, first, self.draft())
        reopened = ResearchKnowledge(type(self.project)(self.project.root))
        research_topics.retry(reopened)
        self.assertEqual(research_topics.status(reopened)["pending_count"], 1)
        self.assertFalse(list((self.project.root / "research").rglob("*.md")))
        second = self.job(2)
        access = ResearchAccess(reopened)
        research_topics.stage(access, second, self.draft())
        research_topics.finish(access, second, success=True)
        self.assertEqual(research_topics.status(reopened)["pending_count"], 0)
        self.assertEqual(len(self.published()), 1)

    def test_failed_or_cancelled_drafts_do_not_publish_on_idle_retry(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                job = self.job()
                research_topics.stage(self.access, job, self.draft())
                if cancelled:
                    job.cancel.set()
                research_topics.finish(self.access, job, success=cancelled)
                research_topics.retry(self.knowledge)
                self.assertEqual(self.published(), [])
                self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)
                self.assertEqual(job.events, [])

    def test_manual_or_remote_markdown_changes_are_retained_as_a_conflict(self):
        first = self.job()
        research_topics.stage(self.access, first, self.draft())
        research_topics.finish(self.access, first, success=True)
        topic, = self.published()
        path = self.project.root / topic["path"]
        path.write_text("A human or remote update.\n")
        before = path.read_bytes(), path.stat().st_mtime_ns
        second = self.job(2)
        research_topics.stage(self.access, second, self.draft("Try a second route."))
        research_topics.finish(self.access, second, success=True)
        research_topics.retry(self.knowledge)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual([event["t"] for event in second.events], ["research_record_error"])
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 1)
        self.assertEqual(self.published()[0]["revision"], 1)

    def test_linked_topic_folder_cannot_write_an_external_file(self):
        outside = tmpdir()
        folder = self.project.root / "research/topics"
        try:
            folder.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("needs symlink capability")
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        research_topics.finish(self.access, job, success=True)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.published(), [])
        self.assertEqual([event["t"] for event in job.events], ["research_record_error"])
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 1)

    def test_unknown_summary_notation_uses_the_shared_hold_without_a_record(self):
        job = self.job()
        data = {"title": "Direct estimate", "entries": [
            {"kind": "idea", "text": "Try $z=x+x$.", "source": "assistant"}]}
        with self.assertRaises(NotationStop):
            research_topics.stage(self.access, job, data)
        self.assertEqual(self.published(), [])
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)
        self.assertEqual(self.knowledge.status()["proposals"][0]["status"], "pending")
        self.assertFalse(list((self.project.root / "research").rglob("*.md")))

    def test_conditional_or_unresolved_author_statements_are_not_confirmed(self):
        statements = ("We will use the direct estimate if the test succeeds.",
                      "Use the direct estimate unless the check fails.",
                      "I confirm we have not decided yet.",
                      "我们决定如果测试通过再采用直接估计。")
        for statement in statements:
            with self.subTest(statement=statement):
                job = self.job(human_text=statement)
                args = {"title": "Direct estimate", "entries": [{"kind": "decision", "text": statement,
                        "source": "human", "quote": statement}]}
                with self.assertRaises(Refused):
                    research_topics.stage(self.access, job, args)
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)
        statement = "We decided to compare the direct and iterative estimates."
        job = self.job(human_text=statement)
        research_topics.stage(self.access, job, {"title": "Direct estimate", "entries": [
            {"kind": "decision", "text": statement, "source": "human", "quote": statement}]})
        research_topics.finish(self.access, job, success=True)
        self.assertIn(statement, (self.project.root / self.published()[0]["path"]).read_text())

    def test_stop_at_the_path_boundary_does_not_apply_or_later_retry_the_draft(self):
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        resolve = research_topics._path
        def stop_before_application(knowledge, draft):
            job.cancel.set()
            return resolve(knowledge, draft)
        with mock.patch("research_topics._path", side_effect=stop_before_application):
            research_topics.finish(self.access, job, success=True)
        research_topics.retry(self.knowledge)
        self.assertEqual(self.published(), [])
        self.assertFalse(list((self.project.root / "research").rglob("*.md")))
        self.assertEqual(job.events, [])
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)

    def test_interpolated_metadata_is_checked_before_document_application(self):
        job = self.job()
        job.provider = "fixture $z$"
        research_topics.stage(self.access, job, self.draft())
        with self.assertRaises(NotationStop):
            research_topics.finish(self.access, job, success=True)
        self.assertEqual(self.published(), [])
        self.assertFalse(list((self.project.root / "research").rglob("*.md")))
        self.assertEqual(job.events, [])

    def test_queue_housekeeping_failure_does_not_report_a_saved_record_as_unsaved(self):
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        write = research_topics._write_queue
        def fail_cleanup(knowledge, queue):
            if not queue["drafts"]:
                raise OSError("controlled cleanup failure")
            return write(knowledge, queue)
        with mock.patch("research_topics._write_queue", side_effect=fail_cleanup):
            research_topics.finish(self.access, job, success=True)
        self.assertEqual(len(self.published()), 1)
        self.assertEqual([event["t"] for event in job.events], ["research_record"])
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)
        before = (self.project.root / self.published()[0]["path"]).stat().st_mtime_ns
        research_topics.retry(self.knowledge)
        self.assertEqual((self.project.root / self.published()[0]["path"]).stat().st_mtime_ns, before)
        self.assertEqual([event["t"] for event in job.events], ["research_record"])

    def test_failed_ready_transition_still_completes_the_known_successful_turn(self):
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        with mock.patch("research_topics._write_queue", side_effect=OSError("controlled queue failure")):
            research_topics.finish(self.access, job, success=True, session_id="completed-conversation")
        self.assertEqual(len(self.published()), 1)
        self.assertEqual([event["t"] for event in job.events], ["research_record"])
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)
        before = (self.project.root / self.published()[0]["path"]).stat().st_mtime_ns
        research_topics.retry(self.knowledge)
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)
        self.assertEqual((self.project.root / self.published()[0]["path"]).stat().st_mtime_ns, before)
        self.assertEqual([event["t"] for event in job.events], ["research_record"])

    def test_stop_after_markdown_application_finishes_the_index_without_a_notice(self):
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        replace = research_topics.replace_file
        def stop_after_document(path, raw, **options):
            result = replace(path, raw, **options)
            if path.suffix == ".md":
                job.cancel.set()
            return result
        with mock.patch("research_topics.replace_file", side_effect=stop_after_document):
            research_topics.finish(self.access, job, success=True)
        self.assertEqual(len(self.published()), 1)
        self.assertEqual(job.events, [])
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)
        before = (self.project.root / self.published()[0]["path"]).stat().st_mtime_ns
        research_topics.retry(self.knowledge)
        self.assertEqual((self.project.root / self.published()[0]["path"]).stat().st_mtime_ns, before)

    def test_failure_of_both_eligibility_and_completion_requires_a_usable_reissue(self):
        first = self.job()
        research_topics.stage(self.access, first, self.draft())
        with mock.patch("research_topics._write_queue", side_effect=OSError("controlled queue failure")), \
                mock.patch.object(self.knowledge, "_write", side_effect=OSError("controlled index failure")):
            research_topics.finish(self.access, first, success=True, session_id="first-conversation")
        self.assertEqual(self.published(), [])
        self.assertEqual([event["t"] for event in first.events], ["research_record_error"])
        self.assertFalse(first.events[0]["retryable"])
        self.assertIn("Repeat", first.events[0]["message"])
        research_topics.retry(self.knowledge)
        self.assertEqual(self.published(), [], "unready state must not imply an abandoned Turn succeeded")
        self.assertIn(research_topics.REISSUE, research_topics.status(self.knowledge)["errors"])
        second = self.job(2)
        research_topics.stage(self.access, second, self.draft())
        research_topics.finish(self.access, second, success=True, session_id="second-conversation")
        self.assertEqual(len(self.published()), 1)
        self.assertEqual([event["t"] for event in second.events], ["research_record"])
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)

    def test_stop_during_shared_review_does_not_leave_an_eligible_draft(self):
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        self.access.cancel = job.cancel
        review = self.access.review_output
        def stop_in_review(text, **options):
            job.cancel.set()
            return review(text, **options)
        with mock.patch.object(self.access, "review_output", side_effect=stop_in_review):
            research_topics.finish(self.access, job, success=True)
        research_topics.retry(self.knowledge)
        self.assertEqual(self.published(), [])
        self.assertFalse(list((self.project.root / "research").rglob("*.md")))
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)

    def test_stop_during_eligibility_persistence_does_not_leave_an_eligible_draft(self):
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        write = research_topics._write_queue
        def stop_after_ready(knowledge, queue):
            result = write(knowledge, queue)
            if any(draft["ready"] for draft in queue["drafts"]):
                job.cancel.set()
            return result
        with mock.patch("research_topics._write_queue", side_effect=stop_after_ready):
            research_topics.finish(self.access, job, success=True)
        research_topics.retry(self.knowledge)
        self.assertEqual(self.published(), [])
        self.assertFalse(list((self.project.root / "research").rglob("*.md")))
        self.assertEqual(research_topics.status(self.knowledge)["pending_count"], 0)


if __name__ == "__main__":
    unittest.main()
