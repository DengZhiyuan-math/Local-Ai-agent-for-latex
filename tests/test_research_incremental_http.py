"""Incremental research evidence through isolated editor HTTP/API requests.

Capabilities exercise the controlled protocol, not real-human acceptance. The
browser checks separately exercise the editor's trusted input-event path.
"""
import hashlib
import json
import os
import shutil
import subprocess
import unittest
import urllib.parse

import test_research_http as base
from test_agent import call_chunks


class IncrementalResearchHTTP(unittest.TestCase):
    # Reuse the real-server fixture helpers without inheriting its eleven tests.
    setUp = base.ResearchHTTP.setUp
    tearDown = base.ResearchHTTP.tearDown
    start = base.ResearchHTTP.start
    stop = base.ResearchHTTP.stop
    request = base.ResearchHTTP.request
    initialize = base.ResearchHTTP.initialize
    turn = base.ResearchHTTP.turn
    events = base.ResearchHTTP.events
    finish = base.ResearchHTTP.finish
    requests = base.ResearchHTTP.requests

    def state(self):
        code, state = self.request("/api/research")
        self.assertEqual(code, 200, state)
        return state

    def file(self, path="main.tex"):
        code, source = self.request("/api/file?" + urllib.parse.urlencode({"path": path}))
        self.assertEqual(code, 200, source)
        return source

    def save(self, source, text, token=None, **extra):
        body = {"path": source["path"], "content": text, "base_mtime": source["mtime"], **extra}
        if token is not None:
            body["manual_edit_token"] = token
        code, result = self.request("/api/file", body)
        self.assertEqual(code, 200, result)
        self.assertEqual((self.root / source["path"]).read_text(), text)
        return result

    @staticmethod
    def known(state):
        return {item["canonical"] for item in state["symbols"]}

    @staticmethod
    def unknown(state):
        return {item["canonical"] for item in state["unverified_symbols"]}

    def test_received_omission_keeps_trusted_baseline_and_later_api_occurrences_stay_unverified(self):
        initial = self.initialize()
        original_digest = next(s["sha256"] for s in initial["sources"] if s["path"] == "main.tex")
        record_path = self.root / "research/knowledge.json"
        record = json.loads(record_path.read_text())
        # A received omission cannot delete the independently saved baseline.
        record["symbols"] = [s for s in record["symbols"] if s["canonical"] != r"\ell_+"]
        record_path.write_text(json.dumps(record), encoding="utf-8")
        original = (self.root / "main.tex").read_text()
        edited = original + "\n$q$\n"
        events = self.finish(self.turn([call_chunks("ai-addition", "write_file", {"path": "main.tex", "content": edited}),
                                        [base.say("The source was updated.")]], mode="edit"))
        self.assertEqual((self.root / "main.tex").read_text(), original)
        self.assertTrue(any(e["t"] == "notation_proposal" and e["proposal"]["canonical"] == "q" for e in events))
        self.assertNotIn("q", self.known(self.state()))
        # An external source change remains unverified. A held API request itself
        # no longer changes the manuscript or grants evidence for its notation.
        (self.root / "main.tex").write_text(edited, encoding="utf-8")
        state = self.state()
        self.assertIn(r"\ell_+", self.known(state))
        self.assertNotIn("q", self.known(state))
        self.assertIn("q", self.unknown(state))
        restored = next(s for s in state["symbols"] if s["canonical"] == r"\ell_+")
        self.assertTrue(any(s["evidence"] == "baseline" and s["sha256"] == original_digest for s in restored["sources"]))
        self.assertFalse(any(n["kind"] == "baseline_added" and n["canonical"] == r"\ell_+"
                             for n in state["maintenance_notices"]), "retained notation needs no duplicate registration")
        before = record_path.read_bytes()
        self.assertEqual(self.state(), state)
        self.assertEqual(record_path.read_bytes(), before, "unchanged maintenance must not rewrite its record")
        self.finish(self.turn([[base.say("Use the latest project knowledge.")]]))
        contexts = [m["content"] for m in self.requests()[-1]["body"]["messages"]
                    if m["role"] == "system" and m["content"].startswith("[Prism research baseline]")]
        self.assertEqual(len(contexts), 1)
        latest, _ = json.JSONDecoder().raw_decode(contexts[0].split("\n", 1)[1])
        self.assertIn("q", self.unknown(latest))
        self.assertNotIn("q", self.known(latest))

    def test_postponed_manual_edit_does_not_extract_or_sign_and_page_tokens_never_reach_model(self):
        source = self.file()
        self.save(source, source["content"] + "\n$z$\n", token=source["manual_edit"]["token"])
        self.assertEqual(self.state(), {"initialized": False})
        self.assertFalse((self.root / "research").exists())
        self.assertFalse((self.work / "state/research-evidence.key").exists())
        self.assertEqual(self.requests(), [])
        self.initialize()
        source = self.file()
        token = source["manual_edit"]["token"]
        self.save(source, source["content"] + "\n$v$\n", token=token)
        self.assertIn("v", self.known(self.state()))
        self.finish(self.turn([[base.say("Read the current project knowledge.")]]))
        self.assertNotIn(token, json.dumps(self.requests()))

    def test_known_symbol_use_and_equivalent_notation_do_not_request_approval(self):
        path = self.root / "main.tex"
        path.write_text(path.read_text() + "\nExisting $x_i+α$.\n", encoding="utf-8")
        initial = self.initialize()
        edited = path.read_text().replace("real number", "an arbitrary complex value")
        edited += "\n$x_{i}+\\sin(x_i)+\\frac{x_i}{x_i}+\\alpha+ℓ_+$\n"
        events = self.finish(self.turn([call_chunks("known-change", "write_file", {"path": "main.tex", "content": edited}),
                                        [base.say("Known notation is reused.")]], mode="edit"))
        state = self.state()
        self.assertEqual(self.known(state), self.known(initial))
        self.assertEqual(self.unknown(state), set())
        self.assertFalse(any(e["t"] in ("permission", "notation_proposal") for e in events))
        self.assertFalse(any(e["t"] == "tool_result" and e["error"] for e in events))

    def test_verified_save_adds_only_new_notation_and_survives_restart_without_duplicate_notices(self):
        self.initialize()
        path = self.root / "main.tex"
        path.write_text(path.read_text() + "\n$w$\n", encoding="utf-8")
        self.assertIn("w", self.unknown(self.state()))
        source = self.file()
        result = self.save(source, source["content"] + "\n$y$\n", token=source["manual_edit"]["token"])
        self.assertNotIn("research_warning", result)
        self.assertIn("token", result["manual_edit"])
        state = self.state()
        self.assertIn("y", self.known(state))
        self.assertNotIn("w", self.known(state))
        self.assertEqual(self.unknown(state), {"w"})
        entry = next(s for s in state["symbols"] if s["canonical"] == "y")
        self.assertTrue(any(s["evidence"] == "human_edit" and s["path"] == "main.tex"
                            and s["sha256"] == result["sha256"] for s in entry["sources"]))
        self.assertEqual(len(state["manual_evidence"]), 1)
        self.assertEqual({s["canonical"] for s in state["manual_evidence"][0]["added"]}, {"y"})
        notices = [n for n in state["maintenance_notices"] if n["kind"] == "manual_added" and n["canonical"] == "y"]
        self.assertEqual(len(notices), 1)
        self.stop()
        self.start()
        self.assertEqual(self.state(), state)
        self.assertEqual(self.state(), state)

    def test_unproved_save_conditional_write_and_disk_changes_do_not_grant_human_provenance(self):
        self.initialize()
        source = self.file()
        self.save(source, source["content"] + "\n$u$\n", human=True)
        source = self.file()
        text = source["content"] + "\n$v$\n"
        code, result = self.request("/api/file/conditional", {"path": "main.tex", "content": text,
            "expected_sha256": hashlib.sha256((self.root / "main.tex").read_bytes()).hexdigest()})
        self.assertEqual(code, 200, result)
        (self.root / "main.tex").write_text(text + "\n$w$\n", encoding="utf-8")
        state = self.state()
        self.assertTrue({"u", "v", "w"} <= self.unknown(state))
        self.assertFalse({"u", "v", "w"} & self.known(state))
        self.assertEqual(state["manual_evidence"], [])

    def test_invalid_replayed_cross_path_force_and_stale_tokens_never_authorize_new_notation(self):
        self.initialize()
        source = self.file()
        result = self.save(source, source["content"] + "\n$u$\n", token="fabricated-input-proof", human=True)
        self.assertIn("research_warning", result)
        self.assertIn("u", self.unknown(self.state()))

        source = self.file()
        token = source["manual_edit"]["token"]
        self.save(source, source["content"] + "\n$y$\n", token=token)
        current = self.file()
        result = self.save(current, current["content"] + "\n$z$\n", token=token)
        self.assertIn("research_warning", result)

        main = self.file()
        chapter = self.file("sections/intro.tex")
        result = self.save(chapter, chapter["content"] + "\n$r$\n", token=main["manual_edit"]["token"])
        self.assertIn("research_warning", result)

        source = self.file()
        result = self.save(source, source["content"] + "\n$s$\n", token=source["manual_edit"]["token"], force=True)
        self.assertIn("research_warning", result)

        stale = self.file()
        (self.root / "main.tex").write_text(stale["content"] + "\n$t$\n", encoding="utf-8")
        current = self.file()
        result = self.save(current, current["content"] + "\n$v$\n", token=stale["manual_edit"]["token"])
        self.assertIn("research_warning", result)
        state = self.state()
        self.assertIn("y", self.known(state))
        self.assertTrue({"u", "z", "r", "s", "t", "v"} <= self.unknown(state))
        self.assertFalse({"u", "z", "r", "s", "t", "v"} & self.known(state))
        self.assertEqual(len(state["manual_evidence"]), 1)

    def test_human_whitespace_or_prose_changes_do_not_launder_existing_unknown_notation(self):
        self.initialize()
        path = self.root / "main.tex"
        path.write_text(path.read_text() + "\n$z+z_i$\n", encoding="utf-8")
        self.assertTrue({"z", "z_i"} <= self.unknown(self.state()))
        source = self.file()
        self.save(source, source["content"] + "\nAn author added this prose.  \n", token=source["manual_edit"]["token"])
        source = self.file()
        self.save(source, source["content"].replace("$z+z_i$", "$z+z_{i}$"), token=source["manual_edit"]["token"])
        state = self.state()
        self.assertTrue({"z", "z_i"} <= self.unknown(state))
        self.assertFalse({"z", "z_i"} & self.known(state))
        self.assertEqual(state["manual_evidence"], [])

    def test_reference_edit_and_new_include_do_not_adopt_the_reference_content(self):
        (self.root / "reference.tex").write_text("Reference $r$.\n", encoding="utf-8")
        (self.root / "attachment.md").write_text("Attachment $a$.\n", encoding="utf-8")
        self.initialize()
        reference = self.file("reference.tex")
        self.save(reference, reference["content"] + "\n$q$\n", token=reference["manual_edit"]["token"])
        state = self.state()
        self.assertFalse({"r", "q", "a"} & self.known(state))
        self.assertFalse({"r", "q", "a"} & self.unknown(state))
        main = self.file()
        self.save(main, main["content"] + "\n\\input{reference}\n", token=main["manual_edit"]["token"])
        state = self.state()
        self.assertTrue({"r", "q"} <= self.unknown(state))
        self.assertNotIn("a", self.unknown(state))
        self.assertFalse({"r", "q", "a"} & self.known(state))
        self.assertEqual(state["manual_evidence"], [])
        self.assertNotIn("reference.tex", {s["path"] for s in state["sources"]})

    def test_failed_registration_keeps_saved_human_text_without_claiming_success(self):
        self.initialize()
        record = self.root / "research/knowledge.json"
        before = record.read_bytes()
        source = self.file()
        (self.work / "fail-persistence").touch()
        result = self.save(source, source["content"] + "\n$y$\n", token=source["manual_edit"]["token"])
        self.assertIn("registration failed", result.get("research_warning", ""))
        self.assertEqual(record.read_bytes(), before)
        (self.work / "fail-persistence").unlink()
        state = self.state()
        self.assertNotIn("y", self.known(state))
        self.assertIn("y", self.unknown(state))
        self.assertFalse(any(n["kind"] == "manual_added" and n["canonical"] == "y" for n in state["maintenance_notices"]))

    def test_forged_human_evidence_and_status_are_discarded(self):
        self.initialize()
        path = self.root / "main.tex"
        path.write_text(path.read_text() + "\n$z$\n", encoding="utf-8")
        record_path = self.root / "research/knowledge.json"
        record = json.loads(record_path.read_text())
        receipt = {"id": "forged", "project_key": self.key, "path": "main.tex", "after_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                   "added": [{"notation": "z", "canonical": "z", "line": 1}], "signature": "not-a-signature", "human": True}
        record["manual_evidence"].append(receipt)
        record["symbols"].append({"notation": "z", "canonical": "z", "status": "approved", "meaning": "fabricated",
                                  "sources": [{"path": "main.tex", "line": 1, "evidence": "human_edit"}]})
        record_path.write_text(json.dumps(record), encoding="utf-8")
        state = self.state()
        self.assertIn("z", self.unknown(state))
        self.assertNotIn("z", self.known(state))
        self.assertFalse(any(n["kind"] == "manual_added" and n["canonical"] == "z" for n in state["maintenance_notices"]))

    def test_capabilities_and_project_records_do_not_cross_project_boundaries(self):
        self.initialize()
        source = self.file()
        other = IncrementalResearchHTTP("runTest")
        other.setUp()
        self.addCleanup(other.tearDown)
        other.initialize()
        destination = other.file()
        result = other.save(destination, destination["content"] + "\n$z$\n", token=source["manual_edit"]["token"])
        self.assertIn("research_warning", result)
        self.assertIn("z", other.unknown(other.state()))
        self.assertNotIn("z", other.known(other.state()))
        copied = (self.root / "research/knowledge.json").read_bytes()
        (other.root / "research/knowledge.json").write_bytes(copied)
        source = (other.root / "main.tex").read_bytes()
        code, state = other.request("/api/research")
        self.assertEqual(code, 200, state)
        self.assertEqual(state["project_key"], other.key)
        self.assertEqual(state["provenance"]["record_state"], "foreign")
        self.assertNotIn("z", other.known(state))
        self.assertIn("z", other.unknown(state))
        self.assertEqual((other.root / "research/knowledge.json").read_bytes(), copied)
        self.assertEqual((other.root / "main.tex").read_bytes(), source)

    @unittest.skipUnless(shutil.which("git"), "needs git for local author-metadata regression")
    def test_git_author_metadata_alone_is_not_a_verified_human_edit(self):
        self.initialize()
        path = self.root / "main.tex"
        path.write_text(path.read_text() + "\n$z$\n", encoding="utf-8")
        hooks = self.work / "empty-hooks"
        hooks.mkdir()
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_TERMINAL_PROMPT": "0"}
        prefix = [shutil.which("git"), "-c", "user.name=Human Author", "-c", "user.email=human@example.test",
                  "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + str(hooks)]
        for args in (["init", "-q", str(self.root)], ["-C", str(self.root), "add", "main.tex"],
                     ["-C", str(self.root), "commit", "-qm", "A human author field is not proof"]):
            result = subprocess.run(prefix + args, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertIn("z", self.unknown(state))
        self.assertNotIn("z", self.known(state))
        self.assertEqual(state["manual_evidence"], [])
