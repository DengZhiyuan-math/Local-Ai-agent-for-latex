"""Research initialization from isolated project files; no external AI calls."""
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from project import Project
from research import RECORD, ResearchKnowledge, math_fragments, math_notations
from tmpdirs import tmpdir


def project_with(files=None):
    root = tmpdir()
    for rel, text in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return Project(root)


class MathematicalNotation(unittest.TestCase):
    def test_complete_notations_and_equivalent_decoration(self):
        parsed = math_notations(r"Words are not variables. $\ell + \ell_+ + \ell_{+} + x_i + x_{i}$")
        self.assertEqual([s["canonical"] for s in parsed["symbols"]],
                         [r"\ell", r"\ell_+", r"\ell_+", "x_i", "x_i"])
        self.assertEqual(parsed["gaps"], [])

    def test_operators_layout_and_text_do_not_become_identifiers(self):
        parsed = math_notations(r"$\frac{x}{y}+\sin x+\sum_{j=1}^n z_j+\text{for all}+\label{eq:x}$")
        names = [s["notation"] for s in parsed["symbols"]]
        self.assertEqual(names, ["x", "y", "x", "j", "n", "z_j"])
        self.assertEqual(parsed["gaps"], [])

    def test_display_environment_positions_and_explicit_gaps(self):
        source = "intro\n\\begin{align*}\n\\alpha_i = \\unknown{x}\n\\end{align*}\n$unfinished"
        result = math_notations(source)
        self.assertEqual(result["symbols"][0], {"notation": r"\alpha_i", "canonical": r"\alpha_i", "line": 3})
        self.assertIn("unknown", result["gaps"][0]["reason"])
        self.assertIn("Unclosed", result["gaps"][-1]["reason"])
        self.assertEqual(math_fragments(source), ["\n\\alpha_i = \\unknown{x}\n", "unfinished"])

    def test_comments_and_escaped_dollars_are_not_math(self):
        source = "% $secret$\nA price is \\$5. $x$\n\\(y\\) and \\[z\\]"
        self.assertEqual(math_fragments(source), ["x", "y", "z"])

    def test_chat_percent_is_not_a_tex_comment(self):
        text = "50% complete; new math $z=2$"
        self.assertEqual(math_fragments(text), [])
        self.assertEqual(math_fragments(text, latex=False), ["z=2"])

    def test_unclosed_chat_math_after_percent_remains_visible_to_guard(self):
        text = "50% complete; new math \\(z=2"
        self.assertEqual(math_fragments(text), [])
        self.assertEqual(math_fragments(text, latex=False), ["z=2"])


