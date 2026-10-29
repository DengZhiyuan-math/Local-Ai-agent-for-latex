"""Issue discussion lifecycle through real editor HTTP and an offline adapter."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import urllib.request

import test_research_http as base
import test_research_topics_http as topics


class ResearchGitHubHTTP(unittest.TestCase):
    setUp = base.ResearchHTTP.setUp
    tearDown = base.ResearchHTTP.tearDown
    stop = base.ResearchHTTP.stop
    request = base.ResearchHTTP.request
    initialize = base.ResearchHTTP.initialize
    events = base.ResearchHTTP.events
    finish = base.ResearchHTTP.finish
    requests = base.ResearchHTTP.requests
    turn = topics.ResearchTopicsHTTP.turn
    record = topics.ResearchTopicsHTTP.record

    def start(self, empty=False, automatic=False, state_dir=None, remote_file=None):
        (self.work / "ready.json").unlink(missing_ok=True)
        self.log = (self.work / "server.log").open("ab")
        fixture = Path(__file__).parent / "browser/github_topics_fixture.py"
        self.process = subprocess.Popen([sys.executable, str(fixture), str(self.work)]
                                        + (["--empty"] if empty else [])
                                        + (["--automatic"] if automatic else [])
                                        + (["--state-dir", str(state_dir)] if state_dir else [])
                                        + (["--remote-file", str(remote_file)] if remote_file else []),
                                        stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 10
        while not (self.work / "ready.json").exists() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            time.sleep(.02)
        self.assertTrue((self.work / "ready.json").exists(), (self.work / "server.log").read_text())
        ready = json.loads((self.work / "ready.json").read_text())
        self.url, self.root = ready["url"].rstrip("/"), Path(ready["root"])
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        code, info = self.request("/api/agent/info")
        self.assertEqual(code, 200, info)
        self.key = info["project_key"]

    def state(self):
        code, result = self.request("/api/research")
        self.assertEqual(code, 200, result)
        return result

    def configure(self, mode="github", approvers=None):
        body = {"project_key": self.key,
            "mode": mode, "approvers": ["author"] if approvers is None else approvers,
            **({"expected_repository": "fixture/paper"} if mode == "github" else {})}
        code, result = self.request("/api/research/discussion", body)
        self.assertEqual(code, 200, result)
        return result["github_discussion"]

    def control(self, action, **data):
        code, result = self.request("/__fixture/github", {"project_key": self.key, "action": action, **data})
        self.assertEqual(code, 200, result)
        return result

    def remote(self):
        code, result = self.request("/__fixture/github")
        self.assertEqual(code, 200, result)
        return result

    def open_page(self):
        self.assertEqual(self.request("/api/presence", {"client": "github-fixture-page"})[0], 200)

    def close_page(self):
        req = urllib.request.Request(self.url + "/api/bye", data=b"github-fixture-page",
            headers={"Content-Type": "text/plain", "X-Prism-Local": "1"})
        with self.opener.open(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(json.load(response)["ok"])

    def test_local_record_is_distinct_from_one_persistent_published_issue(self):
        self.initialize()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*.tex")}
        config = self.configure()
        self.assertEqual(config["repository"]["full_name"], "fixture/paper")
        self.assertEqual(config["approvers"], ["author"])
        events = self.record([topics.entry("idea", "Compare the direct argument before choosing it.")])
        notice = next(event for event in events if event["t"] == "research_record")
        self.assertTrue((self.root / notice["path"]).exists())
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "pending")
        self.assertEqual(self.remote()["issues"], [])
        self.open_page(); self.control("tick")
        status = self.state()["github_discussion"]
        self.assertEqual(status["topics"][0]["publication"], "published")
        remote = self.remote()
        self.assertEqual(len(remote["issues"]), 1, remote)
        self.assertIn("## Ideas", remote["issues"][0]["body"])
        self.assertEqual(next(event for event in events if event["t"] == "done")["changed"], [])
        self.assertEqual({p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*.tex")}, before)
        self.assertEqual(len(self.requests()), 2)
        self.assertFalse(self.request("/api/tree")[1]["sync"]["enabled"])

    def test_tree_discussion_version_tracks_private_publication_and_failure_state(self):
        self.initialize(); self.configure(); self.open_page()
        events = self.record([topics.entry("idea", "Compare the direct estimate.")])
        topic_id = next(event["topic_id"] for event in events if event["t"] == "research_record")
        saved = self.root / "research/knowledge.json"

        def version():
            code, tree = self.request("/api/tree")
            self.assertEqual(code, 200, tree)
            self.assertIn("github_discussion_version", tree)
            self.assertRegex(tree["github_discussion_version"], r"^[0-9a-f]{64}$")
            return tree["github_discussion_version"]

        before = version()
        unchanged = saved.stat().st_mtime_ns, saved.read_bytes()
        self.control("tick")
        published = version()
        self.assertNotEqual(published, before)
        self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), unchanged)
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "published")
        self.control("tick")
        self.assertEqual(version(), published)
        self.record([topics.entry("question", "Which boundary case needs another argument?")], topic_id=topic_id)
        pending = version()
        unchanged = saved.stat().st_mtime_ns, saved.read_bytes()
        self.control("error", method="create_comment", uncertain=False)
        self.control("tick")
        failed = version()
        self.assertNotEqual(failed, pending)
        self.assertTrue(self.state()["github_discussion"]["error"])
        self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), unchanged)
        self.control("unknown", method="create_comment")
        self.control("tick")
        unknown = version()
        self.assertNotEqual(unknown, failed)
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "unknown")
        self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), unchanged)
        self.control("tick")
        recovered = version()
        self.assertNotEqual(recovered, unknown)
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "published")
        self.control("tick")
        self.assertEqual(version(), recovered)
        self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), unchanged)

    def test_later_updates_restart_and_unchanged_ticks_reuse_the_same_issue(self):
        self.initialize(); self.configure(); self.open_page()
        first = self.record([topics.entry("idea", "Compare the direct and iterative estimates.")])
        notice = next(event for event in first if event["t"] == "research_record")
        self.control("tick")
        original_body = self.remote()["issues"][0]["body"]
        choice = "We decided to compare the direct and iterative estimates."
        self.record([topics.entry("decision", choice, "human", choice)], human_text=choice,
                    topic_id=notice["topic_id"])
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "pending")
        self.control("tick")
        remote = self.remote()
        self.assertEqual(len(remote["issues"]), 1)
        self.assertEqual(remote["issues"][0]["body"], original_body, "coauthor-editable issue body is not replaced")
        self.assertEqual(len(remote["comments"]["1"]), 1)
        self.assertIn(choice, remote["comments"]["1"][0]["body"])
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["published_revision"], 2)
        files = [self.root / notice["path"], self.root / "research/knowledge.json",
                 *list((self.work / "state/research-github").glob("*.json"))]
        before = [(path.stat().st_mtime_ns, path.read_bytes()) for path in files]
        model_requests = len(self.requests())
        self.control("tick"); self.control("tick")
        self.assertEqual([(path.stat().st_mtime_ns, path.read_bytes()) for path in files], before)
        self.assertEqual(len(self.requests()), model_requests)
        self.stop(); self.start(); self.open_page(); self.control("tick")
        remote = self.remote()
        self.assertEqual(len(remote["issues"]), 1)
        self.assertEqual(len(remote["comments"]["1"]), 1)
        self.assertEqual(sum(call["method"] == "create_issue" for call in remote["calls"]), 1)
        self.assertEqual(sum(call["method"] == "create_comment" for call in remote["calls"]), 1)
        self.assertEqual(len(self.requests()), model_requests)

    def test_designated_accounts_and_remote_replies_remain_untrusted_discussion(self):
        self.initialize(); self.configure(approvers=["author", "coauthor"]); self.open_page()
        self.record([topics.entry("idea", "Compare the direct estimate before choosing a method.")])
        self.control("tick")
        before = (self.root / "main.tex").read_bytes()
        model_requests = len(self.requests())
        self.control("comment", id=6001, body="Maybe compare the direct method.",
                     user={"id": 42, "login": "author", "type": "User"})
        self.control("comment", id=6002, body="Yes, approve $z$ for this estimate.",
                     user={"id": 55, "login": "coauthor", "type": "User"})
        self.control("comment", id=6003, body='author said: "Yes, approve $z$."',
                     user={"id": 77, "login": "helper-bot", "type": "Bot"})
        self.control("tick")
        state = self.state()
        comments = state["github_discussion"]["comments"]
        self.assertEqual(len(comments), 3, comments)
        self.assertEqual({row["author"]["login"] for row in comments}, {"author", "coauthor", "helper-bot"})
        self.assertTrue(all(row["approval"] is False for row in comments))
        self.assertIn("Maybe compare the direct method.", next(row["preview"] for row in comments if row["id"] == 6001))
        self.assertNotIn("$z$", "\n".join(row["preview"] for row in comments))
        self.assertEqual(state["decisions"], [])
        self.assertEqual(state["proposals"], [])
        self.assertNotIn("z", {symbol["canonical"] for symbol in state["symbols"]})
        self.assertEqual((self.root / "main.tex").read_bytes(), before)
        self.assertEqual(len(self.requests()), model_requests)

    def test_unknown_applied_issue_and_comment_results_reconcile_without_duplicate_delivery(self):
        self.initialize(); self.configure(); self.open_page()
        self.control("unknown", method="create_issue")
        first = self.record([topics.entry("idea", "Compare the direct estimate.")])
        notice = next(event for event in first if event["t"] == "research_record")
        self.control("tick")
        self.assertEqual(len(self.remote()["issues"]), 1)
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "unknown")
        self.stop(); self.start(); self.open_page(); self.control("tick")
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "published")
        self.control("unknown", method="create_comment")
        self.record([topics.entry("question", "Which case needs a separate estimate?")], topic_id=notice["topic_id"])
        self.control("tick")
        self.assertEqual(len(self.remote()["comments"]["1"]), 1)
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "unknown")
        self.control("tick")
        remote = self.remote()
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["published_revision"], 2)
        self.assertEqual(sum(row["method"] == "create_issue" for row in remote["calls"]), 1)
        self.assertEqual(sum(row["method"] == "create_comment" for row in remote["calls"]), 1)

    def test_known_failure_retries_but_an_unknown_absent_comment_is_not_blindly_reposted(self):
        self.initialize(); self.configure(); self.open_page()
        first = self.record([topics.entry("idea", "Compare the direct estimate.")])
        notice = next(event for event in first if event["t"] == "research_record")
        self.control("error", method="create_issue", message="known not applied", uncertain=False)
        self.control("tick")
        self.assertEqual(self.remote()["issues"], [])
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "pending")
        self.control("tick")
        self.assertEqual(len(self.remote()["issues"]), 1)
        self.control("error", method="create_comment", message="unknown outcome", uncertain=True)
        self.record([topics.entry("question", "Which case needs a separate estimate?")], topic_id=notice["topic_id"])
        self.control("tick"); self.control("tick")
        self.stop(); self.start(); self.open_page(); self.control("tick")
        remote = self.remote()
        self.assertEqual(remote["comments"]["1"], [])
        self.assertEqual(sum(row["method"] == "create_comment" for row in remote["calls"]), 1)
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "unknown")
        sent = next(row["body"] for row in remote["calls"] if row["method"] == "create_comment")
        self.control("comment", body=sent)
        self.control("tick")
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "published")
        self.assertEqual(sum(row["method"] == "create_comment" for row in self.remote()["calls"]), 1)

    def test_progress_write_failure_never_reports_remote_success_and_recovers_after_restart(self):
        self.initialize(); self.configure(); self.open_page()
        self.record([topics.entry("idea", "Compare the direct estimate.")])
        marker = self.work / "fail-github-progress"
        marker.touch(); self.control("tick")
        self.assertEqual(self.remote()["issues"], [])
        self.assertNotEqual(self.state()["github_discussion"]["topics"][0]["publication"], "published")
        marker.unlink()
        marker = self.work / "fail-github-progress-after-apply"
        marker.touch(); self.control("tick")
        self.assertEqual(len(self.remote()["issues"]), 1)
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "unknown")
        requests = len(self.requests())
        self.stop(); marker.unlink(); self.start(); self.open_page(); self.control("tick")
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "published")
        self.assertEqual(sum(row["method"] == "create_issue" for row in self.remote()["calls"]), 1)
        self.assertEqual(len(self.requests()), requests)

    def test_failed_comment_mirror_save_is_retryable_without_model_or_permission_changes(self):
        self.initialize(); self.configure(); self.open_page()
        self.record([topics.entry("idea", "Compare the direct estimate.")]); self.control("tick")
        self.control("comment", body="Which boundary term remains unresolved?")
        marker = self.work / "fail-github-mirror"
        marker.touch(); self.control("tick")
        state = self.state()
        saved = json.loads((self.root / "research/knowledge.json").read_text())
        self.assertEqual(saved["discussion"].get("external_comments", []), [])
        self.assertTrue(state["github_discussion"]["error"])
        requests = len(self.requests())
        self.stop(); marker.unlink(); self.start(); self.open_page(); self.control("tick")
        state = self.state()
        self.assertEqual(len(state["github_discussion"]["comments"]), 1)
        saved = json.loads((self.root / "research/knowledge.json").read_text())
        self.assertEqual(len(saved["discussion"]["external_comments"]), 1)
        self.assertEqual(state["decisions"], [])
        self.assertEqual(state["proposals"], [])
        self.assertEqual(len(self.requests()), requests)

    def test_read_outage_preserves_local_revision_and_retries_without_model_work(self):
        self.initialize(); self.configure(); self.open_page()
        first = self.record([topics.entry("idea", "Compare the direct estimate.")])
        topic_id = next(event["topic_id"] for event in first if event["t"] == "research_record")
        self.control("tick")
        self.record([topics.entry("question", "Which boundary case needs another argument?")], topic_id=topic_id)
        saved = self.root / "research/knowledge.json"
        before = saved.stat().st_mtime_ns, saved.read_bytes()
        requests = len(self.requests())
        self.control("offline", value=True); self.control("tick")
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "pending")
        self.assertTrue(self.state()["github_discussion"]["error"])
        self.assertEqual(self.remote()["comments"]["1"], [])
        self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), before)
        self.control("offline", value=False); self.control("tick")
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "published")
        self.assertEqual(len(self.remote()["comments"]["1"]), 1)
        self.assertEqual(len(self.requests()), requests)

    def test_wrong_project_repository_drift_and_forged_settings_cannot_publish(self):
        self.initialize()
        body = {"project_key": "foreign-project", "mode": "github", "approvers": ["author"]}
        self.assertEqual(self.request("/api/research/discussion", body)[0], 409)
        self.assertEqual(self.remote()["calls"], [])
        body["project_key"] = self.key
        body["expected_repository"] = "fixture/other"
        self.assertEqual(self.request("/api/research/discussion", body)[0], 409)
        self.assertEqual(self.remote()["calls"], [])
        self.configure(); self.open_page()
        self.record([topics.entry("idea", "Compare the direct estimate.")])
        saved = self.root / "research/knowledge.json"
        original = saved.read_bytes()
        forged = json.loads(original)
        forged["discussion"]["github"]["approvers"] = ["forged-account"]
        saved.write_text(json.dumps(forged))
        calls = self.remote()["calls"]
        self.control("tick")
        self.assertEqual(self.remote()["calls"], calls)
        self.assertEqual(self.state()["github_discussion"]["state"], "error")
        saved.write_bytes(original)
        self.control("target", url="https://github.com/fixture/other.git")
        self.control("tick")
        self.assertEqual(self.remote()["calls"], calls)
        self.assertEqual(self.remote()["issues"], [])
        self.control("target", url="https://github.com/fixture/paper.git")
        self.control("repository", value={"id": 99})
        self.control("tick")
        self.assertEqual(self.remote()["issues"], [])
        self.assertEqual(self.state()["github_discussion"]["state"], "error")
        self.control("repository", value={"id": 1})
        self.control("override", method="repository", value={"id": "1", "full_name": "fixture/paper"})
        self.control("tick")
        self.assertEqual(self.remote()["issues"], [])
        self.assertEqual(self.state()["github_discussion"]["state"], "error")
        self.control("override", method="repository", value=self.remote()["repo"])
        self.control("tick")
        self.assertEqual(len(self.remote()["issues"]), 1)
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "published")
        self.assertFalse(self.request("/api/tree")[1]["sync"]["enabled"])

    def test_two_open_projects_share_local_state_without_sharing_discussion_progress(self):
        self.initialize(); self.configure(); self.open_page()
        self.record([topics.entry("idea", "Compare the direct estimate.")]); self.control("tick")
        saved = self.root / "research/knowledge.json"
        before = saved.stat().st_mtime_ns, saved.read_bytes()
        other = type(self)("runTest")
        other.work = self.work / "other"
        other.work.mkdir()
        other.process = None
        try:
            other.start(state_dir=self.work / "state", remote_file=self.work / "github-remote.json")
            self.assertNotEqual(other.key, self.key)
            other.initialize(); other.configure(approvers=["coauthor"]); other.open_page()
            other.record([topics.entry("question", "Which boundary estimate remains unresolved?")])
            other.control("tick")
            self.assertEqual(len(other.remote()["issues"]), 2)
            self.assertEqual(other.state()["github_discussion"]["approvers"], ["coauthor"])
            self.assertEqual(self.state()["github_discussion"]["approvers"], ["author"])
            other.control("comment", issue=2, body="Only the second project receives this discussion.")
            other.control("tick"); self.control("tick")
            self.assertEqual(len(other.state()["github_discussion"]["comments"]), 1)
            self.assertEqual(self.state()["github_discussion"]["comments"], [])
            self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), before)
            self.assertEqual(len(self.remote()["issues"]), 2)
            self.assertNotEqual(self.state()["github_discussion"]["topics"][0]["issue_url"],
                                other.state()["github_discussion"]["topics"][0]["issue_url"])
            private = list((self.work / "state/research-github").glob("*.json"))
            progress = [json.loads(path.read_text()) for path in private if not path.name.endswith(".configuration.json")]
            self.assertEqual({row["project_key"] for row in progress}, {self.key, other.key})
            self.assertFalse(other.request("/api/tree")[1]["sync"]["enabled"])
        finally:
            other.stop()

    def test_started_background_worker_follows_presence_without_manual_ticks(self):
        self.stop(); self.start(automatic=True)
        self.initialize(); self.configure(); self.open_page()
        self.record([topics.entry("idea", "Compare the direct estimate.")])
        self.control("wake")
        self.assertTrue(self.remote()["automatic"])
        self.assertEqual(len(self.remote()["issues"]), 1)
        self.assertEqual(self.state()["github_discussion"]["topics"][0]["publication"], "published")
        self.close_page()
        self.control("comment", body="The closed project can read this after reopening.")
        before = self.remote()
        saved = self.root / "research/knowledge.json"
        unchanged = saved.stat().st_mtime_ns, saved.read_bytes()
        self.control("wake")
        after = self.remote()
        self.assertGreater(after["ticks"], before["ticks"])
        self.assertEqual(after["calls"], before["calls"])
        self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), unchanged)
        self.assertEqual(self.state()["github_discussion"]["state"], "paused")
        requests = len(self.requests())
        self.open_page(); self.control("wake")
        self.assertEqual(len(self.state()["github_discussion"]["comments"]), 1)
        self.assertEqual(len(self.requests()), requests)

    def test_last_page_close_pauses_checks_and_reopening_catches_up(self):
        self.initialize(); self.configure(); self.open_page()
        self.record([topics.entry("idea", "Compare the direct estimate.")]); self.control("tick")
        self.close_page()
        self.control("comment", body="The boundary case still needs an argument.")
        before = self.remote()
        saved = self.root / "research/knowledge.json"
        unchanged = saved.stat().st_mtime_ns, saved.read_bytes()
        model_requests = len(self.requests())
        self.control("tick"); self.control("tick")
        self.assertEqual(self.remote()["calls"], before["calls"])
        self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), unchanged)
        self.assertEqual(self.state()["github_discussion"]["state"], "paused")
        self.open_page(); self.control("tick")
        comments = self.state()["github_discussion"]["comments"]
        self.assertEqual(len(comments), 1)
        self.assertIn("boundary case", comments[0]["preview"])
        self.assertEqual(len(self.requests()), model_requests)

    def test_late_read_response_cannot_update_a_closed_project(self):
        self.initialize(); self.configure(); self.open_page()
        self.record([topics.entry("idea", "Compare the direct estimate.")]); self.control("tick")
        self.control("comment", body="A late reply about the boundary case.")
        self.control("block", method="list_comments")
        errors = []
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
        self.close_page()
        saved = self.root / "research/knowledge.json"
        before = saved.stat().st_mtime_ns, saved.read_bytes()
        self.control("release")
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual((saved.stat().st_mtime_ns, saved.read_bytes()), before)
        self.assertEqual(self.state()["github_discussion"]["comments"], [])
        calls = self.remote()["calls"]
        self.control("tick")
        self.assertEqual(self.remote()["calls"], calls)
        self.open_page(); self.control("tick")
        self.assertEqual(len(self.state()["github_discussion"]["comments"]), 1)


if __name__ == "__main__":
    unittest.main()
