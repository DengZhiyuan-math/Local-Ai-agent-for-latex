"""Joined same-project research journeys through real HTTP and supported adapters."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import urllib.request

import test_github_approvals_http as approval
import test_research_guard_http as guard
import test_research_http as base
import test_research_topics_http as topics
from test_agent import call_chunks

NATIVE_CLI = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


@unittest.skipUnless(shutil.which("git"), "needs Git for joined local transport")
class ResearchIntegration(unittest.TestCase):
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
    state = approval.GitHubApprovalsHTTP.state
    configure = approval.GitHubApprovalsHTTP.configure
    control = approval.GitHubApprovalsHTTP.control
    remote = approval.GitHubApprovalsHTTP.remote
    open_page = approval.GitHubApprovalsHTTP.open_page
    close_page = approval.GitHubApprovalsHTTP.close_page
    topic = approval.GitHubApprovalsHTTP.topic
    propose = approval.GitHubApprovalsHTTP.propose
    share = approval.GitHubApprovalsHTTP.share
    remote_reply = approval.GitHubApprovalsHTTP.remote_reply

    def start(self, empty=False, received_origin=None, remote_file=None):
        (self.work / "ready.json").unlink(missing_ok=True)
        self.log = (self.work / "server.log").open("ab")
        fixture = Path(__file__).parent / "browser/research_integration_fixture.py"
        self.process = subprocess.Popen([sys.executable, str(fixture), str(self.work)]
                                        + (["--empty"] if empty else [])
                                        + (["--received-origin", str(received_origin)] if received_origin else [])
                                        + (["--remote-file", str(remote_file)] if remote_file else []),
                                        stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 15
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
        self.profiles = {name: Path(path) for name, path in self.transport()["profiles"].items()}

    def transport(self):
        code, result = self.request("/__fixture/integration")
        self.assertEqual(code, 200, result)
        return result

    def sync_control(self, action, **data):
        timeout = 30 if action in ("tick", "pull", "remote_commit") else 5
        code, result = self.request("/__fixture/integration", {
            "project_key": self.key, "action": action, **data}, timeout=timeout)
        self.assertEqual(code, 200, result)
        return result

    def sync(self, enabled):
        code, result = self.request("/api/git/sync", {"project_key": self.key, "action": "on" if enabled else "off"})
        self.assertEqual(code, 200, result)
        return result

    def author(self, text, **extra):
        return self.finish(self.turn([[base.say("The author decision is saved.")]], human_text=text, **extra))

    def inference_counts(self):
        counts = {"api": len(self.requests())}
        for provider, profile in self.profiles.items():
            path = profile / "cli-requests.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            counts[provider] = sum(row.get("kind") == "user" for row in rows)
        return counts

    def cli(self, provider, actions, *, session=None, human="Continue the current estimate.", model="default", **extra):
        self.sync_control("script", provider=provider, script=actions)
        code, result = self.request("/api/agent", {"project_key": self.key, "provider": provider,
            "model": model, "mode": "edit", "prompt": human, "human_text": human,
            "active_file": "main.tex", "session_id": session, **extra})
        self.assertEqual(code, 200, result)
        return self.finish(result["job"])

    def known_use(self, provider, name, *, session=None, model="default"):
        before = (self.root / "main.tex").read_text()
        text = before.replace("\\end{document}", "Known $" + name + "+" + name + "$.\n\\end{document}")
        if provider.startswith("fixture"):
            events = self.finish(self.turn([
                call_chunks("known", "write_file", {"path": "main.tex", "content": text}),
                [base.say("The estimate uses $" + name + "$.")]], mode="edit", provider=provider,
                model="fixture-model" if model == "default" else model, session=session))
        else:
            events = self.cli(provider, [{"type": "mcp", "name": "write_file", "arguments": {
                "path": "main.tex", "content": text}}, {"type": "text", "text": "The estimate uses $" + name + "$."}],
                session=session, model=model)
        self.assertEqual((self.root / "main.tex").read_text(), text, events)
        self.assertFalse(any(row["t"] == "notation_proposal" for row in events), events)
        self.assertIn("$" + name + "$", "".join(row.get("text", "") for row in events
                                                if row["t"] in ("text", "delta")), (provider, events))
        return next((row["session_id"] for row in events if row["t"] == "init"), None)

    def held_use(self, provider, name, *, session=None):
        before = (self.root / "main.tex").read_bytes()
        content = before.decode().replace("\\end{document}", "Unreviewed $" + name + "$.\n\\end{document}")
        if provider.startswith("fixture"):
            events = self.finish(self.turn([call_chunks("held", "write_file", {
                "path": "main.tex", "content": content})], provider=provider, mode="edit", human_text="Continue the estimate."))
        else:
            events = self.cli(provider, [{"type": "mcp", "name": "write_file", "arguments": {
                "path": "main.tex", "content": content}}], session=session)
        self.assertEqual((self.root / "main.tex").read_bytes(), before, events)
        self.assertEqual(next(row for row in events if row["t"] == "done")["changed"], [])
        return events

    def receive(self, name):
        self.sync(True)
        code, saved = self.request("/api/git/sync", {
            "project_key": self.key, "action": "commit"}, timeout=30)
        self.assertEqual(code, 200, saved)
        self.assertIsNone(saved["error"])
        before = self.sync_control("tick")
        self.assertEqual(before["receiver_head"], before["remote_head"], before)
        text = (self.root / "main.tex").read_text().replace("\\end{document}", "Received $" + name + "$.\n\\end{document}")
        remote = self.sync_control("remote_commit", files={"main.tex": text})
        self.assertNotEqual(remote["remote_head"], remote["receiver_head"])
        pulled = self.sync_control("pull")
        self.assertEqual(pulled["sync"]["behind"], 0)
        self.assertEqual((self.root / "main.tex").read_text(), text)
        return {"before": before, "remote": remote, "pulled": pulled}

    def capture(self, name, value):
        folder = os.environ.get("PRISM_INTEGRATION_EVIDENCE")
        if folder:
            path = Path(folder)
            path.mkdir(parents=True, exist_ok=True)
            (path / ("journey-" + name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def publish_batch(self):
        self.initialize()
        self.configure()
        self.open_page()
        topic = self.topic()
        self.control("tick")
        proposals = self.propose(("z", "w"))
        batch = self.share(topic, proposals)
        self.control("tick")
        return topic, proposals, batch

    def test_github_decisions_restore_across_all_adapters_then_real_receive_needs_new_confirmation(self):
        topic_id, proposals, batch = self.publish_batch()
        self.remote_reply(batch, "Approve $z$; reject $w$.", id=8100,
                          user={"id": 999, "login": "outsider", "type": "User"})
        self.control("tick")
        self.assertTrue(all(row["status"] == "pending" for row in self.state()["proposals"]))
        self.remote_reply(batch, "Approve $z$; reject $w$.", id=8101)
        self.control("tick")
        self.assertEqual({row["canonical"]: row["status"] for row in self.state()["proposals"]}, {"z": "approved", "w": "rejected"})
        self.author("Use $a_i$ for $x+x$ with i in {1,2,3}.")
        sessions = {}
        native_providers = ("claude", "codex", "deepcode") if NATIVE_CLI else ()
        for provider in ("fixture-2", *native_providers):
            sessions[provider] = self.known_use(provider, "z")
            self.held_use(provider, "w", session=sessions[provider] if not provider.startswith("fixture") else None)
        if NATIVE_CLI:
            self.known_use("codex", "z", session=self.transport()["legacy_session"])
        saved = self.state()
        calls = self.inference_counts()
        self.stop(); self.start(); self.open_page()
        self.assertEqual(self.inference_counts(), calls)
        self.assertEqual(self.state()["decisions"], saved["decisions"])
        self.assertEqual(self.state()["families"], saved["families"])
        if NATIVE_CLI:
            self.known_use("codex", "a_2", session=sessions["codex"], model="fast")
            native = [json.loads(line) for line in (self.profiles["codex"] / "cli-requests.jsonl").read_text().splitlines()]
            self.assertTrue(any(row.get("kind") == "thread" and row.get("session_id") == sessions["codex"]
                                and row.get("params", {}).get("model") == "fast" for row in native))
        else:
            self.assertTrue(sessions["fixture-2"])
            resumed = self.known_use("fixture-2", "a_2", session=sessions["fixture-2"], model="fixture-model-2")
            self.assertEqual(resumed, sessions["fixture-2"])
            self.assertEqual(self.requests()[-1]["body"]["model"], "fixture-model-2")
        before_receive = self.inference_counts()
        transport = self.receive("r")
        self.assertEqual(self.inference_counts(), before_receive)
        original = (self.root / "main.tex").read_bytes()
        state = self.state()
        self.assertNotIn("r", {row["canonical"] for row in state["symbols"]})
        question = next(row for row in state["received_confirmations"] if row["canonical"] == "r")
        self.held_use("deepcode" if NATIVE_CLI else "fixture", "r", session=sessions.get("deepcode"))
        self.assertFalse(any(row["canonical"] == "r" for row in self.state()["proposals"]))
        if NATIVE_CLI:
            self.cli("claude", [{"type": "text", "text": "The author decision is saved."}],
                     human="Yes", received_subjects=[question["id"]])
        else:
            self.author("Yes", received_subjects=[question["id"]])
        self.assertEqual((self.root / "main.tex").read_bytes(), original)
        for provider in ("fixture", *native_providers):
            self.known_use(provider, "r")
        final = self.state()
        self.assertEqual({row["canonical"]: row["status"] for row in final["proposals"]}, {"z": "approved", "w": "rejected"})
        self.assertEqual(len(final["discussion_records"]["topics"]), 1)
        self.assertEqual(final["discussion_records"]["topics"][0]["id"], topic_id)
        decisions = final["decisions"]
        self.assertEqual(len({row["id"] for row in decisions}), len(decisions))
        self.capture("all-providers", {"topic": topic_id, "proposals": proposals, "batch": batch,
                     "sessions": sessions, "transport": transport, "final": final,
                     "native_cli_exercised": NATIVE_CLI,
                     "legacy_codex": self.transport()["legacy_session"] if NATIVE_CLI else None})

    def test_received_unknown_refusal_and_family_flow_into_topic_and_issue_lifecycle_without_laundering(self):
        self.initialize(); self.configure(); self.open_page()
        topic_id = self.topic(); self.control("tick")
        self.author("Use $a_i$ for $x+x$ with i in {1,2,3}.")
        events = self.finish(self.turn([call_chunks("q", "propose_notation", {**guard.PROPOSAL, "notation": "q"})]))
        session = next(row["session_id"] for row in events if row["t"] == "init")
        proposed = [row["proposal"] for row in events if row["t"] == "notation_proposal"]
        self.author("Reject $q$ and keep its expression expanded.", session=session)
        self.assertTrue(any(row.get("action", {}).get("rejection_scope") == "expression"
                            for row in self.state()["rejections"]))
        transport = self.receive("r")
        source = (self.root / "main.tex").read_bytes()
        before = self.state()["discussion_records"]["topics"][0]
        markdown = self.root / before["path"]
        original_markdown = markdown.read_bytes()
        for text in ("Try $r$ next.", r"Let $t=\ell+\ell_+$.", "Try $a_9$ next."):
            self.record([topics.entry("idea", text)], topic_id=topic_id)
            self.assertEqual(markdown.read_bytes(), original_markdown)
            self.assertEqual((self.root / "main.tex").read_bytes(), source)
            held = self.state()
            self.assertNotIn("r", {row["canonical"] for row in held["symbols"]})
            self.assertFalse(any(row["canonical"] in ("r", "t") for row in held["proposals"]))
        allowed = self.record([topics.entry("idea", "Compare $a_2$ with $x$.")], topic_id=topic_id)
        self.assertTrue(any(row["t"] == "research_record" for row in allowed), allowed)
        self.control("tick")
        self.assertIn("Compare $a_2$", markdown.read_text())
        (self.work / "fail-topic-markdown").touch()
        self.record([topics.entry("idea", "Compare the two finite estimates.")], topic_id=topic_id)
        request_count = self.inference_counts()
        self.stop(); self.start(); self.open_page()
        (self.work / "fail-topic-markdown").unlink()
        self.state()  # The real page's research refresh drives the existing local retry.
        self.control("tick")
        self.assertEqual(self.inference_counts(), request_count)
        self.assertIn("Compare the two finite estimates.", markdown.read_text())
        self.close_page()
        self.control("comment", issue=1, id=8200, body="Maybe compare the alternate argument.")
        before_calls = len(self.remote()["calls"])
        self.control("tick")
        self.assertEqual(len(self.remote()["calls"]), before_calls)
        self.open_page(); self.control("tick")
        self.assertEqual(self.inference_counts(), request_count)
        stable = markdown.read_bytes(), self.state()["decisions"], self.state()["maintenance_notices"]
        for _ in range(2):
            self.control("tick")
            self.assertEqual((markdown.read_bytes(), self.state()["decisions"], self.state()["maintenance_notices"]), stable)
        final = self.state()
        self.assertEqual(len(self.remote()["issues"]), 1)
        self.assertNotIn("r", {row["canonical"] for row in final["symbols"]})
        self.assertEqual((self.root / "main.tex").read_bytes(), source)
        self.capture("summaries", {"transport": transport, "rejected_proposal": proposed,
                     "final": final, "remote": self.remote(), "model_requests": request_count})

    def test_github_persistence_failure_then_git_upload_failure_recover_one_authority(self):
        topic_id, proposals, batch = self.publish_batch()
        (self.work / "fail-github-mirror").touch()
        self.remote_reply(batch, "Approve $z$.", id=8300)
        self.control("tick")
        self.assertEqual(next(row for row in self.state()["proposals"] if row["canonical"] == "z")["status"], "pending")
        original = (self.root / "main.tex").read_bytes()
        self.held_use("codex" if NATIVE_CLI else "fixture", "z")
        self.assertEqual((self.root / "main.tex").read_bytes(), original)
        (self.work / "fail-github-mirror").unlink()
        count = len(self.requests())
        self.control("tick")
        self.assertEqual(len(self.requests()), count)
        self.known_use("deepcode" if NATIVE_CLI else "fixture", "z")
        self.sync_control("reject_upload", value=True)
        self.sync(True)
        # A rejected upload retains all five GitSync attempts and their retry
        # waits. Give only synchronous Git operations the transport budget.
        code, failed_sync = self.request("/api/git/sync", {
            "project_key": self.key, "action": "commit"}, timeout=30)
        self.assertEqual(code, 200, failed_sync)
        failed = {**self.transport(), "sync": failed_sync}
        self.assertIsNotNone(failed["sync"]["error"])
        saved = self.state()
        self.assertEqual(next(row for row in saved["proposals"] if row["canonical"] == "z")["status"], "approved")
        self.sync_control("reject_upload", value=False)
        code, retried_sync = self.request("/api/git/sync", {
            "project_key": self.key, "action": "commit"}, timeout=30)
        self.assertEqual(code, 200, retried_sync)
        retried = {**self.transport(), "sync": retried_sync}
        self.assertEqual(retried["receiver_head"], retried["remote_head"])
        self.assertIsNone(retried["sync"]["error"])
        self.control("tick"); self.control("tick")
        self.assertEqual(self.state()["decisions"], saved["decisions"])
        self.assertEqual(len(self.remote()["issues"]), 1)
        self.assertEqual(len(self.state()["github_discussion"]["proposal_batches"]), 1)
        self.capture("failure-recovery", {"topic": topic_id, "proposals": proposals,
                     "failed": failed, "retried": retried, "final": self.state()})

    def test_actual_clone_keeps_received_topic_identity_and_exact_prefix_without_borrowing_authority(self):
        self.initialize(); self.configure(); self.open_page()
        self.author("Use $h$ for $x+x$.")
        events = self.record([topics.entry("idea", "Compare $h$ with $x$.")])
        notice = next(row for row in events if row["t"] == "research_record")
        topic_id, relative = notice["topic_id"], notice["path"]
        prefix = (self.root / relative).read_bytes()
        original_topic = self.state()["discussion"]["topics"][0]
        self.control("tick")
        original_issue = self.remote()["issues"][0]
        original_adoption = self.state()["adoptions"][0]
        code, uploaded = self.request("/api/git/sync", {
            "project_key": self.key, "action": "commit"}, timeout=30)
        self.assertEqual(code, 200, uploaded)
        self.assertIsNone(uploaded["error"])
        original_record = (self.root / "research/knowledge.json").read_bytes()
        other = type(self)("runTest")
        other.work, other.process = self.work / "received-clone", None
        other.work.mkdir()
        try:
            other.start(received_origin=self.work / "remote.git", remote_file=self.work / "github-remote.json")
            self.assertNotEqual(self.key, other.key)
            self.assertEqual((other.root / relative).read_bytes(), prefix)
            self.assertEqual((other.root / "research/knowledge.json").read_bytes(), original_record)
            foreign = other.state()
            self.assertFalse(foreign["initialized"])
            self.assertEqual(foreign["provenance"]["record_state"], "foreign")
            other.initialize()
            initialized = other.state()
            self.assertEqual((other.root / relative).read_bytes(), prefix)
            reference = next(row for row in initialized["discussion_records"]["topics"] if row["id"] == topic_id)
            self.assertEqual(reference["source_state"], "unverified_received_reference")
            self.assertTrue(reference["source_warning"])
            self.assertEqual(initialized["github_discussion"]["mode"], "markdown")
            self.assertEqual(initialized["adoptions"], [])
            self.assertNotIn("h", {row["canonical"] for row in initialized["symbols"]})
            original_source = (other.root / "main.tex").read_bytes()
            suffix = "Compare $x$ with itself."
            continued = other.record([topics.entry("idea", suffix)], title="Direct proof")
            notice2 = next(row for row in continued if row["t"] == "research_record")
            self.assertEqual(notice2["topic_id"], topic_id)
            self.assertEqual(notice2["path"], relative)
            combined = (other.root / relative).read_bytes()
            self.assertTrue(combined.startswith(prefix))
            self.assertEqual(combined.decode().count(suffix), 1)
            self.assertEqual(len(list((other.root / "research/topics").glob("*.md"))), 1)
            state = other.state()
            self.assertEqual(next(row for row in state["discussion_records"]["topics"]
                                  if row["id"] == topic_id)["source_state"], "unverified_received_reference")
            local_topic = next(row for row in state["discussion"]["topics"] if row["id"] == topic_id)
            self.assertEqual([row["text"] for row in local_topic["entries"]], [suffix])
            self.assertTrue(all(row["provenance"]["project_key"] == other.key for row in local_topic["entries"]))
            self.assertTrue(all(row["id"] not in {old["id"] for old in original_topic["entries"]}
                                for row in local_topic["entries"]))
            other.stop(); other.start()
            replay = other.record([topics.entry("idea", suffix)], title="Direct proof", topic_id=topic_id)
            self.assertFalse(any(row["t"] == "research_record" for row in replay))
            self.assertEqual((other.root / relative).read_bytes(), combined)
            self.assertEqual((other.root / "main.tex").read_bytes(), original_source)
            self.assertEqual(other.state()["adoptions"], [])
            self.assertNotIn("h", {row["canonical"] for row in other.state()["symbols"]})
            other.open_page()
            other.configure()
            before_publish = other.inference_counts()
            remote_before = other.remote()
            other.control("tick")
            blocked_remote = other.remote()
            self.assertEqual(len(blocked_remote["issues"]), 1)
            self.assertEqual(blocked_remote["issues"][0]["id"], original_issue["id"])
            self.assertEqual(blocked_remote["comments"], remote_before["comments"])
            self.assertEqual(other.inference_counts(), before_publish)
            self.assertEqual((other.root / relative).read_bytes(), combined)
            self.assertEqual((other.root / "main.tex").read_bytes(), original_source)
            self.assertEqual(other.state()["adoptions"], [])
            self.capture("clone-before-own-adoption", {"state": other.state(), "remote": blocked_remote,
                         "human_text": "Use $h$ for $x+x$."})
            other.author("Use $h$ for $x+x$.")
            local_adoption = other.state()["adoptions"][0]
            self.assertEqual(local_adoption["project_key"], other.key)
            self.assertNotEqual(local_adoption["id"], original_adoption["id"])
            own_calls = other.inference_counts()
            other.control("tick")
            published_remote = other.remote()
            self.assertEqual(len(published_remote["issues"]), 1)
            self.assertEqual(published_remote["issues"][0]["id"], original_issue["id"])
            comments = [row for rows in published_remote["comments"].values() for row in rows]
            prior_comments = [row for rows in remote_before["comments"].values() for row in rows]
            self.assertEqual(len(comments), len(prior_comments) + 1)
            self.assertIn(suffix, comments[-1]["body"])
            self.assertEqual(other.inference_counts(), own_calls)
            other.stop(); other.start(); other.open_page(); other.control("tick")
            self.assertEqual(other.remote()["issues"], published_remote["issues"])
            self.assertEqual(other.remote()["comments"], published_remote["comments"])
            self.assertEqual((other.root / relative).read_bytes(), combined)
            self.assertEqual((other.root / "main.tex").read_bytes(), original_source)
            self.capture("clone-continuity", {"original_project": self.key, "received_project": other.key,
                         "topic": topic_id, "path": relative, "received_sha256": notice["sha256"],
                         "exact_received_prefix": prefix.decode(), "continued": combined.decode(),
                         "initialized": initialized, "blocked_publication": blocked_remote,
                         "published_same_issue": published_remote, "local_adoption": local_adoption,
                         "final": other.state()})
        finally:
            other.stop()


if __name__ == "__main__":
    unittest.main()