class Baseline(unittest.TestCase):
    def test_literal_unbraced_input_and_include_share_baseline_and_refresh_coverage(self):
        project = project_with({
            "main.tex": "$x$\n\\input chapters/proof\n\\include{appendix}\n",
            "chapters/proof.tex": "$\\ell_+$\n",
            "appendix.tex": "$y$\n",
        })
        knowledge = ResearchKnowledge(project)
        state = knowledge.initialize()
        self.assertEqual(state["coverage"]["read"], ["main.tex", "chapters/proof.tex", "appendix.tex"])
        self.assertEqual(knowledge.classify(r"$\ell_+ + y$")["unknown"], [])
        sources, gaps = knowledge._manuscript()
        self.assertEqual([source["path"] for source in sources], state["coverage"]["read"])
        self.assertEqual(gaps, [])

    def test_refused_large_include_does_not_consume_remaining_refresh_budget(self):
        project = project_with({
            "main.tex": "\\input{large}\n\\input{small}\n",
            "large.tex": "%" + " " * 100,
            "small.tex": "$x$\n",
        })
        knowledge = ResearchKnowledge(project)
        with mock.patch("research.MAX_TOTAL_TEXT", 80):
            state = knowledge.initialize()
            sources, gaps = knowledge._manuscript()
        self.assertEqual(state["coverage"]["read"], ["main.tex", "small.tex"])
        self.assertEqual([source["path"] for source in sources], state["coverage"]["read"])
        self.assertEqual([gap["path"] for gap in gaps], ["large.tex"])

    def test_status_does_not_create_or_extract_anything(self):
        project = project_with({"main.tex": "$x$"})
        self.assertEqual(ResearchKnowledge(project).status(), {"initialized": False})
        self.assertFalse((project.root / "research").exists())

    def test_multidocument_baseline_keeps_versions_locations_and_source_text(self):
        files = {"main.tex": "\\documentclass{article}\n$\\ell$\n\\input{chapters/proof}\n",
                 "chapters/proof.tex": "\\emph{sector} means a connected region.\n$\\ell_+ + a$\n"}
        project = project_with(files)
        changed = []
        project.on_change = lambda: changed.append(True)
        state = ResearchKnowledge(project).initialize()
        self.assertTrue(state["initialized"])
        self.assertEqual(state["coverage"]["read"], list(files))
        self.assertEqual({s["notation"] for s in state["symbols"]}, {r"\ell", r"\ell_+", "a"})
        ell = state["symbols"][0]
        self.assertEqual(ell["sources"][0]["line"], 2)
        self.assertEqual(ell["sources"][0]["evidence"], "baseline")
        self.assertEqual(ell["sources"][0]["sha256"], hashlib.sha256(files["main.tex"].encode()).hexdigest())
        self.assertEqual(state["terms"][0]["meaning"], "a connected region")
        self.assertEqual(state["terms"][0]["status"], "documented")
        saved = json.loads((project.root / RECORD).read_text())
        self.assertEqual(saved["sources"][0]["text"], files["main.tex"])
        self.assertNotIn("text", state["sources"][0])
        self.assertEqual(state["discussion"], {"mode": "markdown"})
        self.assertEqual(changed, [True])
        for rel, text in files.items():
            self.assertEqual((project.root / rel).read_text(), text)

    def test_reopen_restores_existing_baseline_without_reinitializing_new_source(self):
        project = project_with({"main.tex": "$x$"})
        first = ResearchKnowledge(project).initialize("question")
        raw = (project.root / RECORD).read_bytes()
        (project.root / "main.tex").write_text("$new$", encoding="utf-8")
        reopened = ResearchKnowledge(Project(project.root))
        self.assertEqual(reopened.status(), first)
        self.assertEqual(reopened.initialize("different", "y"), first)
        self.assertEqual((project.root / RECORD).read_bytes(), raw)

    def test_empty_project_with_question_and_explicit_notation(self):
        project = project_with()
        state = ResearchKnowledge(project).initialize("Can this estimate improve?", r"\rho, \ell_+")
        self.assertEqual(state["sources"], [])
        self.assertEqual(state["question"], "Can this estimate improve?")
        self.assertEqual({s["notation"] for s in state["symbols"]}, {r"\rho", r"\ell_+"})
        self.assertEqual(state["symbols"][0]["sources"][0]["evidence"], "human_input")
        self.assertEqual(state["coverage"]["main"], None)

    def test_empty_libraries_are_valid(self):
        state = ResearchKnowledge(project_with()).initialize()
        self.assertEqual(state["terms"], [])
        self.assertEqual(state["symbols"], [])

    def test_reference_document_outside_input_set_is_excluded_not_adopted(self):
        project = project_with({"main.tex": "$x$", "reference.tex": "$r$", "references.bib": "@article{paper}"})
        state = ResearchKnowledge(project).initialize()
        self.assertEqual([s["notation"] for s in state["symbols"]], ["x"])
        self.assertEqual({s["path"] for s in state["coverage"]["excluded"]}, {"reference.tex", "references.bib"})

    def test_missing_and_conflicting_definitions_are_not_repaired(self):
        text = "\\emph{undefined}\n\\emph{sector} means a region.\n\\emph{sector} means an interval.\n$\\ell$"
        project = project_with({"main.tex": text})
        state = ResearchKnowledge(project).initialize()
        terms = {t["name"]: t for t in state["terms"]}
        self.assertEqual(terms["undefined"]["status"], "unresolved")
        self.assertEqual(terms["sector"]["status"], "conflicting")
        self.assertEqual(terms["sector"]["definitions"], ["a region", "an interval"])
        self.assertIsNone(state["symbols"][0]["meaning"])
        self.assertEqual((project.root / "main.tex").read_text(), text)

    def test_definition_environment_quotes_documented_meaning(self):
        state = ResearchKnowledge(project_with({"main.tex":
            "\\begin{definition}[Sector]\nA connected region.\n\\end{definition}"})).initialize()
        self.assertEqual(state["terms"][0]["name"], "Sector")
        self.assertEqual(state["terms"][0]["meaning"], "A connected region.")

    def test_explicit_symbol_definitions_keep_original_sentence_and_location(self):
        text = "intro\n$x$ denotes a real number.\nLet $\\ell_+$ be the positive exponent.\n\\(y\\) is defined as the input."
        state = ResearchKnowledge(project_with({"main.tex": text})).initialize()
        symbols = {s["notation"]: s for s in state["symbols"]}
        self.assertEqual(symbols["x"]["meaning"], "a real number")
        self.assertEqual(symbols["x"]["status"], "documented")
        evidence = symbols["x"]["definition_sources"][0]
        self.assertEqual((evidence["path"], evidence["line"], evidence["definition"]),
                         ("main.tex", 2, "$x$ denotes a real number."))
        self.assertEqual(symbols[r"\ell_+"]["meaning"], "the positive exponent")
        self.assertEqual(symbols["y"]["meaning"], "the input")

    def test_conflicting_symbol_definitions_preserve_both_sources(self):
        project = project_with({"main.tex": "$x$ denotes a real number.\n\\input{other}",
                                "other.tex": "Let $x$ be a vector."})
        state = ResearchKnowledge(project).initialize()
        entry = state["symbols"][0]
        self.assertEqual(entry["status"], "conflicting")
        self.assertEqual(entry["definitions"], ["a real number", "a vector"])
        self.assertEqual([s["path"] for s in entry["definition_sources"]], ["main.tex", "other.tex"])
        self.assertEqual(entry["definition_sources"][1]["definition"], "Let $x$ be a vector.")

    def test_equations_and_multiple_identifier_expressions_do_not_gain_definitions(self):
        text = "$x+y$ denotes a total.\n$x+1$ denotes a shifted input.\n$x$ is positive.\n$x=2$"
        state = ResearchKnowledge(project_with({"main.tex": text})).initialize()
        self.assertTrue(all(s["meaning"] is None and s["status"] == "unresolved" for s in state["symbols"]))

    def test_missing_excluded_and_large_includes_are_reported(self):
        project = project_with({"main.tex": "\\input{missing}\n\\input{hidden}\n\\input{large}",
                                "hidden.tex": "$secret$", "large.tex": "x" * 30,
                                "prism.json": '{"exclude":["hidden.tex"]}'})
        with mock.patch("research.MAX_TEXT", 25):
            state = ResearchKnowledge(project).initialize()
        # Main itself is too large under the first limit, so retry with a focused size fixture.
        self.assertTrue(state["coverage"]["unread"])
        project = project_with({"main.tex": "\\input{missing}\\input{hidden}\\input{large}",
                                "hidden.tex": "$secret$", "large.tex": "x" * 200,
                                "prism.json": '{"exclude":["hidden.tex"]}'})
        with mock.patch("research.MAX_TEXT", 100):
            state = ResearchKnowledge(project).initialize()
        self.assertEqual(state["coverage"]["read"], ["main.tex"])
        self.assertTrue(any(s["path"] == "missing" for s in state["coverage"]["unread"]))
        self.assertTrue(any(s["path"] == "hidden.tex" for s in state["coverage"]["excluded"]))
        self.assertTrue(any(s["path"] == "large.tex" for s in state["coverage"]["unread"]))

    def test_includes_never_read_external_or_hidden_files(self):
        base = tmpdir()
        outside = base / "private.tex"
        outside.write_text("$SECRET$", encoding="utf-8")
        root = base / "project"
        root.mkdir()
        (root / "main.tex").write_text("\\input{../private}\\input{.hidden}", encoding="utf-8")
        (root / ".hidden.tex").write_text("$HIDDEN$", encoding="utf-8")
        state = ResearchKnowledge(Project(root)).initialize()
        self.assertEqual(state["coverage"]["read"], ["main.tex"])
        self.assertEqual(state["symbols"], [])
        self.assertEqual(len(state["coverage"]["excluded"]), 2)

    def test_external_record_symlink_and_folder_link_are_refused(self):
        base = tmpdir()
        outside = base / "outside"
        outside.mkdir()
        project = project_with()
        try:
            (project.root / "research").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic links are unavailable")
        with self.assertRaises(ValueError):
            ResearchKnowledge(project).initialize()
        self.assertEqual(list(outside.iterdir()), [])

    def test_corrupt_record_remains_a_blocked_reference_until_resolved(self):
        project = project_with({RECORD: "{broken"})
        knowledge = ResearchKnowledge(project)
        state = knowledge.status()
        self.assertFalse(state["initialized"])
        self.assertEqual(state["provenance"]["record_state"], "malformed")
        self.assertTrue(state["provenance"]["write_blocked"])
        with self.assertRaises(ValueError):
            knowledge.initialize()
        self.assertEqual((project.root / RECORD).read_text(), "{broken")

    def test_record_from_another_project_cannot_authorize_this_project(self):
        first, second = project_with({"main.tex": "$z$"}), project_with({"main.tex": "$z$"})
        ResearchKnowledge(first).initialize()
        (second.root / "research").mkdir()
        received = (first.root / RECORD).read_bytes()
        (second.root / RECORD).write_bytes(received)
        knowledge = ResearchKnowledge(second)
        state = knowledge.status()
        self.assertFalse(state["initialized"])
        self.assertEqual(state["provenance"]["record_state"], "foreign")
        self.assertEqual(knowledge.classify("$z$")["known"], [])
        self.assertEqual((second.root / RECORD).read_bytes(), received)
        self.assertEqual((second.root / "main.tex").read_text(), "$z$")

    def test_malformed_received_source_keeps_trusted_basis_and_original_bytes(self):
        project = project_with({"main.tex": "$x$"})
        knowledge = ResearchKnowledge(project)
        knowledge.initialize()
        path = project.root / RECORD
        original = json.loads(path.read_text())
        for source in ("not an object", {**original["sources"][0], "text": "$z$"}):
            changed = {**original, "sources": [source]}
            path.write_text(json.dumps(changed))
            received = path.read_bytes()
            with self.subTest(source=source):
                state = knowledge.status()
                self.assertTrue(state["initialized"])
                self.assertTrue(state["provenance"]["write_blocked"])
                self.assertEqual([item["canonical"] for item in knowledge.classify("$x$")["known"]], ["x"])
                self.assertEqual(knowledge.classify("$z$")["known"], [])
                self.assertEqual(path.read_bytes(), received)

    def test_save_failure_does_not_report_initialization_success_or_notify(self):
        project = project_with({"main.tex": "$x$"})
        project.on_change = mock.Mock()
        with mock.patch("research.replace_file", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                ResearchKnowledge(project).initialize()
        self.assertEqual(ResearchKnowledge(project).status(), {"initialized": False})
        project.on_change.assert_not_called()

    def test_no_initialization_during_active_turn(self):
        project = project_with()
        project.turn = mock.Mock(**{"busy.return_value": True})
        with self.assertRaisesRegex(ValueError, "Turn"):
            ResearchKnowledge(project).initialize()
        self.assertFalse((project.root / RECORD).exists())

    def test_report_corrections_keep_sources_and_removal_evidence(self):
        project = project_with({"main.tex": "\\emph{sector} means a region.\n$x+y$"})
        knowledge = ResearchKnowledge(project)
        before = knowledge.initialize()
        after = knowledge.correct([{ "name": "sector", "meaning": "the closed region"}], ["y"], "y was a typo")
        self.assertEqual(after["terms"][0]["meaning"], "the closed region")
        self.assertEqual(after["terms"][0]["sources"], before["terms"][0]["sources"])
        self.assertEqual([s["notation"] for s in after["symbols"]], ["x"])
        self.assertEqual(after["corrections"][0]["removed_symbols"][0]["notation"], "y")
        self.assertEqual(ResearchKnowledge(Project(project.root)).status(), after)

    def test_failed_correction_preserves_previous_record(self):
        project = project_with({"main.tex": "\\emph{sector} means a region."})
        knowledge = ResearchKnowledge(project)
        before = knowledge.initialize()
        with mock.patch("research.replace_file", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                knowledge.correct([{ "name": "sector", "meaning": "new"}])
        self.assertEqual(knowledge.status(), before)

    def test_invalid_corrections_never_change_permission_or_record(self):
        project = project_with({"main.tex": "$x$"})
        knowledge = ResearchKnowledge(project)
        before = knowledge.initialize()
        for kwargs in ({"excluded_symbols": ["unknown"]},
                       {"term_updates": [{"name": "invented", "meaning": "fact"}]},
                       {"symbol_updates": [{"notation": "y", "meaning": "invented"}]},
                       {"symbol_updates": [{"notation": "x", "meaning": "fact", "status": "approved"}]},
                       {"term_updates": {}}, {"notes": 12}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                knowledge.correct(**kwargs)
            self.assertEqual(knowledge.status(), before)


if __name__ == "__main__":
    unittest.main()
