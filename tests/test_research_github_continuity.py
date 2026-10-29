"""A cloned research topic reuses its verified issue without borrowing consent."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "prism_local"), str(Path(__file__).parent)]
from backends import Job
from fake_github_issues import FakeIssueAdapter
from project import Project
from research import ResearchKnowledge
from research_access import ResearchAccess
from research_github import GitHubDiscussions
import research_topics
from test_gitsync import git, identity
from tmpdirs import tmpdir


@unittest.skipUnless(shutil.which("git"), "needs local Git")
class GitHubTopicContinuity(unittest.TestCase):
    def setUp(self):
        self.work = tmpdir()
        self.source, self.clone = self.work / "author-a", self.work / "author-b"
        self.source.mkdir()
        (self.source / "main.tex").write_text("$x$\n")
        self.state_a, self.state_b = self.work / "state-a", self.work / "state-b"
        self.adapter = FakeIssueAdapter(repo_id=101)
        self.target = {"own": True, "blocked_reason": None,
            "target": {"repository_identity": "github.com/fixture/paper", "branch_ref": "refs/heads/main"},
            "bound_target": {"repository_identity": "github.com/fixture/paper", "branch_ref": "refs/heads/main"}}

    git = staticmethod(git)

    @staticmethod
    def commit_clone(source, destination, message):
        identity(source)
        git(source, "add", "-A")
        git(source, "commit", "-qm", message)
        git(source.parent, "clone", "-q", str(source), str(destination))

    @staticmethod
    def record(knowledge, text, number=1, topic_id=None):
        job = Job(number)
        job.root, job.provider, job.human_text = knowledge.project.root, "continuity-fixture", "Continue the estimate."
        args = {"title": "Direct estimate", "entries": [{"kind": "idea", "source": "assistant", "text": text}]}
        if topic_id is not None:
            args["topic_id"] = topic_id
        access = ResearchAccess(knowledge)
        queued = research_topics.stage(access, job, args)
        research_topics.finish(access, job, success=True, session_id="fixture-" + str(number))
        return queued

    def worker(self, knowledge):
        worker = GitHubDiscussions(knowledge, adapter=self.adapter, target=lambda: self.target)
        self.addCleanup(worker.close)
        return worker

    def published_clone(self, *, publish=True, text="Compare the direct estimate using $x$."):
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_a)}):
            knowledge = ResearchKnowledge(Project(self.source))
            knowledge.initialize(notation="h")
            topic = self.record(knowledge, text)
            self.source_project_id, self.original_issue = None, None
            if publish:
                worker = self.worker(knowledge)
                worker.configure("github", ["author"])
                self.source_project_id = knowledge.status()["discussion"]["github"]["project_id"]
                worker.tick()
                self.assertEqual(len(self.adapter.issue_rows), 1)
                self.original_issue = copy.deepcopy(self.adapter.issue_rows[0])
            self.original_document = (self.source / topic["path"]).read_bytes()
            self.topic_id, self.topic_path = topic["topic_id"], topic["path"]
        self.git(self.source, "init", "-q")
        self.commit_clone(self.source, self.clone, "Fixture research topic")

    def continue_clone(self):
        knowledge = ResearchKnowledge(Project(self.clone))
        self.assertFalse(knowledge.status()["initialized"])
        knowledge.initialize()
        self.assertEqual(knowledge.status()["discussion"]["mode"], "markdown")
        self.record(knowledge, "Check the endpoint hypothesis.", number=2, topic_id=self.topic_id)
        worker = self.worker(knowledge)
        worker.configure("github", ["author"], expected_repository="fixture/paper")
        self.assertNotEqual(knowledge.status()["discussion"]["github"]["project_id"], self.source_project_id)
        return knowledge, worker

    def test_explicit_clone_binding_reuses_one_issue_and_local_approval_identity(self):
        self.published_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge, worker = self.continue_clone()
            worker.tick()
            status = worker.status()
            proof = {"boundary": "Actual local clone and controlled issue adapter; no account or network.",
                "issues": self.adapter.issue_rows, "comments": self.adapter.comment_rows,
                "status": status, "source_project_id": self.source_project_id,
                "clone_project_id": knowledge.status()["discussion"]["github"]["project_id"],
                "known": [row["canonical"] for row in knowledge.classify("$x+h$")["known"]]}
            if os.environ.get("PRISM_GITHUB_CONTINUITY_EVIDENCE"):
                Path(os.environ["PRISM_GITHUB_CONTINUITY_EVIDENCE"]).write_text(json.dumps(proof, indent=2) + "\n")
            self.assertEqual(len(self.adapter.issue_rows), 1)
            self.assertEqual(self.adapter.issue_rows[0], self.original_issue)
            comments = self.adapter.comment_rows[1]
            self.assertEqual(len(comments), 1)
            self.assertIn("Check the endpoint hypothesis.", comments[0]["body"])
            self.assertEqual(status["topics"][0]["issue_url"], "https://github.com/fixture/paper/issues/1")
            self.assertEqual(status["topics"][0]["publication"], "published")
            self.assertEqual([row["canonical"] for row in knowledge.classify("$x+h$")["known"]], ["x"])
            self.assertTrue((self.clone / self.topic_path).read_bytes().startswith(self.original_document))

    def test_missing_or_ambiguous_received_issue_pauses_without_creating_then_retries(self):
        self.published_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge, _ = self.continue_clone()
            raw = (self.clone / self.topic_path).read_bytes()
            duplicate = {**self.original_issue, "id": 101000002, "number": 2,
                "html_url": "https://github.com/fixture/paper/issues/2"}
            wrong_url = {**self.original_issue, "html_url": "https://github.com/other/paper/issues/1"}
            for label, rows in (("missing", []), ("ambiguous", [self.original_issue, duplicate]),
                                ("wrong repository", [wrong_url])):
                with self.subTest(remote=label):
                    self.adapter.issue_rows = copy.deepcopy(rows)
                    before = copy.deepcopy(self.adapter.issue_rows)
                    worker = self.worker(knowledge)
                    worker.tick()
                    self.assertEqual(worker.status()["state"], "error")
                    self.assertEqual(self.adapter.issue_rows, before)
                    self.assertEqual(self.adapter.comment_rows[1], [])
                    self.assertEqual((self.clone / self.topic_path).read_bytes(), raw)
                    self.assertEqual(sum(call["method"] == "create_issue" for call in self.adapter.calls), 1)
            self.adapter.issue_rows = [copy.deepcopy(self.original_issue)]
            recovered = self.worker(knowledge)
            recovered.tick()
            self.assertEqual(recovered.status()["topics"][0]["publication"], "published")
            self.assertEqual(len(self.adapter.issue_rows), 1)
            self.assertEqual(len(self.adapter.comment_rows[1]), 1)
            self.worker(knowledge).tick()
            self.assertEqual(len(self.adapter.comment_rows[1]), 1)

    def test_received_repository_locator_cannot_replace_current_explicit_binding(self):
        self.published_clone()
        record = self.clone / "research/knowledge.json"
        data = json.loads(record.read_text())
        data["discussion"]["github"]["repository"]["id"] = 202
        record.write_text(json.dumps(data))
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge, worker = self.continue_clone()
            before = (self.clone / self.topic_path).read_bytes()
            worker.tick()
            self.assertEqual(worker.status()["state"], "error")
            self.assertEqual(knowledge.status()["discussion"]["github"]["repository"]["id"], 101)
            self.assertEqual(self.adapter.issue_rows, [self.original_issue])
            self.assertEqual(self.adapter.comment_rows[1], [])
            self.assertEqual((self.clone / self.topic_path).read_bytes(), before)
            self.assertEqual(sum(call["method"] == "create_issue" for call in self.adapter.calls), 1)

    def test_linked_issue_identity_or_marker_changes_cannot_redirect_continuation(self):
        self.published_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge, worker = self.continue_clone()
            worker.tick()
            self.assertEqual(len(self.adapter.comment_rows[1]), 1)
            before = (self.clone / self.topic_path).read_bytes()
            for change in ({"id": 101000099}, {"body": "A different issue body."}):
                with self.subTest(change=change):
                    self.adapter.issue_rows = [{**self.original_issue, **change}]
                    altered = copy.deepcopy(self.adapter.issue_rows)
                    reopened = self.worker(knowledge)
                    reopened.tick()
                    self.assertEqual(reopened.status()["state"], "error")
                    self.assertEqual(self.adapter.issue_rows, altered)
                    self.assertEqual(len(self.adapter.comment_rows[1]), 1)
                    self.assertEqual((self.clone / self.topic_path).read_bytes(), before)
            self.adapter.issue_rows = [copy.deepcopy(self.original_issue)]
            restarted = self.worker(knowledge)
            restarted.tick()
            self.assertEqual(restarted.status()["state"], "ok")
            self.assertEqual(len(self.adapter.comment_rows[1]), 1)
            self.assertEqual(sum(call["method"] == "create_issue" for call in self.adapter.calls), 1)

    def test_reused_issue_still_requires_a_current_root_proposal_and_actual_reply(self):
        self.published_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge, worker = self.continue_clone()
            worker.tick()
            proposal = knowledge.propose("q", "the current error term", "this argument", "readability",
                source_expression="x+x")
            worker.share_proposals(self.topic_id, [{key: proposal[key] for key in ("id", "revision", "content_hash")}])
            worker.tick()
            batch, = worker.status()["proposal_batches"]
            self.assertEqual(batch["publication"], "published")
            publication = next(row for row in self.adapter.comment_rows[1]
                if "Pending notation proposals" in row["body"])
            current_id = knowledge.status()["discussion"]["github"]["project_id"]
            self.assertIn("prism-proposals:" + current_id + ":" + self.topic_id, publication["body"])
            self.assertNotIn("prism-proposals:" + self.source_project_id + ":", publication["body"])
            reply = {"id": 6000, "body": "prism-batch:" + batch["id"] + "\nApprove $q$.",
                "user": {"id": 42, "login": "author", "type": "User"},
                "created_at": "2020-01-01T00:00:00Z", "updated_at": "2020-01-01T00:00:00Z",
                "issue_url": "https://api.github.com/repos/fixture/paper/issues/1",
                "html_url": "https://github.com/fixture/paper/issues/1#issuecomment-6000"}
            self.adapter.comment_rows[1].append(reply)
            self.worker(knowledge).tick()
            self.assertEqual(knowledge.status()["proposals"][0]["status"], "pending")
            self.assertEqual(knowledge.status()["github_decisions"], [])
            self.adapter.comment_rows[1].append({**reply, "id": 6001,
                "html_url": "https://github.com/fixture/paper/issues/1#issuecomment-6001",
                "created_at": "2026-10-29T12:00:00Z", "updated_at": "2026-10-29T12:00:00Z"})
            self.worker(knowledge).tick()
            self.assertEqual(knowledge.status()["proposals"][0]["status"], "approved")
            self.assertEqual([row["canonical"] for row in knowledge.classify("$h+q$")["known"]], ["q"])
            self.assertEqual(len(knowledge.status()["github_decisions"]), 1)
            self.assertEqual(len(self.adapter.issue_rows), 1)

    def test_a_second_clone_keeps_the_original_topic_issue_namespace(self):
        self.published_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge, worker = self.continue_clone()
            worker.tick()
            self.assertEqual(len(self.adapter.comment_rows[1]), 1)
            second_id = knowledge.status()["discussion"]["github"]["project_id"]
            second_document = (self.clone / self.topic_path).read_bytes()
        second = self.clone
        third = self.work / "author-c"
        self.commit_clone(second, third, "Fixture continued topic")
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.work / "state-c")}):
            knowledge = ResearchKnowledge(Project(third))
            knowledge.initialize()
            continued = self.record(knowledge, "Compare the boundary case.", number=3, topic_id=self.topic_id)
            worker = self.worker(knowledge)
            worker.configure("github", ["author"], expected_repository="fixture/paper")
            third_id = knowledge.status()["discussion"]["github"]["project_id"]
            self.assertNotIn(third_id, [self.source_project_id, second_id])
            worker.tick()
            status = worker.status()
            proof = {"boundary": "Two actual local clones and controlled issue adapter; no account/network.",
                "source_namespace": self.source_project_id, "second_namespace": second_id,
                "third_namespace": third_id, "topic_id": continued["topic_id"],
                "issue_count": len(self.adapter.issue_rows), "comment_count": len(self.adapter.comment_rows[1]),
                "status": status}
            if os.environ.get("PRISM_GITHUB_RELAY_EVIDENCE"):
                Path(os.environ["PRISM_GITHUB_RELAY_EVIDENCE"]).write_text(json.dumps(proof, indent=2) + "\n")
            self.assertEqual(continued["topic_id"], self.topic_id)
            self.assertEqual(len(self.adapter.issue_rows), 1)
            self.assertEqual(len(self.adapter.comment_rows[1]), 2)
            self.assertEqual(status["topics"][0]["publication"], "published")
            self.assertEqual(status["topics"][0]["issue_url"], "https://github.com/fixture/paper/issues/1")
            self.assertIn("Compare the boundary case.", self.adapter.comment_rows[1][-1]["body"])
            self.assertTrue((third / self.topic_path).read_bytes().startswith(second_document))
            self.assertEqual([row["canonical"] for row in knowledge.classify("$x+h$")["known"]], ["x"])

    def test_markdown_origin_can_publish_once_then_relay_its_first_issue(self):
        self.published_clone(publish=False)
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge, worker = self.continue_clone()
            worker.tick()
            self.assertEqual(len(self.adapter.issue_rows), 1)
            self.assertEqual(self.adapter.comment_rows[1], [])
            second_id = knowledge.status()["discussion"]["github"]["project_id"]
            second_document = (self.clone / self.topic_path).read_bytes()
            original_issue = copy.deepcopy(self.adapter.issue_rows[0])
        second = self.clone
        third = self.work / "author-c"
        self.commit_clone(second, third, "Fixture first issue publication")
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.work / "state-c")}):
            knowledge = ResearchKnowledge(Project(third))
            knowledge.initialize()
            self.record(knowledge, "Compare the boundary case.", number=3, topic_id=self.topic_id)
            worker = self.worker(knowledge)
            worker.configure("github", ["author"], expected_repository="fixture/paper")
            self.assertNotEqual(knowledge.status()["discussion"]["github"]["project_id"], second_id)
            worker.tick()
            self.assertEqual(self.adapter.issue_rows, [original_issue])
            self.assertEqual(len(self.adapter.comment_rows[1]), 1)
            self.assertEqual(worker.status()["topics"][0]["publication"], "published")
            self.assertTrue((third / self.topic_path).read_bytes().startswith(second_document))

    def test_received_unknown_notation_is_not_republished_by_marker_reuse(self):
        self.published_clone(text="Compare the direct estimate using $h$.")
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge, worker = self.continue_clone()
            before = (self.clone / self.topic_path).read_bytes()
            worker.tick()
            self.assertEqual(worker.status()["state"], "error")
            self.assertEqual(self.adapter.issue_rows, [self.original_issue])
            self.assertEqual(self.adapter.comment_rows[1], [])
            self.assertEqual(knowledge.classify("$h$")["known"], [])
            self.assertEqual((self.clone / self.topic_path).read_bytes(), before)
            self.assertTrue(before.startswith(self.original_document))


if __name__ == "__main__":
    unittest.main()
