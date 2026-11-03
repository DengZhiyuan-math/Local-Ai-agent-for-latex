"""Automatic topic records through editor HTTP and controlled API output."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agent import call_chunks
import test_research_http as base


def entry(kind, text, source="assistant", quote=""):
    return {"kind": kind, "text": text, "source": source, "quote": quote}


class ResearchTopicsHTTP(unittest.TestCase):
    setUp = base.ResearchHTTP.setUp
    tearDown = base.ResearchHTTP.tearDown
    stop = base.ResearchHTTP.stop
    request = base.ResearchHTTP.request
    initialize = base.ResearchHTTP.initialize
    events = base.ResearchHTTP.events
    finish = base.ResearchHTTP.finish
    requests = base.ResearchHTTP.requests

    def start(self, empty=False):
        (self.work / "ready.json").unlink(missing_ok=True)
        fixture = Path(__file__).parent / "browser/research_topics_fixture.py"
        self.log = (self.work / "server.log").open("ab")
        command = [sys.executable, str(fixture), str(self.work)] + (["--empty"] if empty else [])
        if getattr(self, "shared_state", None) is not None:
            command += ["--state-dir", str(self.shared_state)]
        self.process = subprocess.Popen(command, stdout=self.log, stderr=self.log)
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

    def turn(self, script, human_text="Maybe compare the direct proof.", session=None, **extra):
        (self.work / "api-script.json").write_text(json.dumps(script), encoding="utf-8")
        code, result = self.request("/api/agent", {
            "project_key": self.key, "provider": "fixture", "model": "fixture-model",
            "mode": "ask", "prompt": human_text, "human_text": human_text,
            "session_id": session, "active_file": "main.tex", **extra})
        self.assertEqual(code, 200, result)
        return result["job"]

    def record(self, entries, human_text="Maybe compare the direct proof.", session=None,
               title="Direct proof", topic_id=None, related_topics=None, **extra):
        payload = {"title": title, "entries": entries}
        if topic_id is not None:
            payload["topic_id"] = topic_id
        if related_topics is not None:
            payload["related_topics"] = related_topics
        script = [call_chunks("topic", "record_research_topic", payload),
                  [base.say("The discussion remains tentative.")]]
        return self.finish(self.turn(script, human_text, session, **extra))

    def records(self):
        code, state = self.request("/api/research")
        self.assertEqual(code, 200, state)
        return state["discussion_records"]

    def test_tentative_idea_is_saved_automatically_without_manuscript_edits(self):
        self.initialize()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*.tex")}
        events = self.record([entry("idea", "Compare the direct proof before choosing a method.")])
        notices = [event for event in events if event["t"] == "research_record"]
        self.assertEqual(len(notices), 1, events)
        notice = notices[0]
        self.assertTrue(notice["path"].startswith("research/topics/"), notice)
        saved = self.root / notice["path"]
        self.assertIn("Compare the direct proof before choosing a method.", saved.read_text())
        self.assertEqual(notice["sha256"], hashlib.sha256(saved.read_bytes()).hexdigest())
        self.assertEqual(self.records()["topics"][0]["id"], notice["topic_id"])
        self.assertEqual({p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*.tex")}, before)
        self.assertEqual(next(event for event in events if event["t"] == "done")["changed"], [])
        self.assertEqual(len(self.requests()), 2, "only the scripted conversation runs; no research task is started")

    def test_one_topic_continues_across_conversations_models_providers_and_restart(self):
        self.initialize()
        first = self.record([entry("idea", "Compare the direct proof before choosing a method.")])
        notice = next(event for event in first if event["t"] == "research_record")
        choice = "We decided to use the direct proof."
        second = self.record([entry("decision", choice, "human", choice)],
            human_text=choice, topic_id=notice["topic_id"], provider="fixture-2", model="fixture-model-2")
        second_notice = next(event for event in second if event["t"] == "research_record")
        self.assertEqual(second_notice["topic_id"], notice["topic_id"])
        self.assertEqual(second_notice["path"], notice["path"])
        self.assertEqual(second_notice["revision"], 2)
        self.stop(); self.start()
        question = "Which boundary case remains open?"
        third = self.record([entry("question", question, "human", question)], human_text=question,
                            topic_id=notice["topic_id"])
        third_notice = next(event for event in third if event["t"] == "research_record")
        self.assertEqual(third_notice["revision"], 3)
        topics = self.records()["topics"]
        self.assertEqual(len(topics), 1, topics)
        self.assertEqual(topics[0]["id"], notice["topic_id"])
        self.assertEqual(len(list((self.root / "research/topics").glob("*.md"))), 1)
        text = (self.root / notice["path"]).read_text()
        ideas, decisions = text.split("## Confirmed decisions", 1)
        decisions, questions = decisions.split("## Open questions", 1)
        self.assertIn("Compare the direct proof before choosing a method.", ideas)
        self.assertIn(choice, decisions)
        self.assertIn("provider fixture-2", decisions)
        self.assertIn(question, questions)
        state = self.request("/api/research")[1]
        self.assertEqual(state["decisions"], [], "topic decisions are not notation adoption receipts")

    def test_unchanged_topic_has_no_write_or_saved_notice_and_status_starts_no_model(self):
        self.initialize()
        items = [entry("idea", "Compare the direct proof before choosing a method.")]
        first = self.record(items)
        notice = next(event for event in first if event["t"] == "research_record")
        paths = [self.root / notice["path"], self.root / "research/knowledge.json"]
        before = [(path.stat().st_mtime_ns, path.read_bytes()) for path in paths]
        repeated = self.record(items, topic_id=notice["topic_id"])
        self.assertFalse(any(event["t"] == "research_record" for event in repeated), repeated)
        self.assertEqual([(path.stat().st_mtime_ns, path.read_bytes()) for path in paths], before)
        self.assertEqual(len(self.requests()), 4, "two ordinary scripted Turns need no extra summary generation")
        count = len(self.requests())
        state = self.records()
        self.assertEqual(self.records(), state)
        self.stop(); self.start()
        self.assertEqual(self.records(), state)
        self.assertEqual(len(self.requests()), count, "status/reopening must not start a summary task")
        self.assertEqual([(path.stat().st_mtime_ns, path.read_bytes()) for path in paths], before)

    def test_failed_markdown_or_index_save_recovers_after_restart_without_a_new_model(self):
        self.initialize()
        for index, marker in enumerate(("fail-topic-markdown", "fail-topic-index", "fail-topic-final-index")):
            with self.subTest(marker=marker):
                (self.work / marker).touch()
                events = self.record([entry("idea", "Compare the direct argument before committing to it.")],
                                     title=f"Recovery route {index}")
                self.assertFalse(any(event["t"] == "research_record" for event in events), events)
                errors = [event for event in events if event["t"] == "research_record_error"]
                self.assertEqual(len(errors), 1, events)
                self.assertTrue(errors[0]["retryable"])
                self.assertEqual(self.records()["pending_count"], 1)
                path = self.root / errors[0]["path"]
                self.assertEqual(path.exists(), marker != "fail-topic-markdown")
                request_count = len(self.requests())
                self.stop(); self.start()
                (self.work / marker).unlink()
                recovered = self.records()
                self.assertEqual(recovered["pending_count"], 0, recovered)
                self.assertEqual(len(recovered["topics"]), index + 1)
                topic = next(topic for topic in recovered["topics"] if topic["path"] == errors[0]["path"])
                self.assertEqual(topic["revision"], 1)
                self.assertIn("Compare the direct argument before committing to it.", path.read_text())
                saved = path.stat().st_mtime_ns, path.read_bytes()
                self.assertEqual(self.records(), recovered)
                self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), saved)
                self.assertEqual(len(self.requests()), request_count)

    def test_ai_assembled_prompt_quotation_and_tentative_text_cannot_confirm_a_decision(self):
        self.initialize()
        choice = "We decided to use the direct proof."
        cases = (
            ("assistant", choice, choice, {}),
            ("human", choice, None, {"prompt": choice}),
            ("human", "> " + choice, "> " + choice, {}),
            ("human", "Maybe we could use the direct proof.", "Maybe we could use the direct proof.", {}),
        )
        for source, quote, human_text, extra in cases:
            with self.subTest(source=source, human_text=human_text):
                events = self.record([entry("decision", "Use the direct proof.", source, quote)],
                                     human_text=human_text, **extra)
                self.assertTrue(any(event["t"] == "tool_result" and event["error"] for event in events), events)
                self.assertFalse(any(event["t"] == "research_record" for event in events), events)
                self.assertEqual(self.records()["topics"], [])
        state = self.request("/api/research")[1]
        self.assertEqual(state["decisions"], [])
        self.assertEqual(state["adoptions"], [])

    def test_unknown_summary_notation_is_held_before_a_topic_document_is_written(self):
        self.initialize()
        original = (self.root / "main.tex").read_bytes()
        events = self.record([entry("idea", "A possible estimate uses $z$.")])
        self.assertFalse(any(event["t"] == "research_record" for event in events), events)
        self.assertEqual(self.records()["topics"], [])
        self.assertFalse(list((self.root / "research").rglob("*.md")))
        state = self.request("/api/research")[1]
        self.assertTrue(any(proposal["canonical"] == "z" and proposal["status"] == "pending"
                            for proposal in state["proposals"]))
        self.assertNotIn("z", {symbol["canonical"] for symbol in state["symbols"]})
        self.assertEqual((self.root / "main.tex").read_bytes(), original)
        self.assertNotIn("$z", "".join(event.get("text", "") for event in events
                                      if event["t"] in ("delta", "text")))

    def test_topic_summary_uses_current_family_and_expression_refusal_rules(self):
        self.initialize()
        self.finish(self.turn([[base.say("The author supplied an indexed family.")]],
                              human_text="Use $a_i$ for $x+x$ with i in {1,2,3}."))
        events = self.record([entry("idea", "The known member $a_2$ may be useful.")])
        notice = next(event for event in events if event["t"] == "research_record")
        path = self.root / notice["path"]
        original = path.read_bytes()
        blocked = self.record([entry("idea", "Try the out-of-family member $a_4$.")], topic_id=notice["topic_id"])
        self.assertTrue(any(event["t"] == "notation_proposal" for event in blocked))
        self.assertFalse(any(event["t"] == "research_record" for event in blocked))
        self.assertEqual(path.read_bytes(), original)
        self.finish(self.turn([[base.say("Keep the expression expanded.")]],
                              human_text="保持 $x+x$ 展开，不要简称。"))
        refused = self.record([entry("idea", "The abbreviation is $x=x+x$.")], topic_id=notice["topic_id"])
        self.assertFalse(any(event["t"] == "research_record" for event in refused))
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn("$x=x+x$", "".join(event.get("text", "") for event in refused
                                             if event["t"] in ("delta", "text")))

    def test_manual_record_edit_is_preserved_and_automatic_update_remains_pending(self):
        self.initialize()
        events = self.record([entry("idea", "Compare the direct proof.")])
        notice = next(event for event in events if event["t"] == "research_record")
        code, source = self.request("/api/file?" + urllib.parse.urlencode({"path": notice["path"]}))
        self.assertEqual(code, 200, source)
        manual = source["content"] + "\nAuthor annotation: keep this unresolved.\n"
        code, result = self.request("/api/file", {"path": notice["path"], "content": manual,
            "base_mtime": source["mtime"]})
        self.assertEqual(code, 200, result)
        pending = self.record([entry("question", "Which hypothesis needs checking?")], topic_id=notice["topic_id"])
        self.assertFalse(any(event["t"] == "research_record" for event in pending), pending)
        self.assertTrue(any(event["t"] == "research_record_error" for event in pending), pending)
        self.assertEqual((self.root / notice["path"]).read_text(), manual)
        self.assertEqual(self.records()["pending_count"], 1)
        self.stop(); self.start()
        state = self.records()
        self.assertEqual(state["pending_count"], 1)
        self.assertTrue(state["errors"])
        self.assertEqual(state["topics"][0]["revision"], 1)
        self.assertEqual((self.root / notice["path"]).read_text(), manual)

    def test_stopped_turn_cannot_materialize_its_staged_topic_on_later_status_reads(self):
        self.initialize()
        payload = {"title": "Interrupted proof discussion", "entries": [entry("idea", "Try another estimate.")]}
        job = self.turn([call_chunks("topic", "record_research_topic", payload),
                         [1.5, base.say("The tentative discussion is complete.")]])
        observed = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.events(job, len(observed), wait=.1)
            observed.extend(result["events"])
            if any(event["t"] == "tool_result" for event in observed):
                break
        else:
            self.fail("topic draft was not staged")
        code, stopped = self.request("/api/agent/stop", {"project_key": self.key, "job": job})
        self.assertEqual(code, 200, stopped)
        events = self.finish(job)
        self.assertFalse(any(event["t"] == "research_record" for event in events), events)
        self.assertEqual(next(event for event in events if event["t"] == "done")["outcome"], "stopped")
        self.assertEqual(self.records()["topics"], [])
        self.stop(); self.start()
        self.assertEqual(self.records()["topics"], [])
        self.assertFalse(list((self.root / "research").rglob("*.md")))

    def test_related_topics_and_pending_sources_do_not_leak_to_another_project(self):
        self.initialize()
        first = next(event for event in self.record([entry("idea", "Compare direct estimates.")])
                     if event["t"] == "research_record")
        second = next(event for event in self.record([entry("question", "Which endpoint needs attention?")],
            title="Endpoint question", related_topics=[first["topic_id"]]) if event["t"] == "research_record")
        second_path = self.root / second["path"]
        self.assertIn(first["topic_id"] + ".md", second_path.read_text())
        prior_work = self.work
        prior_file = self.root / first["path"]
        prior_bytes = prior_file.read_bytes()
        (self.work / "fail-topic-markdown").touch()
        pending = self.record([entry("question", "Which estimate remains unresolved?")], title="Pending estimate")
        pending_notice = next(event for event in pending if event["t"] == "research_record_error")
        pending_path = self.root / pending_notice["path"]
        self.assertEqual(self.records()["pending_count"], 1)
        self.assertFalse(pending_path.exists())
        self.stop()
        self.shared_state = prior_work / "state"
        self.work = prior_work / "another-editor"
        self.work.mkdir()
        self.start()
        self.initialize()
        self.assertEqual(self.records()["topics"], [])
        self.assertEqual(self.records()["pending_count"], 0)
        events = self.record([entry("idea", "Try the current project's estimate.")],
                             topic_id=first["topic_id"])
        self.assertFalse(any(event["t"] == "research_record" for event in events), events)
        self.assertTrue(any(event["t"] == "tool_result" and event["error"] for event in events), events)
        foreign = self.record([entry("question", "Which endpoint is relevant here?")],
                              related_topics=[first["topic_id"]])
        self.assertFalse(any(event["t"] == "research_record" for event in foreign), foreign)
        current = self.record([entry("idea", "Try the current project's estimate.")])
        current_notice = next(event for event in current if event["t"] == "research_record")
        self.assertNotEqual(current_notice["topic_id"], first["topic_id"])
        self.assertEqual(prior_file.read_bytes(), prior_bytes)
        self.assertFalse(pending_path.exists(), "another project must not materialize the first project's pending topic")


if __name__ == "__main__":
    unittest.main()
