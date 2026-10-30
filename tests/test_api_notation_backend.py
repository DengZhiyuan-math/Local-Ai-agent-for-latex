"""The API adapter's notation contract through a local streamed API fixture.

The access double isolates adapter ordering/protocol behavior. Research policy and
durability are covered by the separate shared-policy and editor HTTP tests.
"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend_openai import OpenAICompat
from test_agent import FakeAPI, job_for
import research_access
from agent import outcome
from project import Project
from research import RECORD, ResearchKnowledge


def say(text, **extra):
    return {"choices": [{"delta": {"content": text, **extra}}]}


def call(*items, text=""):
    return {"choices": [{"delta": {"content": text, "tool_calls": [
        {"index": i, "id": cid, "function": {"name": name, "arguments": json.dumps(args)}}
        for i, (cid, name, args) in enumerate(items)]}}]}


PROPOSAL = {"notation": "z", "definition": "the unchanged original expression",
            "scope": "this derivation", "reason": "A candidate abbreviation for discussion",
            "source_expression": r"-\rho+2\ell-a+b-c+d-e"}


class AccessDouble:
    initialized = True

    def __init__(self, project):
        self.knowledge = SimpleNamespace(project=project)
        self.saved, self.applied = [], []
        self.compiles = 0
        self.fail_save = False

    def context(self):
        return "[Prism research baseline]\n{}"

    def propose(self, args):
        if self.fail_save:
            raise OSError("isolated persistence unavailable")
        if args.get("notation") == "x":
            raise ValueError("Known notation needs no proposal.")
        if not all(args.get(k) for k in ("notation", "definition", "scope", "reason")):
            raise ValueError("Incomplete notation proposal.")
        proposal = {"id": "proposal-1", "status": "pending", "revision": 1, **args}
        self.saved.append(proposal)
        return proposal

    def review_output(self, text, channel="chat"):
        if "$z" in text:
            proposal = self.propose(PROPOSAL)
            raise research_access.NotationStop(proposals=[proposal])
        return text

    def review_auxiliary(self, text, channel):
        return "Mathematical preview withheld." if "$z" in text or "DO_NOT_SHOW" in text else text

    def review_source_preview(self, source, preview, *, latex=True):
        checked = self.review_auxiliary(source, "tool")
        return preview if checked == source else checked

    def apply_write(self, path, text, expected):
        self.review_output(text, channel="manuscript")
        if path.read_bytes() != expected:
            raise ValueError("The source changed before application.")
        self.applied.append((path, text, expected))
        path.write_text(text)

    def guarded_compile(self, *, clean=False, cancel=None):
        self.compiles += 1
        return {"exit": 1, "diagnostics": [{"severity": "error", "message": "DO_NOT_SHOW $z$"}]}


class APINotationContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "main.tex").write_text("$x$")
        (self.root / "reference.tex").write_text("Reference $z$.")
        self.access = AccessDouble(Project(self.root))

    def setup_turn(self, script, access=True):
        api = FakeAPI(script)
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        backend = OpenAICompat("fixture", {"base_url": f"http://127.0.0.1:{api.server_port}",
                                           "models": ["fixture-model"]})
        import snapshot
        before, _issues = snapshot.take(self.root)
        job = job_for(root=self.root, mode="edit", model="fixture-model", prompt="Discuss the source.", before=before,
                      server_url="http://127.0.0.1:1/", writable=lambda rel: True,
                      files=lambda: ["main.tex", "reference.tex"],
                      research_access=self.access if access else None)
        return backend, job, api

    def assert_protocol(self, messages):
        calls = [c["id"] for msg in messages for c in msg.get("tool_calls", [])]
        replies = [msg["tool_call_id"] for msg in messages if msg["role"] == "tool"]
        self.assertCountEqual(calls, replies)

    def test_proposal_precedes_sibling_write_and_compile_and_discards_raw_text(self):
        backend, job, api = self.setup_turn([[call(
            ("write", "write_file", {"path": "main.tex", "content": "$x+x$"}),
            ("proposal", "propose_notation", {**PROPOSAL, "approved": True}),
            ("compile", "compile", {}), text="Use $z$ now.")]])
        result = backend.run(job)
        self.assertTrue(result["notation_pending"])
        self.assertEqual((self.root / "main.tex").read_text(), "$x$")
        self.assertEqual(self.access.applied, [])
        self.assertEqual(self.access.compiles, 0)
        self.assertEqual(len(api.requests), 1)
        self.assertEqual(len(self.access.saved), 1)
        self.assertNotIn("approved", self.access.saved[0])
        self.assertEqual([e["t"] for e in job.events].count("notation_proposal"), 1)
        self.assertFalse(any(e["t"] in ("delta", "thinking", "permission") for e in job.events))
        messages = backend.sessions[result["session_id"]]
        self.assert_protocol(messages)
        self.assertFalse(any("Use $z$" in str(m.get("content")) for m in messages))

    def test_fragmented_chat_is_not_published_before_full_review(self):
        backend, job, api = self.setup_turn([[say("Use $"), .25, say("z"), say("$ now.")]])
        result = {}
        run = threading.Thread(target=lambda: result.update(backend.run(job)))
        run.start()
        deadline = time.monotonic() + 2
        while not api.requests and time.monotonic() < deadline:
            time.sleep(.01)
        time.sleep(.05)
        self.assertFalse(any(e["t"] in ("delta", "thinking", "notation_proposal") for e in job.events))
        self.assertEqual((self.root / "main.tex").read_text(), "$x$")
        run.join(3)
        self.assertFalse(run.is_alive())
        self.assertTrue(result["notation_pending"])
        self.assertFalse(any(e["t"] == "delta" for e in job.events))
        self.assertEqual(len(self.access.saved), 1)

    def test_blocked_write_stops_remaining_tools_and_does_not_publish_success_text(self):
        backend, job, api = self.setup_turn([[call(
            ("write", "write_file", {"path": "main.tex", "content": "$z$"}),
            ("compile", "compile", {}), text="The file has been changed.")]])
        result = backend.run(job)
        self.assertTrue(result["notation_pending"])
        self.assertEqual((self.root / "main.tex").read_text(), "$x$")
        self.assertEqual(self.access.compiles, 0)
        self.assertEqual(len(api.requests), 1)
        self.assertFalse(any(e["t"] == "delta" for e in job.events))
        self.assert_protocol(backend.sessions[result["session_id"]])

    def test_known_math_uses_shared_application_and_remains_available(self):
        backend, job, api = self.setup_turn([[call(("edit", "edit_file", {
            "path": "main.tex", "old_string": "$x$", "new_string": "$x+x$"}))], [say("Known $x+x$.")]])
        result = backend.run(job)
        self.assertFalse(result.get("is_error"))
        self.assertEqual((self.root / "main.tex").read_text(), "$x+x$")
        self.assertEqual(self.access.applied[0][2], b"$x$")
        self.assertEqual("".join(e["text"] for e in job.events if e["t"] == "delta"), "Known $x+x$.")
        names = [t["function"]["name"] for t in api.requests[0]["body"]["tools"]]
        self.assertIn("propose_notation", names)
        self.assertFalse(backend.info()["notation_protection"])
        self.assertTrue(backend.info()["notation_guard"]["decisions"])

    def test_reference_preview_is_filtered_without_requesting_adoption(self):
        backend, job, _ = self.setup_turn([[call(("read", "read_file", {"path": "reference.tex"}))],
                                          [say("Known $x$ remains available.")]])
        result = backend.run(job)
        preview = next(e["preview"] for e in job.events if e["t"] == "tool_result")
        self.assertNotIn("$z$", preview)
        self.assertEqual(self.access.saved, [])
        self.assertFalse(result.get("notation_pending"))

    def test_partial_reference_previews_use_full_source_context_and_keep_raw_model_input(self):
        source = "\\[\n\\ell_+ + x\n\\]\n"
        (self.root / "reference.tex").write_text(source)
        knowledge = ResearchKnowledge(Project(self.root))
        baseline = knowledge.initialize()
        self.assertEqual([s["notation"] for s in baseline["symbols"]], ["x"])
        self.access = research_access.ResearchAccess(knowledge)
        for name, args in (("read_file", {"path": "reference.tex", "offset": 2, "limit": 1}),
                           ("search", {"pattern": "ell", "path": "reference.tex"})):
            with self.subTest(tool=name):
                backend, job, api = self.setup_turn([[call(("reference", name, args))], [say("Known $x$.")]])
                result = backend.run(job)
                preview = next(e["preview"] for e in job.events if e["t"] == "tool_result")
                self.assertNotIn(r"\ell_+", preview)
                self.assertIn("withheld", preview)
                raw = next(m["content"] for m in api.requests[1]["body"]["messages"] if m["role"] == "tool")
                self.assertIn(r"\ell_+ + x", raw)
                self.assertFalse(result.get("notation_pending"))
                self.assertFalse(any(e["t"] == "notation_proposal" for e in job.events))
                self.assertEqual(json.loads((self.root / RECORD).read_text()).get("proposals", []), [])
                self.assertEqual((self.root / "reference.tex").read_text(), source)

    def test_partial_known_source_preserves_preview_and_navigation(self):
        (self.root / "main.tex").write_text("\\[\nx+x\n\\]\n")
        knowledge = ResearchKnowledge(Project(self.root))
        knowledge.initialize()
        self.access = research_access.ResearchAccess(knowledge)
        backend, job, api = self.setup_turn([[call(("read", "read_file", {"path": "main.tex", "offset": 2, "limit": 1}))],
                                           [say("Known $x$.")]])
        backend.run(job)
        event = next(e for e in job.events if e["t"] == "tool")
        preview = next(e["preview"] for e in job.events if e["t"] == "tool_result")
        self.assertEqual((event["path"], event["lines"]), ("main.tex", [2, 2]))
        self.assertEqual(preview, "     2\tx+x")
        raw = next(m["content"] for m in api.requests[1]["body"]["messages"] if m["role"] == "tool")
        self.assertEqual(raw, preview)

    def test_source_preview_checker_failure_hides_preview_without_an_adoption_request(self):
        backend, job, api = self.setup_turn([[call(("read", "read_file", {"path": "reference.tex"}))],
                                           [say("Known $x$.")]])
        self.access.review_source_preview = mock.Mock(side_effect=ValueError(r"Unsafe bare \ell_+"))
        result = backend.run(job)
        preview = next(e["preview"] for e in job.events if e["t"] == "tool_result")
        self.assertIn("source could not be checked", preview)
        self.assertNotIn(r"\ell_+", preview)
        raw = next(m["content"] for m in api.requests[1]["body"]["messages"] if m["role"] == "tool")
        self.assertIn("$z$", raw)
        self.assertFalse(result.get("notation_pending"))
        self.assertEqual(self.access.saved, [])

    def test_thinking_and_tool_preview_are_checked_but_provider_reasoning_protocol_survives(self):
        backend, job, api = self.setup_turn([[say("", reasoning_content="DO_NOT_SHOW $z$"),
                                           call(("read", "read_file", {"path": "main.tex"}))],
                                          [say("Known $x$.")]])
        result = backend.run(job)
        self.assertFalse(any("DO_NOT_SHOW" in e.get("text", "") for e in job.events))
        prior = next(m for m in api.requests[1]["body"]["messages"] if m.get("tool_calls"))
        self.assertEqual(prior["reasoning_content"], "DO_NOT_SHOW $z$")
        self.assertFalse(any("reasoning_content" in m for m in backend.sessions[result["session_id"]]))

    def test_proposal_persistence_failure_stops_without_success_or_write(self):
        self.access.fail_save = True
        backend, job, api = self.setup_turn([[call(("proposal", "propose_notation", PROPOSAL),
                                                  ("write", "write_file", {"path": "main.tex", "content": "$x+x$"}))]])
        result = backend.run(job)
        self.assertTrue(result["is_error"])
        self.assertEqual(len(api.requests), 1)
        self.assertEqual((self.root / "main.tex").read_text(), "$x$")
        self.assertFalse(any(e["t"] == "notation_proposal" for e in job.events))
        self.assertTrue(any(e["t"] == "error" for e in job.events))
        self.assert_protocol(backend.sessions[result["session_id"]])

    def test_raw_chat_persistence_failure_is_held_and_does_not_run_tools(self):
        self.access.fail_save = True
        backend, job, api = self.setup_turn([[call(
            ("write", "write_file", {"path": "main.tex", "content": "$x+x$"}),
            text="Use $z$ now.")]])
        result = backend.run(job)
        self.assertTrue(result["is_error"])
        self.assertEqual(len(api.requests), 1)
        self.assertEqual((self.root / "main.tex").read_text(), "$x$")
        self.assertFalse(any(e["t"] in ("delta", "notation_proposal") for e in job.events))
        self.assert_protocol(backend.sessions[result["session_id"]])

    def test_tool_summary_is_filtered_without_a_new_adoption_proposal(self):
        backend, job, _ = self.setup_turn([[call(("search", "search", {"pattern": "$z$"}))],
                                         [say("Known $x$.")]])
        backend.run(job)
        self.assertEqual(next(e["summary"] for e in job.events if e["t"] == "tool"),
                         "Mathematical preview withheld.")
        self.assertEqual(self.access.saved, [])

    def test_bad_tool_arguments_remain_recoverable_with_the_guard(self):
        backend, job, api = self.setup_turn([[call(("search", "search", {"pattern": 123}))],
                                           [say("Known $x$.")]])
        result = backend.run(job)
        self.assertFalse(result.get("is_error"))
        self.assertEqual(len(api.requests), 2)
        self.assertTrue(next(e["error"] for e in job.events if e["t"] == "tool_result"))
        self.assert_protocol(backend.sessions[result["session_id"]])

    def test_preview_review_failure_stops_and_pairs_unexecuted_tools(self):
        backend, job, api = self.setup_turn([[call(("files", "list_files", {}),
                                                   ("write", "write_file", {"path": "main.tex", "content": "$x+x$"}))]])
        original = self.access.review_auxiliary
        def fail_preview(text, channel):
            if "main.tex\nreference.tex" in text:
                raise OSError("isolated preview review unavailable")
            return original(text, channel)
        self.access.review_auxiliary = fail_preview
        result = backend.run(job)
        self.assertTrue(result["is_error"])
        self.assertEqual(len(api.requests), 1)
        self.assertEqual((self.root / "main.tex").read_text(), "$x$")
        self.assertFalse(any("$z" in e.get("preview", "") for e in job.events))
        self.assertTrue(next(e["error"] for e in job.events if e["t"] == "tool_result"))
        self.assert_protocol(backend.sessions[result["session_id"]])

    def test_provider_errors_are_filtered_and_keep_failure_semantics(self):
        for response, expected in (((500, "DO_NOT_SHOW $z$"), "HTTP 500"),
                                   ([{"error": {"message": "DO_NOT_SHOW $z$"}}], "provider returned an error")):
            with self.subTest(response=response):
                backend, job, _ = self.setup_turn([response])
                result = backend.run(job)
                self.assertEqual((result["exit"], result["subtype"]), (1, "API error"))
                self.assertIn(expected, result["stderr"])
                self.assertNotIn("$z$", str(result))
                self.assertNotIn("DO_NOT_SHOW", str(result))
                self.assertEqual(outcome(job, result)[0], "failed")
                self.assertEqual(self.access.saved, [])
                self.assertFalse(any(e["t"] == "notation_proposal" for e in job.events))

    def test_plain_provider_error_details_remain_visible(self):
        backend, job, _ = self.setup_turn([(401, "bad key")])
        result = backend.run(job)
        self.assertIn("HTTP 401", result["stderr"])
        self.assertIn("bad key", result["stderr"])

    def test_context_length_error_keeps_its_classification_after_filtering(self):
        backend, job, _ = self.setup_turn([(400, "context window exceeded: DO_NOT_SHOW $z$")])
        result = backend.run(job)
        self.assertIn("too long for the model", result["error"])
        self.assertNotIn("$z$", result["stderr"])
        self.assertIn("HTTP 400", result["stderr"])

    def test_notation_storage_error_details_are_filtered_without_a_proposal(self):
        backend, job, _ = self.setup_turn([[call(("proposal", "propose_notation", PROPOSAL))]])
        self.access.propose = mock.Mock(side_effect=OSError("Disk failure: DO_NOT_SHOW $z$"))
        result = backend.run(job)
        self.assertEqual(result["subtype"], "notation review failed")
        error = next(e["message"] for e in job.events if e["t"] == "error")
        self.assertIn("Notation review failed", error)
        self.assertNotIn("$z$", error)
        self.assertNotIn("DO_NOT_SHOW", error)
        self.assertFalse(any(e["t"] == "notation_proposal" for e in job.events))

    def test_error_checker_failure_never_publishes_the_unchecked_provider_error(self):
        backend, job, _ = self.setup_turn([(500, "DO_NOT_SHOW $z$")])
        self.access.review_auxiliary = mock.Mock(side_effect=ValueError("Cannot check $z$"))
        result = backend.run(job)
        self.assertIn("HTTP 500", result["stderr"])
        self.assertIn("could not be checked", result["stderr"])
        self.assertNotIn("$z$", str(result))

    def test_unknown_tool_label_is_filtered_without_a_proposal(self):
        backend, job, _ = self.setup_turn([[call(("unknown", "DO_NOT_SHOW $z$", {}))], [say("Known $x$.")]])
        result = backend.run(job)
        event = next(e for e in job.events if e["t"] == "tool")
        self.assertEqual(event["name"], "Unknown tool")
        self.assertFalse(any("$z$" in str(e) or "DO_NOT_SHOW" in str(e) for e in job.events))
        self.assertEqual(self.access.saved, [])
        self.assert_protocol(backend.sessions[result["session_id"]])

    def test_tool_paths_keep_checked_navigation_and_omit_unverified_math(self):
        for name, visible in (("part$x$.tex", True), ("part$z$.tex", False)):
            with self.subTest(name=name):
                (self.root / name).write_text("Known $x$.\nSecond line.")
                backend, job, _ = self.setup_turn([[call(("read", "read_file", {"path": name, "offset": 1, "limit": 2}))],
                                                 [say("Known $x$.")]])
                backend.run(job)
                event = next(e for e in job.events if e["t"] == "tool")
                if visible:
                    self.assertEqual(event["path"], name)
                    self.assertEqual(event["lines"], [1, 2])
                else:
                    self.assertNotIn("path", event)
                    self.assertNotIn("lines", event)
                    self.assertNotIn("$z$", event["summary"])
                self.assertEqual(self.access.saved, [])

    def test_tool_path_check_uses_the_resolved_navigation_target(self):
        target = self.root / "part$z$.tex"
        target.write_text("Known $x$.")
        try:
            (self.root / "alias.tex").symlink_to(target)
        except OSError:
            self.skipTest("symbolic links are unavailable")
        backend, job, _ = self.setup_turn([[call(("read", "read_file", {"path": "alias.tex"}))],
                                         [say("Known $x$.")]])
        backend.run(job)
        event = next(e for e in job.events if e["t"] == "tool")
        self.assertEqual(event["summary"], "alias.tex")
        self.assertNotIn("path", event)
        self.assertEqual(self.access.saved, [])

    def test_stop_discards_unchecked_partial_output_and_never_runs_tools(self):
        backend, job, api = self.setup_turn([[say("Use $z"), .3,
                                           call(("write", "write_file", {"path": "main.tex", "content": "$x+x$"}))]])
        result = {}
        run = threading.Thread(target=lambda: result.update(backend.run(job)))
        run.start()
        deadline = time.monotonic() + 2
        while not api.requests and time.monotonic() < deadline:
            time.sleep(.01)
        job.cancel.set()
        run.join(3)
        self.assertFalse(run.is_alive())
        self.assertEqual(result["subtype"], "stopped")
        self.assertEqual(self.access.saved, [])
        self.assertFalse(any(e["t"] in ("delta", "thinking", "notation_proposal") for e in job.events))
        self.assertFalse(any("$z" in str(m.get("content", "")) for m in backend.sessions[result["session_id"]]))

    def test_known_proposal_is_not_requested_again_and_siblings_still_do_not_run(self):
        backend, job, _ = self.setup_turn([[call(("proposal", "propose_notation", {**PROPOSAL, "notation": "x"}),
                                                 ("write", "write_file", {"path": "main.tex", "content": "$x+x$"}))],
                                         [say("Reuse known $x$.")]])
        result = backend.run(job)
        self.assertFalse(result.get("notation_pending"))
        self.assertEqual(self.access.saved, [])
        self.assertEqual((self.root / "main.tex").read_text(), "$x$")
        self.assertEqual("".join(e["text"] for e in job.events if e["t"] == "delta"), "Reuse known $x$.")

    def test_guarded_compile_never_calls_the_original_root_build_endpoint(self):
        backend, job, _ = self.setup_turn([[call(("compile", "compile", {}))], [say("Known $x$.")]])
        with mock.patch("backend_openai.mcp_compile.build") as original:
            backend.run(job)
        original.assert_not_called()
        self.assertEqual(self.access.compiles, 1)
        self.assertTrue(any(e["t"] == "tool_result" and e["preview"] == "Mathematical preview withheld."
                            for e in job.events))

    def test_unconnected_legacy_backend_keeps_original_tools_and_streaming(self):
        backend, job, api = self.setup_turn([[say("Legacy "), say("stream.")]], access=False)
        backend.run(job)
        self.assertEqual([e["text"] for e in job.events if e["t"] == "delta"], ["Legacy ", "stream."])
        self.assertNotIn("propose_notation", [t["function"]["name"] for t in api.requests[0]["body"]["tools"]])
