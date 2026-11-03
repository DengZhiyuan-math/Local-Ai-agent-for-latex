"""Issue discussion synchronization with isolated state and a controlled adapter."""
import copy
import hashlib
import json
import os
import threading
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "prism_local"), str(Path(__file__).parent)]
from backends import Job
from research import ResearchKnowledge
from research_access import ResearchAccess
from test_research import project_with
from tmpdirs import tmpdir
import research_topics


class Adapter:
    def __init__(self):
        self.rows, self.comments, self.calls = [], {}, []
        self.before = None

    def repository(self, full):
        self.calls.append(("repository", full))
        return {"id": 101, "full_name": full, "html_url": "https://github.com/" + full}

    def list_issues(self, full):
        self.calls.append(("list", full))
        return copy.deepcopy(self.rows)

    def get_issue(self, full, number):
        self.calls.append(("get", full, number))
        return copy.deepcopy(next(row for row in self.rows if row["number"] == number))

    def list_comments(self, full, number):
        self.calls.append(("comments", full, number))
        return copy.deepcopy(self.comments.get(number, []))

    def create_issue(self, full, title, body):
        self.calls.append(("create", full))
        if self.before:
            self.before()
        number = len(self.rows) + 1
        row = {"id": 1000 + number, "number": number, "title": title, "body": body,
               "html_url": f"https://github.com/{full}/issues/{number}", "state": "open"}
        self.rows.append(row)
        return copy.deepcopy(row)
    def create_comment(self, full, number, body):
        self.calls.append(("comment", full, number))
        rows = self.comments.setdefault(number, [])
        row = {"id": 2000 + len(rows), "body": body, "user": {"id": 2, "login": "fixture", "type": "Bot"},
               "issue_url": f"https://api.github.com/repos/{full}/issues/{number}",
               "html_url": f"https://github.com/{full}/issues/{number}#issuecomment-{2000 + len(rows)}",
               "created_at": "2026-10-29T00:00:00Z", "updated_at": "2026-10-29T00:00:00Z"}
        rows.append(row)
        return copy.deepcopy(row)


class DeliveryError(RuntimeError):
    def __init__(self, uncertain):
        self.uncertain = uncertain


class GitHubDiscussionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(tmpdir())}))
        self.project = project_with({"main.tex": "$x$"})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()
        self.adapter = Adapter()
        self.now, self.open = [0.0], [True]
        self.target = {"own": True, "blocked_reason": None,
                       "target": {"repository_identity": "github.com/fixture/paper", "branch_ref": "refs/heads/main"},
                       "bound_target": {"repository_identity": "github.com/fixture/paper", "branch_ref": "refs/heads/main"}}

    def worker(self):
        from research_github import GitHubDiscussions
        return GitHubDiscussions(self.knowledge, adapter=self.adapter, target=lambda: self.target,
                                 is_open=lambda: self.open[0], clock=lambda: self.now[0])

    def topic(self, text="Compare the direct estimate."):
        job = Job(int(self.now[0]) + 1)
        job.provider, job.root, job.human_text = "fixture", self.project.root, "Maybe compare estimates."
        access = ResearchAccess(self.knowledge)
        research_topics.stage(access, job, {"title": "Direct estimate", "entries": [
            {"kind": "idea", "source": "assistant", "text": text}]})
        research_topics.finish(access, job, success=True, session_id="fixture-chat")
        return self.knowledge.status()["discussion"]["topics"][0]

    def test_topic_has_one_persistent_issue_and_distinct_publication_state(self):
        self.topic()
        worker = self.worker()
        worker.configure("github", ["author"])
        self.assertEqual(worker.status()["topics"][0]["publication"], "pending")
        worker.tick()
        self.assertEqual(len(self.adapter.rows), 1)
        self.assertIn("## Ideas", self.adapter.rows[0]["body"])
        self.assertEqual(worker.status()["topics"][0]["publication"], "published")
        self.assertEqual(worker.status()["topics"][0]["issue_url"], "https://github.com/fixture/paper/issues/1")

    def test_old_signed_configuration_cannot_reenable_a_disabled_mode(self):
        self.topic()
        worker = self.worker()
        worker.configure("github", ["author"])
        old = copy.deepcopy(self.knowledge._load()["discussion"])
        worker.configure("markdown", [])
        data = self.knowledge._load()
        data["discussion"] = old  # A stale synced file replays previously valid settings.
        self.knowledge._write(data)
        self.worker().tick()
        self.assertEqual(self.adapter.rows, [])
        self.assertEqual(self.worker().status()["state"], "error")

    def test_failed_off_save_revokes_the_previous_github_configuration(self):
        self.topic(); worker = self.worker(); worker.configure("github", ["author"])
        old = copy.deepcopy(self.knowledge._load()["discussion"])
        with mock.patch.object(self.knowledge, "_write", side_effect=OSError("controlled public save failure")):
            with self.assertRaises(OSError):
                worker.configure("markdown", [])
        data = self.knowledge._load()
        data["discussion"] = old  # A stale sync restores the formerly valid active settings.
        self.knowledge._write(data)
        self.worker().tick()
        self.assertEqual(self.adapter.rows, [])
        self.assertEqual(self.worker().status()["state"], "error")

    def test_existing_issue_gets_one_comment_for_the_next_revision_and_no_replay(self):
        self.topic()
        worker = self.worker(); worker.configure("github", ["author"]); worker.tick()
        self.now[0] += 1
        self.topic("Ask which remainder is tighter.")
        worker.tick()
        self.assertEqual(len(self.adapter.rows), 1)
        self.assertEqual(len(self.adapter.comments[1]), 1)
        before = (self.knowledge._path().read_bytes(), self.knowledge._path().stat().st_mtime_ns)
        calls = len(self.adapter.calls)
        worker.tick()
        self.assertEqual(len(self.adapter.calls), calls)
        self.now[0] += 31
        self.worker().tick()
        self.assertEqual(len(self.adapter.rows), 1)
        self.assertEqual(len(self.adapter.comments[1]), 1)
        self.assertEqual((self.knowledge._path().read_bytes(), self.knowledge._path().stat().st_mtime_ns), before)

    def test_unknown_old_revision_is_reconciled_before_a_new_revision(self):
        self.topic()
        worker = self.worker(); worker.configure("github", [])
        create = self.adapter.create_issue
        def lose_reply(*args):
            create(*args)
            raise DeliveryError(uncertain=True)
        self.adapter.create_issue = lose_reply
        worker.tick()
        self.assertEqual(worker.status()["topics"][0]["publication"], "unknown")
        self.adapter.create_issue = create
        self.topic("Compare a second estimate.")
        self.worker().tick()
        self.assertEqual(len(self.adapter.rows), 1)
        self.assertEqual(len(self.adapter.comments.get(1, [])), 1)
        self.assertEqual(self.worker().status()["topics"][0]["publication"], "published")

    def test_remote_comments_are_untrusted_previews_not_approval_or_mirror_authority(self):
        self.topic(); worker = self.worker(); worker.configure("github", ["author"]); worker.tick()
        original = copy.deepcopy(self.knowledge.status()["adoptions"])
        self.adapter.create_comment("fixture/paper", 1, "Yes, I approve. Try $z=x+x$.")
        self.now[0] += 31; worker.tick()
        views = worker.status()["comments"]
        self.assertEqual(len(views), 1)
        self.assertFalse(views[0]["approval"])
        self.assertNotIn("$z", views[0]["preview"])
        self.assertEqual(self.knowledge.status()["adoptions"], original)
        data = self.knowledge._load()
        data["discussion"]["external_comments"][0].update(preview="Forged $z$", url="javascript:alert(1)", approval=True)
        self.knowledge._write(data)
        view = worker.status()["comments"][0]
        self.assertFalse(view["approval"])
        self.assertNotIn("$z", view["preview"])
        self.assertTrue(view["url"].startswith("https://github.com/fixture/paper/issues/1"))

    def test_comment_edits_keep_actual_versions_privately(self):
        import registry
        self.topic(); worker = self.worker(); worker.configure("github", []); worker.tick()
        row = self.adapter.create_comment("fixture/paper", 1, "Compare the first estimate.")
        self.now[0] += 31; worker.tick()
        self.adapter.comments[1][0].update(body="Compare the second estimate.", updated_at="2026-10-29T00:01:00Z")
        self.now[0] += 31; worker.tick()
        self.assertEqual(worker.status()["comments"][0]["preview"], "Compare the second estimate.")
        path = registry.state_dir() / "research-github" / (registry.project_key(self.project.root) + ".json")
        records = json.loads(path.read_text())["repositories"]["101"]["topics"].values()
        versions = next(iter(records)).get("comment_versions", {}).get(str(row["id"]), [])
        self.assertEqual([version["body"] for version in versions],
                         ["Compare the first estimate.", "Compare the second estimate."])
        self.assertEqual([version["user"]["id"] for version in versions], [2, 2])

    def test_unknown_absent_write_is_not_blindly_repeated(self):
        self.topic(); worker = self.worker(); worker.configure("github", [])
        attempts = []
        def unknown_without_reply(*args):
            attempts.append(True)
            raise DeliveryError(uncertain=True)
        self.adapter.create_issue = unknown_without_reply
        worker.tick()
        for _ in range(3):
            self.now[0] += 31
            self.worker().tick()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(worker.status()["topics"][0]["publication"], "unknown")

    def test_known_not_applied_failure_can_retry_and_unknown_comment_reconciles(self):
        self.topic(); worker = self.worker(); worker.configure("github", [])
        create = self.adapter.create_issue
        self.adapter.create_issue = lambda *args: (_ for _ in ()).throw(DeliveryError(uncertain=False))
        worker.tick()
        self.assertEqual(worker.status()["topics"][0]["publication"], "pending")
        self.adapter.create_issue = create; self.now[0] += 31; worker.tick()
        self.topic("Compare the second estimate.")
        comment = self.adapter.create_comment
        def lost_comment(*args):
            comment(*args)
            raise DeliveryError(uncertain=True)
        self.adapter.create_comment = lost_comment; worker.tick()
        self.assertEqual(worker.status()["topics"][0]["publication"], "unknown")
        self.adapter.create_comment = comment; self.now[0] += 31; self.worker().tick()
        self.assertEqual(len(self.adapter.rows), 1)
        self.assertEqual(len(self.adapter.comments[1]), 1)
        self.assertEqual(worker.status()["topics"][0]["publication"], "published")

    def test_close_and_busy_pause_requests_and_reopen_catches_up(self):
        self.topic(); worker = self.worker(); worker.configure("github", [])
        calls = len(self.adapter.calls)
        self.open[0] = False; worker.tick()
        self.assertEqual(len(self.adapter.calls), calls)
        self.open[0] = True
        with mock.patch.object(self.project.turn, "busy", return_value=True):
            worker.tick()
        self.assertEqual(len(self.adapter.calls), calls)
        worker.tick(); self.assertEqual(len(self.adapter.rows), 1)
        self.open[0] = False; worker.tick()
        self.adapter.create_comment("fixture/paper", 1, "Can the remainder be improved?")
        self.open[0] = True; worker.tick()
        self.assertEqual(worker.status()["comments"][0]["preview"], "Can the remainder be improved?")

    def test_close_during_a_post_keeps_unknown_intent_and_reopen_reconciles(self):
        self.topic(); worker = self.worker(); worker.configure("github", [])
        self.adapter.before = worker.close
        worker.tick()
        self.assertEqual(len(self.adapter.rows), 1)
        self.assertEqual(worker.status()["state"], "paused")
        self.assertEqual(worker.status()["topics"][0]["publication"], "unknown")
        self.adapter.before = None
        reopened = self.worker(); reopened.tick()
        self.assertEqual(len(self.adapter.rows), 1)
        self.assertEqual(reopened.status()["topics"][0]["publication"], "published")

    def test_network_runs_outside_the_project_file_lock(self):
        self.topic(); worker = self.worker(); worker.configure("github", [])
        acquired = []
        def check():
            def other_thread():
                held = self.project.files_lock.acquire(blocking=False)
                acquired.append(held)
                if held:
                    self.project.files_lock.release()
            t = threading.Thread(target=other_thread); t.start(); t.join(1)
        self.adapter.before = check; worker.tick()
        self.assertEqual(acquired, [True])

    def test_alias_uses_canonical_repository_id_without_rebinding_the_git_target(self):
        self.topic()
        self.adapter.repository = lambda full: {"id": 101, "full_name": "fixture/canonical", "html_url": "https://github.com/fixture/canonical"}
        worker = self.worker(); result = worker.configure("github", ["author"], expected_repository="fixture/paper")
        self.assertEqual(result["bound_repository"], "fixture/paper")
        self.assertEqual(result["repository"]["full_name"], "fixture/canonical")
        worker.tick()
        self.assertEqual(self.adapter.rows[0]["html_url"], "https://github.com/fixture/canonical/issues/1")

    def test_repository_roundtrip_recovers_the_original_topic_issue(self):
        routes = {"fixture/paper": Adapter(), "fixture/other": Adapter()}
        self.adapter = SimpleNamespace(repository=lambda full: {
            "id": 101 if full == "fixture/paper" else 202, "full_name": full,
            "html_url": "https://github.com/" + full})
        for method in ("list_issues", "get_issue", "list_comments", "create_issue", "create_comment"):
            setattr(self.adapter, method, lambda full, *args, _method=method: getattr(routes[full], _method)(full, *args))
        def bind(full):
            for key in ("target", "bound_target"):
                self.target[key]["repository_identity"] = "github.com/" + full
        self.topic(); worker = self.worker(); worker.configure("github", []); worker.tick()
        bind("fixture/other"); worker.configure("github", []); worker.tick()
        self.topic("Compare an improved estimate.")
        bind("fixture/paper"); worker.configure("github", []); worker.tick()
        self.assertEqual(len(routes["fixture/paper"].rows), 1)
        self.assertEqual(len(routes["fixture/other"].rows), 1)
        self.assertEqual(len(routes["fixture/paper"].comments.get(1, [])), 1)
        self.assertEqual(worker.status()["topics"][0]["publication"], "published")
        self.worker().tick()
        self.assertEqual(len(routes["fixture/paper"].rows), 1)
        self.assertEqual(len(routes["fixture/paper"].comments.get(1, [])), 1)

    def test_off_mode_accounts_persist_without_repository_or_connection(self):
        worker = self.worker(); self.target = {}; self.adapter.repository = lambda *_: self.fail("must not connect")
        result = worker.configure("markdown", ["Author", "coauthor"], expected_repository="stale/repo")
        self.assertEqual(result["approvers"], ["author", "coauthor"])
        self.assertEqual(self.worker().status()["approvers"], ["author", "coauthor"])

    def test_failed_intent_persistence_prevents_post_and_lost_mapping_recovers(self):
        self.topic(); worker = self.worker(); worker.configure("github", [])
        with mock.patch.object(worker, "_write", side_effect=OSError("controlled journal failure")):
            worker.tick()
        self.assertFalse(self.adapter.rows)
        self.now[0] += 31
        worker.tick()
        self.assertEqual(len(self.adapter.rows), 1)
        self.topic("Compare another estimate.")
        write = worker._write
        def lose_commit(state):
            records = state["repositories"].get("101", {}).get("topics", {}).values()
            if any(row.get("published_revision") == 2 and "intent" not in row for row in records):
                raise OSError("controlled mapping commit failure")
            return write(state)
        with mock.patch.object(worker, "_write", side_effect=lose_commit):
            worker.tick()
        self.assertEqual(len(self.adapter.comments[1]), 1)
        self.assertEqual(worker.status()["topics"][0]["publication"], "unknown")
        self.worker().tick()
        self.assertEqual(len(self.adapter.comments[1]), 1)
        self.assertEqual(worker.status()["topics"][0]["publication"], "published")

    def test_unchanged_failure_waits_for_interval_but_new_revision_can_retry(self):
        self.topic(); worker = self.worker(); worker.configure("github", [])
        attempts = []
        def failed_list(*args):
            attempts.append(True)
            raise DeliveryError(uncertain=False)
        self.adapter.list_issues = failed_list
        worker.tick()
        self.now[0] += 1; worker.tick()
        self.assertEqual(len(attempts), 1)
        self.topic("Compare a changed estimate.")
        worker.tick()
        self.assertEqual(len(attempts), 2)
        self.now[0] += 31; worker.tick()
        self.assertEqual(len(attempts), 3)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Named-pipe fixture requires os.mkfifo.")
    def test_topic_pipe_is_refused_without_waiting_for_a_writer(self):
        topic = self.topic(); worker = self.worker(); worker.configure("github", [])
        path = self.project.root / topic["path"]
        path.unlink(); os.mkfifo(path)
        thread = threading.Thread(target=worker.tick, daemon=True)
        thread.start(); thread.join(1)
        finished_without_writer = not thread.is_alive()
        if not finished_without_writer:
            # Unblock the prior unsafe read so the failing test leaves no worker.
            with open(path, "wb", buffering=0) as stream:
                stream.write(b"not a regular topic file")
            thread.join(2)
        self.assertTrue(finished_without_writer)
        self.assertEqual(self.adapter.rows, [])
        self.assertEqual(worker.status()["state"], "error")

    def test_received_markdown_is_guarded_even_if_its_index_hash_was_replaced(self):
        topic = self.topic(); worker = self.worker(); worker.configure("github", [])
        path = self.project.root / topic["path"]
        raw = path.read_bytes() + b"\nTry the unapproved symbol $z$.\n"
        path.write_bytes(raw)
        data = self.knowledge._load()
        data["discussion"]["topics"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
        self.knowledge._write(data)
        worker.tick()
        self.assertEqual(self.adapter.rows, [])
        self.assertEqual(worker.status()["state"], "error")
        self.assertNotIn("$z$", worker.status()["error"])

    def test_linked_issue_repository_and_immutable_id_are_verified(self):
        self.topic(); worker = self.worker(); worker.configure("github", []); worker.tick()
        self.adapter.rows[0]["html_url"] = "https://github.com/other/paper/issues/1"
        self.topic("Compare a second estimate."); worker.tick()
        self.assertEqual(self.adapter.comments, {})
        self.assertEqual(worker.status()["state"], "error")
        self.adapter.rows[0]["html_url"] = "https://github.com/fixture/paper/issues/1"
        self.adapter.rows[0]["id"] = 9999
        self.now[0] += 31; worker.tick()
        self.assertEqual(self.adapter.comments, {})
        self.assertEqual(worker.status()["state"], "error")

    def test_failed_configuration_witness_can_be_explicitly_reconfirmed(self):
        import research_github
        self.topic(); worker = self.worker()
        with mock.patch.object(research_github, "replace_file", side_effect=OSError("controlled private state failure")):
            with self.assertRaises(OSError):
                worker.configure("github", ["author"])
        self.worker().tick()
        self.assertFalse(self.adapter.rows)
        worker.configure("github", ["author"])
        worker.tick()
        self.assertEqual(len(self.adapter.rows), 1)

    def test_target_drift_and_forged_configuration_do_not_make_requests(self):
        self.topic(); worker = self.worker(); worker.configure("github", ["author"])
        calls = len(self.adapter.calls)
        self.target["target"]["repository_identity"] = "github.com/other/paper"
        worker.tick()
        self.assertEqual(len(self.adapter.calls), calls)
        self.target["target"]["repository_identity"] = "github.com/fixture/paper"
        data = self.knowledge._load(); data["discussion"]["github"]["approvers"] = ["attacker"]
        self.knowledge._write(data)
        self.worker().tick()
        self.assertEqual(len(self.adapter.calls), calls)
        worker.configure("github", ["author"]); worker.tick()
        self.assertEqual(len(self.adapter.rows), 1)


if __name__ == "__main__":
    unittest.main()
