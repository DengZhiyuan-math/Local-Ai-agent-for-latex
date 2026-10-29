"""Incremental knowledge behavior with a controlled local receipt verifier."""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from research import RECORD, ResearchKnowledge, declared_identifier_commands
from registry import project_key
from test_research import project_with


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def signature(receipt):
    payload = {k: v for k, v in receipt.items() if k != "signature"}
    return hmac.new(b"isolated-receipt-fixture", json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def verified(receipt, key):
    return (isinstance(receipt, dict) and receipt.get("project_key") == key
            and hmac.compare_digest(str(receipt.get("signature", "")), signature(receipt)))


class IncrementalKnowledge(unittest.TestCase):
    def setUp(self):
        # Signature generation itself is exercised by the dedicated receipt tests;
        # this deterministic seam verifies the core never trusts an actor/status field.
        self.verifier = mock.patch("research._verify_manual", side_effect=verified)
        self.verifier.start()
        self.addCleanup(self.verifier.stop)
        self.project = project_with({"main.tex": "$x$"})
        self.knowledge = ResearchKnowledge(self.project)

    def receipt(self, before, after, names, path="main.tex", **changes):
        value = {"id": digest(before + "\0" + after + path), "project_key": project_key(self.project.root),
                 "path": path, "before_sha256": digest(before), "after_sha256": digest(after),
                 "added": [{"notation": name, "canonical": name, "line": 1} for name in names], "at": 1}
        value.update(changes)
        value["signature"] = signature(value)
        return value

    def write(self, text, path="main.tex"):
        (self.project.root / path).write_text(text, encoding="utf-8")

    def test_uninitialized_refresh_does_not_read_or_write_research(self):
        with mock.patch.object(self.project, "resolve", side_effect=AssertionError("must not extract")):
            self.assertEqual(self.knowledge.refresh(), {"initialized": False})
        self.assertFalse((self.project.root / "research").exists())

    def test_unchanged_refresh_does_not_write_or_repeat_notices(self):
        state = self.knowledge.initialize()
        raw = (self.project.root / RECORD).read_bytes()
        self.project.on_change = mock.Mock()
        with mock.patch("research.replace_file", side_effect=AssertionError("must not write")):
            self.assertEqual(self.knowledge.refresh(), state)
        self.assertEqual((self.project.root / RECORD).read_bytes(), raw)
        self.project.on_change.assert_not_called()

    def test_omitted_original_macro_is_added_from_its_initialization_version(self):
        original = r"\newcommand{\stored}{s}" + "\n" + r"$\stored$"
        self.write(original)
        initial = self.knowledge.initialize()
        self.assertEqual(initial["symbols"], [])
        self.write(original + "\n$z$")
        state = self.knowledge.refresh()
        stored = next(s for s in state["symbols"] if s["notation"] == r"\stored")
        self.assertEqual(stored["sources"], [{"path": "main.tex", "line": 2,
            "sha256": digest(original), "evidence": "baseline"}])
        self.assertEqual([s["notation"] for s in state["unverified_symbols"]], ["z"])
        self.assertEqual(state["maintenance_notices"][0]["kind"], "baseline_added")
        again = self.knowledge.refresh()
        self.assertEqual(again, state)

    def test_later_ai_occurrence_cannot_become_baseline(self):
        self.knowledge.initialize()
        self.write(r"\newcommand{\later}{q} $x+\later+\rho$")
        state = self.knowledge.refresh()
        self.assertEqual([s["notation"] for s in state["symbols"]], ["x"])
        self.assertEqual({s["notation"] for s in state["unverified_symbols"]}, {r"\later", r"\rho"})
        result = self.knowledge.classify(r"$\rho+\later+x$")
        self.assertEqual([s["notation"] for s in result["known"]], ["x"])
        self.assertEqual({s["notation"] for s in result["unknown"]}, {r"\later", r"\rho"})
        self.assertEqual((self.project.root / "main.tex").read_text(), r"\newcommand{\later}{q} $x+\later+\rho$")

    def test_new_include_is_unverified_not_a_retroactive_baseline(self):
        self.knowledge.initialize()
        self.write("$x$\\input{chapter}")
        self.write("$y$", "chapter.tex")
        state = self.knowledge.refresh()
        self.assertEqual([s["notation"] for s in state["unverified_symbols"]], ["y"])
        self.assertEqual(state["unverified_symbols"][0]["sources"][0]["path"], "chapter.tex")
        self.assertEqual([s["path"] for s in state["sources"]], ["main.tex"])

    def test_reference_and_attachment_content_do_not_authorize_adoption(self):
        self.write("$r$", "reference.tex")
        self.write("$a$", "attachment.md")
        self.knowledge.initialize()
        self.assertEqual(self.knowledge.refresh()["unverified_symbols"], [])
        result = self.knowledge.classify("$r+a$")
        self.assertEqual(result["known"], [])
        self.assertEqual({s["notation"] for s in result["unknown"]}, {"r", "a"})

    def test_signed_manual_edit_registers_only_its_added_notation(self):
        before, after = "$x+w$", "$x+w+y$"
        self.knowledge.initialize()
        self.write(before)
        self.knowledge.refresh()
        self.write(after)
        receipt = self.receipt(before, after, ["y"])
        state = self.knowledge.add_manual(receipt)
        self.assertEqual({s["notation"] for s in state["symbols"]}, {"x", "y"})
        self.assertEqual([s["notation"] for s in state["unverified_symbols"]], ["w"])
        y = next(s for s in state["symbols"] if s["notation"] == "y")
        self.assertEqual(y["sources"][0]["evidence"], "human_edit")
        self.assertEqual(y["sources"][0]["receipt_id"], receipt["id"])
        self.assertEqual(y["sources"][0]["sha256"], digest(after))
        self.assertEqual(state["manual_evidence"], [receipt])
        self.assertTrue(any(n["kind"] == "manual_added" and n["notation"] == "y"
                            for n in state["maintenance_notices"]))
        self.assertEqual(self.knowledge.classify("$y$")["unknown"], [])

    def test_receipt_replay_remains_idempotent_after_other_edits(self):
        before, after = "$x$", "$x+y$"
        self.knowledge.initialize()
        self.write(after)
        receipt = self.receipt(before, after, ["y"])
        state = self.knowledge.add_manual(receipt)
        self.write(after + "\n$z$")
        self.assertEqual(self.knowledge.add_manual(receipt), state)
        self.assertEqual(len(self.knowledge.status()["manual_evidence"]), 1)

    def test_fake_human_status_and_bad_signature_never_grant_permission(self):
        initial = self.knowledge.initialize()
        self.write("$x+y$")
        receipt = self.receipt("$x$", "$x+y$", ["y"])
        receipt["signature"] = "forged"
        with self.assertRaisesRegex(ValueError, "not verified"):
            self.knowledge.add_manual(receipt)
        self.assertEqual(self.knowledge.status(), initial)
        path = self.project.root / RECORD
        data = json.loads(path.read_text())
        data["manual_evidence"] = [receipt]
        data["symbols"].append({"notation": "y", "canonical": "y", "meaning": "claimed",
                                "status": "approved", "sources": [{"evidence": "human_edit"}]})
        path.write_text(json.dumps(data))
        self.assertEqual(self.knowledge.classify("$y$")["known"], [])
        refreshed = self.knowledge.refresh()
        self.assertNotIn("y", {s["notation"] for s in refreshed["symbols"]})
        self.assertEqual(refreshed["unverified_symbols"][0]["notation"], "y")

    def test_receipt_is_bound_to_project_path_version_and_actual_notation(self):
        initial = self.knowledge.initialize()
        after = "$x+y$"
        self.write(after)
        other = self.receipt("$x$", after, ["y"], project_key="other-project")
        stale = self.receipt("$x$", "$x+z$", ["z"])
        invented = self.receipt("$x$", after, ["z"])
        for receipt in (other, stale, invented):
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                self.knowledge.add_manual(receipt)
            self.assertEqual(self.knowledge.status(), initial)
        self.assertEqual((self.project.root / "main.tex").read_text(), after)

    def test_even_a_valid_reference_edit_does_not_adopt_reference_notation(self):
        initial = self.knowledge.initialize()
        self.write("$r$", "reference.tex")
        receipt = self.receipt("", "$r$", ["r"], path="reference.tex")
        self.assertEqual(self.knowledge.add_manual(receipt), initial)
        self.assertEqual(self.knowledge.classify("$r$")["known"], [])

    def test_report_exclusion_is_not_silently_undone(self):
        self.write(r"$\ell+\ell_+$")
        self.knowledge.initialize()
        excluded = self.knowledge.correct(excluded_symbols=[r"\ell_+"])
        state = self.knowledge.refresh()
        self.assertEqual(state, excluded)
        self.assertEqual([s["notation"] for s in state["symbols"]], [r"\ell"])
        self.assertFalse(any(n["kind"] == "baseline_added" for n in state["maintenance_notices"]))

    def test_later_human_adoption_overrides_only_the_extraction_exclusion(self):
        self.write("$x+h$")
        self.knowledge.initialize()
        self.knowledge.correct(excluded_symbols=["h"])
        self.assertEqual(self.knowledge.classify("$h$")["known"], [])
        self.knowledge.decide_reply("Use $h$ for $x+x$.", "fixture:exclusion")
        self.assertEqual(self.knowledge.classify("$h$")["unknown"], [])
        adopted = next(s for s in self.knowledge.status()["symbols"] if s["canonical"] == "h")
        self.assertEqual([s["evidence"] for s in adopted["sources"]], ["human_decision"])
        self.assertEqual(self.knowledge.refresh()["corrections"][0]["excluded_symbols"], ["h"])
        self.knowledge.decide_reply("Don't use h.", "fixture:exclusion")
        self.assertEqual(self.knowledge.classify("$h$")["known"], [])
        self.assertEqual(self.knowledge.status()["adoptions"], [])
        self.knowledge.decide_reply("Reopen h.", "fixture:exclusion")
        self.assertEqual(self.knowledge.classify("$h$")["known"], [])
        self.assertNotIn("h", {s["canonical"] for s in self.knowledge.refresh()["symbols"]})

    def real_manual_save(self, text):
        from research_edits import EditTokens
        path = self.project.root / "main.tex"
        before, after = path.read_bytes(), text.encode()
        tokens = EditTokens()
        key = project_key(self.project.root)
        capability = tokens.issue(key, "main.tex", before)
        path.write_bytes(after)
        consumed, receipt = tokens.consume(capability["token"], key, "main.tex", before, after)
        self.assertTrue(consumed)
        if receipt:
            self.knowledge.add_manual(receipt)
        return receipt

    def test_new_manual_adoption_overrides_extraction_exclusion_with_its_own_evidence(self):
        from research_edits import verify_receipt
        with mock.patch("research._verify_manual", side_effect=verify_receipt):
            self.write("$x+h$")
            self.knowledge.initialize()
            self.knowledge.correct(excluded_symbols=["h"])
            self.real_manual_save("$x$")
            receipt = self.real_manual_save("$x+h$")
            self.assertEqual(self.knowledge.classify("$h$")["unknown"], [])
            adopted = next(s for s in self.knowledge.status()["symbols"] if s["canonical"] == "h")
            self.assertEqual([s["evidence"] for s in adopted["sources"]], ["human_edit"])
            self.assertEqual(adopted["sources"][0]["receipt_id"], receipt["id"])
            state = self.knowledge.refresh()
            self.assertEqual(state["corrections"][0]["excluded_symbols"], ["h"])
            self.assertTrue(any(n["kind"] == "manual_added" and n["canonical"] == "h"
                                for n in state["maintenance_notices"]))
            self.assertEqual(ResearchKnowledge(self.project).classify("$h$")["unknown"], [])
            self.knowledge.decide_reply("Don't use h.", "fixture:manual-exclusion")
            self.assertEqual(self.knowledge.classify("$h$")["known"], [])
            # A fresh save alone does not override the explicit name refusal.
            self.real_manual_save("$x$")
            self.real_manual_save("$x+h$")
            self.assertEqual(self.knowledge.classify("$h$")["known"], [])
            self.knowledge.decide_reply("Reopen h.", "fixture:manual-exclusion")
            self.assertEqual(self.knowledge.classify("$h$")["known"], [])
            self.real_manual_save("$x$")
            self.real_manual_save("$x+h$")
            self.assertEqual(self.knowledge.classify("$h$")["unknown"], [])

    def test_excluded_old_manual_receipt_cannot_revive_but_a_new_save_can_adopt(self):
        from research_edits import verify_receipt
        with mock.patch("research._verify_manual", side_effect=verify_receipt):
            self.knowledge.initialize()
            first = self.real_manual_save("$x+h$")
            self.knowledge.correct(excluded_symbols=["h"])
            self.knowledge.refresh()
            self.assertEqual(self.knowledge.classify("$h$")["known"], [])
            self.knowledge.add_manual(first)
            self.assertEqual(self.knowledge.classify("$h$")["known"], [])
            self.real_manual_save("$x$")
            second = self.real_manual_save("$x+h$")
            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(self.knowledge.classify("$h$")["unknown"], [])
            adopted = next(s for s in self.knowledge.status()["symbols"] if s["canonical"] == "h")
            self.assertEqual([s["receipt_id"] for s in adopted["sources"]], [second["id"]])
            self.assertEqual(len([n for n in self.knowledge.status()["maintenance_notices"]
                                  if n["kind"] == "manual_added" and n["canonical"] == "h"]), 2)
            self.knowledge.correct(excluded_symbols=["h"])
            self.knowledge.refresh()
            self.assertEqual(self.knowledge.classify("$h$")["known"], [])
            self.knowledge.add_manual(second)
            self.assertEqual(self.knowledge.classify("$h$")["known"], [])

    def test_changed_definition_and_equivalent_spelling_keep_known_identity(self):
        self.write("$\\alpha_i$ denotes a vector.")
        self.knowledge.initialize()
        result = self.knowledge.classify("$α_{i}$ denotes a different object. $\\sum_{i=1}^2\\alpha_i$")
        self.assertEqual({s["canonical"] for s in result["known"]}, {r"\alpha_i"})
        self.assertEqual([s["notation"] for s in result["unknown"]], ["i"])
        self.assertEqual(result["gaps"], [])

    def test_unbraced_styled_baseline_keeps_identity_after_refresh_and_reopen(self):
        original = r"$\mathbb R+\hat x+\hat\theta$"
        equivalent = r"$\mathbb{R}+\hat{x}+\hat{\theta}$"
        self.write(original)
        initial = self.knowledge.initialize()
        self.assertEqual(len(initial["symbols"]), 3)
        for knowledge in (self.knowledge, ResearchKnowledge(self.project)):
            for text in (original, equivalent):
                with self.subTest(text=text, knowledge=knowledge):
                    result = knowledge.classify(text)
                    self.assertEqual(result["unknown"], [])
                    self.assertEqual(len(result["known"]), 3)
                    self.assertEqual(result["gaps"], [])
            refreshed = knowledge.refresh()
            self.assertEqual(len(refreshed["symbols"]), 3)
            self.assertEqual(refreshed["unverified_symbols"], [])
        self.assertEqual(self.knowledge.classify("$R+x+z$")["known"], [])

    def test_signed_manual_styled_notation_uses_the_parser_canonical(self):
        before, after = "$x$", r"$x+\hat y$"
        self.knowledge.initialize()
        self.write(after)
        receipt = self.receipt(before, after, [r"\hat y"])
        receipt["added"][0]["canonical"] = r"\hat{y}"
        receipt["signature"] = signature(receipt)
        state = self.knowledge.add_manual(receipt)
        symbol = next(s for s in state["symbols"] if s["canonical"] == r"\hat{y}")
        self.assertEqual(symbol["sources"][0]["receipt_id"], receipt["id"])
        self.assertEqual(self.knowledge.classify(r"$\hat{y}+\hat y$")["unknown"], [])
        self.assertEqual(self.knowledge.refresh(), state)
        self.assertEqual(self.knowledge.classify("$y$")["known"], [])

    def test_unbraced_styled_proposal_keeps_approval_and_refusal_bound_to_identity(self):
        self.knowledge.initialize()
        proposal = self.knowledge.propose(r"\hat z", "The error term.", "The proof.",
                                          "Name the error.", source_expression="x+x")
        self.assertEqual(proposal["canonical"], r"\hat{z}")
        self.assertEqual(self.knowledge.classify(r"$\hat z$")["known"], [])
        self.knowledge.decide_reply(r"Approve $\hat z$.", "fixture:style")
        self.assertEqual(self.knowledge.classify(r"$\hat z+\hat{z}$")["unknown"], [])
        self.assertEqual(self.knowledge.classify("$z$")["known"], [])
        self.knowledge.decide_reply(r"Reject $\hat{z}$.", "fixture:style")
        self.assertEqual(self.knowledge.classify(r"$\hat z+\hat{z}$")["known"], [])

    def test_starred_standard_operator_can_reach_shared_output_and_source_checks(self):
        from research_access import ResearchAccess
        self.write("$x+n$")
        self.knowledge.initialize()
        access = ResearchAccess(self.knowledge)
        draft = r"$\operatorname*{lim}_{n\to\infty} x$"
        self.assertEqual(access.review_output(draft), draft)
        path = self.project.root / "main.tex"
        access.apply_write(path, draft, path.read_bytes())
        self.assertEqual(path.read_text(), draft)
        self.assertEqual(self.knowledge.status()["proposals"], [])

    def test_unbraced_style_does_not_allow_a_mismatched_manual_identity(self):
        self.knowledge.initialize()
        after = r"$x+\hat y$"
        self.write(after)
        receipt = self.receipt("$x$", after, [r"\hat y"])
        receipt["added"][0]["canonical"] = "z"
        receipt["signature"] = signature(receipt)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.knowledge.add_manual(receipt)
        self.assertEqual(self.knowledge.classify(r"$\hat y+z$")["known"], [])

    def test_unknown_complex_notation_stays_a_reported_gap(self):
        self.knowledge.initialize()
        self.write(r"$x^{(r)}+\unexplained{words}$")
        state = self.knowledge.refresh()
        self.assertTrue(state["incremental_gaps"])
        self.assertFalse(any(s["notation"] == "words" for s in state["symbols"]))

    def test_saved_manual_evidence_and_notices_survive_reopen(self):
        self.knowledge.initialize()
        self.write("$x+y$")
        state = self.knowledge.add_manual(self.receipt("$x$", "$x+y$", ["y"]))
        from project import Project
        reopened = ResearchKnowledge(Project(self.project.root))
        self.assertEqual(reopened.refresh(), state)
        self.assertEqual(reopened.classify("$y$")["unknown"], [])

    def test_manual_record_failure_keeps_source_and_previous_permission_state(self):
        initial = self.knowledge.initialize()
        after = "$x+y$"
        self.write(after)
        with mock.patch("research.replace_file", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.knowledge.add_manual(self.receipt("$x$", after, ["y"]))
        self.assertEqual(self.knowledge.status(), initial)
        self.assertEqual(self.knowledge.classify("$y$")["known"], [])
        self.assertEqual((self.project.root / "main.tex").read_text(), after)

    def test_baseline_fill_failure_does_not_claim_registration(self):
        self.write(r"\newcommand{\stored}{s} $\stored$")
        initial = self.knowledge.initialize()
        with mock.patch("research.replace_file", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.knowledge.refresh()
        self.assertEqual(self.knowledge.status(), initial)
        self.assertEqual(self.knowledge.classify(r"$\stored$")["known"], [])

    def test_active_turn_does_not_reindex_incomplete_generation(self):
        state = self.knowledge.initialize()
        self.write("$new$")
        self.project.turn = mock.Mock(**{"busy.return_value": True})
        with mock.patch.object(self.project, "resolve", side_effect=AssertionError("must not scan")):
            self.assertEqual(self.knowledge.refresh(), state)

    def test_current_includes_never_read_excluded_or_external_sources(self):
        self.knowledge.initialize()
        name = "outside-" + self.project.root.name
        self.write("$x$\\input{../" + name + "}\\input{hidden}")
        outside = self.project.root.parent / (name + ".tex")
        outside.write_text("$S$", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        self.write("$H$", "hidden.tex")
        self.write('{"exclude":["hidden.tex"]}', "prism.json")
        state = self.knowledge.refresh()
        self.assertEqual(state["unverified_symbols"], [])
        self.assertTrue(state["incremental_gaps"])

    def test_parameter_and_expression_macros_are_not_assumed_to_be_names(self):
        commands = declared_identifier_commands([
            r"\newcommand{\named}{\alpha_i} \newcommand{\hidden}[1]{x} "
            r"\newcommand{\expression}{x+y} \newcommand{\optional}[0]{y}"])
        self.assertEqual(commands, {"named", "optional"})
        self.assertNotIn("named", declared_identifier_commands([
            r"\newcommand{\named}{x} \renewcommand{\named}[1]{x}"]))

    def test_real_local_signature_survives_reopen_but_foreign_host_state_does_not_authorize(self):
        from research_edits import make_receipt, verify_receipt
        from tmpdirs import tmpdir
        first_state, other_state = tmpdir(), tmpdir() / "foreign-state"
        self.knowledge.initialize()
        self.write("$x+y$")
        with mock.patch("research._verify_manual", side_effect=verify_receipt):
            with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(first_state)}):
                receipt = make_receipt(project_key(self.project.root), "main.tex", b"$x$", b"$x+y$")
                registered = self.knowledge.add_manual(receipt)
                self.assertEqual(self.knowledge.refresh(), registered)
                self.assertEqual(self.knowledge.classify("$y$")["unknown"], [])
            with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(other_state)}):
                self.assertEqual(self.knowledge.classify("$y$")["known"], [])
                self.assertFalse(other_state.exists(), "verification must not create a replacement key")
                state = self.knowledge.refresh()
                self.assertFalse(state["initialized"])
                self.assertNotIn("x", {s["notation"] for s in state["symbols"]})
                self.assertNotIn("y", {s["notation"] for s in state["symbols"]})
                self.assertEqual([s["notation"] for s in state["unverified_symbols"]], ["x", "y"])

    def assert_real_receipt_for_main(self, main):
        from research_edits import make_receipt, verify_receipt
        from tmpdirs import tmpdir
        self.write(json.dumps({"main": main}), "prism.json")
        initialized = self.knowledge.initialize()
        self.assertEqual([source["path"] for source in initialized["sources"]], ["main.tex"])
        before, after = (self.project.root / "main.tex").read_bytes(), b"$x+y$"
        self.write(after.decode())
        saved_path = self.project.rel(self.project.resolve(main))
        with mock.patch("research._verify_manual", side_effect=verify_receipt), \
                mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(tmpdir())}):
            receipt = make_receipt(project_key(self.project.root), saved_path, before, after)
            state = self.knowledge.add_manual(receipt)
            added = next(s for s in state["symbols"] if s["notation"] == "y")
            self.assertEqual(added["sources"][0]["path"], "main.tex")
            self.assertEqual(added["sources"][0]["receipt_id"], receipt["id"])
            self.assertEqual(self.knowledge.classify("$y$")["unknown"], [])
            self.assertEqual(self.knowledge.refresh(), state)

    def test_dot_prefixed_main_matches_canonical_manual_receipt(self):
        self.assert_real_receipt_for_main("./main.tex")

    def test_safe_lexical_main_alias_matches_canonical_manual_receipt(self):
        self.assert_real_receipt_for_main("nested/../main.tex")

    def test_internal_link_main_is_deduplicated_and_matches_manual_receipt(self):
        try:
            (self.project.root / "alias.tex").symlink_to(self.project.root / "main.tex")
        except OSError:
            self.skipTest("symbolic links are unavailable")
        self.write("$x$\\input{alias}")
        self.assert_real_receipt_for_main("alias.tex")

    def test_legacy_snapshot_paths_are_not_rewritten_when_receipt_paths_are_normalized(self):
        from research_edits import make_receipt, verify_receipt
        from tmpdirs import tmpdir
        self.write('{"main":"./main.tex"}', "prism.json")
        self.knowledge.initialize()
        record = self.project.root / RECORD
        data = json.loads(record.read_text())
        data["sources"][0]["path"] = "./main.tex"
        for item in data["symbols"]:
            for source in item["sources"]:
                source["path"] = "./main.tex"
        record.write_text(json.dumps(data))
        snapshots = copy.deepcopy(data["sources"])
        original_record = record.read_bytes()
        self.write("$x+y$")
        with mock.patch("research._verify_manual", side_effect=verify_receipt), \
                mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(tmpdir())}):
            receipt = make_receipt(project_key(self.project.root), "main.tex", b"$x$", b"$x+y$")
            state = self.knowledge.add_manual(receipt)
            self.assertEqual(self.knowledge.classify("$y$")["unknown"], [])
            self.assertEqual(json.loads(record.read_text())["sources"], snapshots)
            self.assertEqual(record.read_bytes(), original_record)
            self.assertFalse(state["initialized"])
            self.assertEqual(state["sources"], [])
            references = state["provenance"]["received_references"]["sources"]
            self.assertEqual(references, [{"path": "./main.tex", "sha256": snapshots[0]["sha256"]}])
            self.assertEqual(self.knowledge.classify("$x$")["known"], [])


if __name__ == "__main__":
    unittest.main()
