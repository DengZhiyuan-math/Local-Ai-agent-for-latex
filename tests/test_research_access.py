"""Real shared checks before an API draft reaches a file or the chat."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from research import RECORD, ResearchKnowledge
from research_access import NotationStop, ResearchAccess
from fsutil import with_line_ends_of
from test_research import project_with


class ResearchDraftChecks(unittest.TestCase):
    def setUp(self):
        self.original = "$x + \\ell$\n"
        self.project = project_with({"main.tex": self.original})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()
        self.path = self.project.root / "main.tex"
        self.access = ResearchAccess(self.knowledge)

    def saved(self):
        return json.loads((self.project.root / RECORD).read_text(encoding="utf-8"))

    def assert_held(self, text, names=None, path=None):
        path = path or self.path
        before = path.read_bytes() if path.exists() else None
        with mock.patch("research_access.write_bytes") as apply:
            with self.assertRaises(NotationStop) as raised:
                self.access.apply_write(path, text, before)
            apply.assert_not_called()
        self.assertEqual(path.read_bytes() if path.exists() else None, before)
        if names is not None:
            self.assertEqual({p["canonical"] for p in raised.exception.proposals}, set(names))
            saved = {p["id"]: p for p in self.saved()["proposals"]}
            for proposal in raised.exception.proposals:
                self.assertEqual(saved[proposal["id"]], proposal)
                self.assertEqual(proposal["status"], "pending")
                self.assertNotIn(proposal["canonical"],
                                 {s["canonical"] for s in self.knowledge.classify("$" + proposal["notation"] + "$")["known"]})
        return raised.exception

    def received(self, text):
        """An outside source edit carries no editor adoption receipt."""
        self.path.write_text(text, encoding="utf-8")
        self.access = ResearchAccess(self.knowledge)

    def assert_received_held(self, text, names):
        from research_confirmation import questions
        held = self.assert_held(text)
        self.assertEqual(held.proposals, [])
        self.assertEqual(self.saved().get("proposals", []), [])
        self.assertEqual({question["canonical"] for question in questions(self.knowledge)}, names)
        for name in names:
            self.assertEqual(self.knowledge.classify("$" + name + "$")["known"], [])

    def test_known_notation_changes_use_without_approval(self):
        draft = "A revised estimate.\n$\\ell = x^2 + x + 1$\n"
        self.access.apply_write(self.path, draft, self.path.read_bytes())
        self.assertEqual(self.path.read_text(), draft)
        self.assertEqual(self.saved().get("proposals", []), [])
        self.assertEqual(self.access.review_output("Use $x^2 + \\ell$ in the estimate."),
                         "Use $x^2 + \\ell$ in the estimate.")

    def test_unicode_decorations_require_review_as_complete_names(self):
        for spelling in ("x₊", "x₀", "x⁺", "x″", "x̂", "x⃗"):
            with self.subTest(spelling=spelling), self.assertRaises(NotationStop) as raised:
                self.access.review_output("$" + spelling + "=x+x$")
            self.assertTrue(raised.exception.proposals)
            self.assertEqual(raised.exception.proposals[0]["notation"], spelling)

    def test_math_fence_checks_bare_remainder_on_the_same_line(self):
        for fence in ("```", "~~~"):
            for tail in ("+ z", "z_i"):
                text = fence + "math\n$x$ " + tail + "\n" + fence
                with self.subTest(text=text), self.assertRaises(NotationStop):
                    self.access.review_output(text)

    def test_source_preview_checks_full_context_without_proposing(self):
        source = "\\[\nz\n\\]\n"
        preview = self.access.review_source_preview(source, "     2\tz")
        self.assertNotEqual(preview, "     2\tz")
        self.assertEqual(self.saved().get("proposals", []), [])
        self.assertEqual(self.access.review_source_preview("\\[\nx\n\\]\n", "     2\tx"), "     2\tx")

    def test_formal_proposal_storage_failure_ends_the_turn(self):
        for error in (OSError("disk full"), ValueError("damaged research record")):
            with self.subTest(error=error), mock.patch("research.replace_file", side_effect=error):
                with self.assertRaises(NotationStop) as raised:
                    self.access.propose({"notation": "z", "definition": "Error", "scope": "Proof", "reason": "Shorten"})
                self.assertIn("could not be saved", raised.exception.error)
        self.assertEqual(self.saved().get("proposals", []), [])

    def test_api_compile_requires_initialization_without_starting_a_build(self):
        access = ResearchAccess(ResearchKnowledge(project_with({"main.tex": "$x$"})))
        with mock.patch("guarded_compile.guarded_compile") as compile_draft:
            with self.assertRaisesRegex(ValueError, "Initialize"):
                access.guarded_compile()
        compile_draft.assert_not_called()

    def test_damaged_record_blocks_even_a_prose_write_before_application(self):
        (self.project.root / RECORD).write_text("{broken")
        with mock.patch("research_access.write_bytes") as apply, self.assertRaises(NotationStop):
            self.access.apply_write(self.path, self.original + "An explanation.\n", self.path.read_bytes())
        apply.assert_not_called()
        self.assertEqual(self.path.read_text(), self.original)

    def test_ordinary_prose_is_not_a_variable_list(self):
        text = "Let us be careful. The next step explains the estimate."
        self.assertEqual(self.access.review_output(text), text)
        self.assertEqual(self.saved().get("proposals", []), [])

    def test_new_complete_decoration_is_held_before_any_file_write(self):
        held = self.assert_held("$x + \\ell_+$\n", {r"\ell_+"})
        self.assertEqual(held.proposals[0]["origin"]["path"], "main.tex")
        self.assertTrue(held.proposals[0]["needs_details"])
        self.assertEqual(held.proposals[0]["source_expression"], "x + \\ell")

    def test_plain_known_identifier_does_not_authorize_a_new_script(self):
        self.assert_held("$x_i + \\ell$\n", {"x_i"})

    def test_chat_raw_candidate_prefix_does_not_create_display_permission(self):
        with self.assertRaises(NotationStop) as raised:
            self.access.review_output("Candidate: $z = x + x$. Thus $z^2$ is small.")
        self.assertEqual({p["canonical"] for p in raised.exception.proposals}, {"z"})
        self.assertEqual(self.path.read_text(), self.original)

    def test_unclosed_math_is_not_displayed_or_applied(self):
        for draft in ("$z_i=x", r"\(z_i=x"):
            with self.subTest(draft=draft), self.assertRaises(NotationStop):
                self.access.review_output(draft)
            self.assert_held(draft)

    def test_explicit_bare_names_are_checked_in_chat(self):
        for text in ("Let z denote x+x.", "`z` denotes the error in $x+x$.",
                     "Define z = x+x.", "设 z 为误差。", "记作 z，随后使用。"):
            with self.subTest(text=text), self.assertRaises(NotationStop) as raised:
                self.access.review_output(text)
            self.assertIn("z", {p["canonical"] for p in raised.exception.proposals})

    def test_math_text_argument_cannot_hide_a_new_name(self):
        with self.assertRaises(NotationStop) as raised:
            self.access.review_output(r"$\text{Let z denote x+x.}$")
        self.assertIn("z", {p["canonical"] for p in raised.exception.proposals})

    def test_bare_unicode_greek_is_checked(self):
        with self.assertRaises(NotationStop) as raised:
            self.access.review_output("Let ζ denote $x+x$.")
        self.assertEqual({p["canonical"] for p in raised.exception.proposals}, {r"\zeta"})

    def test_latex_math_fence_without_delimiters_is_checked(self):
        for language in ("math", "tex", "latex"):
            with self.subTest(language=language), self.assertRaises(NotationStop) as raised:
                self.access.review_output("```" + language + "\nz_i=x+x\n```")
            self.assertEqual({p["canonical"] for p in raised.exception.proposals}, {"z_i"})

    def test_math_fence_checks_bare_equation_after_a_delimited_known_expression(self):
        text = "```math\n$x$\nz=x+x\n```"
        with self.assertRaises(NotationStop) as raised:
            self.access.review_output(text)
        self.assertIn("z", {p["canonical"] for p in raised.exception.proposals})
        self.assert_held(self.original + text, {"z"})

    def test_tilde_math_fence_checks_bare_equation(self):
        text = "~~~math\nz=x+x\n~~~"
        with self.assertRaises(NotationStop) as raised:
            self.access.review_output(text)
        self.assertIn("z", {p["canonical"] for p in raised.exception.proposals})
        self.assert_held(self.original + text, {"z"})

    def test_supplementary_unicode_identifier_in_naming_is_checked(self):
        for text in ("Let 𝓩 denote x+x.", "Define 𝓩 := $x+x$."):
            with self.subTest(text=text), self.assertRaises(NotationStop) as raised:
                self.access.review_output(text)
            self.assertIn("𝓩", {p["canonical"] for p in raised.exception.proposals})
            self.assert_held(self.original + text, {"𝓩"})

    def test_text_argument_cannot_hide_a_bare_equation(self):
        text = r"$\text{z=x+x}$"
        with self.assertRaises(NotationStop) as raised:
            self.access.review_output(text)
        self.assertIn("z", {p["canonical"] for p in raised.exception.proposals})
        self.assert_held(self.original + text, {"z"})

    def test_bare_equation_is_checked_before_chat_or_source_publication(self):
        text = "z=x+x"
        with self.assertRaises(NotationStop) as raised:
            self.access.review_output(text)
        self.assertIn("z", {p["canonical"] for p in raised.exception.proposals})
        self.assert_held(self.original + text, {"z"})

    def test_html_entity_cannot_hide_an_explicit_identifier(self):
        with self.assertRaises(NotationStop) as raised:
            self.access.review_output("Let &#122; denote $x+x$.")
        self.assertIn("z", {p["canonical"] for p in raised.exception.proposals})

    def test_auxiliary_preview_is_masked_without_requesting_adoption(self):
        before = (self.project.root / RECORD).read_bytes()
        for channel in ("tool", "thinking", "summary"):
            result = self.access.review_auxiliary("Read the reference: $z_i=x$", channel=channel)
            self.assertNotIn("$z_i", result)
        self.assertEqual((self.project.root / RECORD).read_bytes(), before)
        self.assertEqual(self.access.review_auxiliary("Known $x+\\ell$"), "Known $x+\\ell$")

    def test_registration_failure_never_reaches_source_write(self):
        before_record = (self.project.root / RECORD).read_bytes()
        with mock.patch("research.replace_file", side_effect=OSError("disk full")):
            held = self.assert_held("$z+x$\n")
        self.assertIn("could not be saved", held.error)
        self.assertEqual(held.proposals, [])
        self.assertEqual((self.project.root / RECORD).read_bytes(), before_record)

    def test_received_unknown_can_remain_unchanged_while_prose_changes(self):
        self.received(self.original + "Received result.\n$z=x$\n")
        draft = self.original + "Received result.\n$z=x$\nAn explanation follows.\n"
        self.access.apply_write(self.path, draft, self.path.read_bytes())
        self.assertEqual(self.path.read_text(), draft)
        self.assertEqual(self.saved().get("proposals", []), [])
        with self.assertRaises(NotationStop):
            self.access.review_output("Therefore $z=x$.")

    def test_known_math_inserted_before_received_unknown_preserves_its_location(self):
        self.received(self.original + "Original paragraph.\n$z=x$\n")
        draft = self.original + "Original paragraph.\n$x+x$\n$z=x$\n"
        self.access.apply_write(self.path, draft, self.path.read_bytes())
        self.assertEqual(self.path.read_text(), draft)
        self.assertEqual(self.saved().get("proposals", []), [])

    def test_crlf_received_unknown_survives_prose_append_without_line_end_changes(self):
        source = self.original + "Original paragraph.\n$z=x$\n"
        self.path.write_bytes(source.replace("\n", "\r\n").encode("utf-8"))
        self.access = ResearchAccess(self.knowledge)
        draft = with_line_ends_of(source + "An appended explanation.\n", self.path)
        self.access.apply_write(self.path, draft, self.path.read_bytes())
        self.assertEqual(self.path.read_bytes(), draft.encode("utf-8"))
        self.assertEqual(draft, (source + "An appended explanation.\n").replace("\n", "\r\n"))
        self.assertEqual(self.saved().get("proposals", []), [])

    def test_copying_received_unknown_to_another_location_is_held(self):
        self.received(self.original + "$z=x$\n")
        self.assert_received_held(self.path.read_text() + "A new argument.\n$z=x$\n", {"z"})

    def test_moving_received_unknown_to_another_section_is_held(self):
        self.received(self.original + "\\section{Received}\n$z=x$\n\\section{New}\n")
        self.assert_received_held(self.original + "\\section{Received}\n\\section{New}\n$z=x$\n", {"z"})

    def test_changing_received_unknown_expression_is_held(self):
        self.received(self.original + "$z=x$\n")
        self.assert_received_held(self.original + "$z=x+x$\n", {"z"})

    def test_received_equation_environment_can_remain_unchanged(self):
        source = self.original + "\\begin{align*}\nz=x\n\\end{align*}\n"
        self.received(source)
        draft = source + "Only the surrounding explanation changes.\n"
        self.access.apply_write(self.path, draft, self.path.read_bytes())
        self.assertEqual(self.path.read_text(), draft)
        self.assertEqual(self.saved().get("proposals", []), [])

    def test_changed_math_environment_cannot_republish_received_unknown(self):
        self.received(self.original + "\\begin{align*}\nz=x\n\\end{align*}\n")
        self.assert_received_held(self.original + "\\begin{equation}\nz=x\n\\end{equation}\n", {"z"})

    def test_moving_received_unknown_to_another_argument_is_held(self):
        self.received(self.original + "Received statement.\n$z=x$\nEnd received statement.\n"
                      "A new argument begins.\nEnd new argument.\n")
        self.assert_received_held(self.original + "Received statement.\nEnd received statement.\n"
                                  "A new argument begins.\n$z=x$\nEnd new argument.\n", {"z"})

    def test_moving_received_unknown_past_a_different_paragraph_is_held(self):
        self.received(self.original + "Original paragraph.\n$z=x$\nDifferent paragraph.\n")
        self.assert_received_held(self.original + "Original paragraph.\nDifferent paragraph.\n$z=x$\n", {"z"})

    def test_bare_received_name_cannot_be_moved_into_a_new_section(self):
        self.received(self.original + "\\section{Received}\nLet z denote the error.\n\\section{New}\n")
        self.assert_held(self.original + "\\section{Received}\n\\section{New}\nLet z denote the error.\n", {"z"})

    def test_new_math_macro_is_held_even_when_its_body_uses_known_names(self):
        self.assert_held(self.original + "\\newcommand{\\Z}{-x+x}\n", {r"\Z"})

    def test_new_macro_body_unknown_without_math_delimiters_is_held(self):
        self.assert_held(self.original + "\\newcommand{\\fresh}{z}\n", {r"\fresh", "z"})

    def test_existing_macro_redefinition_checks_new_body_notation(self):
        source = "\\newcommand{\\saved}{x}\n$x + \\ell + \\saved$\n"
        self.project = project_with({"main.tex": source})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()
        self.path = self.project.root / "main.tex"
        self.access = ResearchAccess(self.knowledge)
        self.assert_held(source.replace("{\\saved}{x}", "{\\saved}{z}"), {"z"})

    def test_existing_macro_known_body_change_needs_no_new_approval(self):
        source = "\\newcommand{\\saved}{x}\n$x + \\ell + \\saved$\n"
        self.project = project_with({"main.tex": source})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()
        self.path = self.project.root / "main.tex"
        self.access = ResearchAccess(self.knowledge)
        draft = source.replace("{\\saved}{x}", "{\\saved}{x+x}")
        self.access.apply_write(self.path, draft, self.path.read_bytes())
        self.assertEqual(self.path.read_text(), draft)
        self.assertEqual(self.saved().get("proposals", []), [])

    def test_intermediate_macro_redefinition_cannot_hide_behind_restored_body(self):
        source = "\\newcommand{\\saved}{x}\n$x + \\ell + \\saved$\n"
        self.project = project_with({"main.tex": source})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()
        self.path = self.project.root / "main.tex"
        self.access = ResearchAccess(self.knowledge)
        draft = ("\\newcommand{\\saved}{x}\n\\renewcommand{\\saved}{z}\n"
                 "\\renewcommand{\\saved}{x}\n$x + \\ell + \\saved$\n")
        self.assert_held(draft, {"z"})

    def test_optional_macro_default_with_unknown_notation_is_held(self):
        self.assert_held(self.original + "\\newcommand{\\fresh}[1][z]{#1}\n")

    def test_optional_default_change_on_next_line_does_not_reuse_command_line(self):
        source = self.original + "\\newcommand{\\fresh}\n[1][x]{#1}\n"
        self.received(source)
        self.assert_held(source.replace("[1][x]", "[1][z]"))

    def test_unsupported_optional_duplicate_cannot_hide_behind_valid_same_name(self):
        source = "\\newcommand{\\saved}{x}\n$x + \\ell + \\saved$\n"
        self.project = project_with({"main.tex": source})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()
        self.path = self.project.root / "main.tex"
        self.access = ResearchAccess(self.knowledge)
        self.assert_held(source + "\\renewcommand{\\saved}[1][z]{#1}\n")

    def test_let_alias_to_unknown_notation_is_held(self):
        self.assert_held(self.original + "\\let\\fresh=z\n")

    def test_ensuremath_cannot_hide_new_notation_outside_dollars(self):
        self.assert_held(self.original + "\\ensuremath{z=x}\n", {"z"})

    def test_new_plain_text_format_macro_does_not_request_notation(self):
        draft = self.original + "\\newcommand{\\strongword}{\\textbf{Proof}}\n"
        self.access.apply_write(self.path, draft, self.path.read_bytes())
        self.assertEqual(self.path.read_text(), draft)
        self.assertEqual(self.saved().get("proposals", []), [])

    def test_new_braced_and_bare_inputs_check_previously_excluded_sources(self):
        (self.project.root / "reference.tex").write_text("$z=x$\n")
        for command in (r"\input{reference}", r"\input reference", r"\include{reference}"):
            with self.subTest(command=command):
                self.assert_held(self.original + command + "\n", {"z"})

    def test_recursive_input_checks_descendant_before_reachability_changes(self):
        (self.project.root / "reference.tex").write_text("\\input{deep}\n")
        (self.project.root / "deep.tex").write_text("$z_i=x$\n")
        self.assert_held(self.original + "\\input{reference}\n", {"z_i"})

    def test_known_only_input_can_be_added_without_approval(self):
        (self.project.root / "reference.tex").write_text("$x+\\ell$\n")
        draft = self.original + "\\input{reference}\n"
        self.access.apply_write(self.path, draft, self.path.read_bytes())
        self.assertEqual(self.path.read_text(), draft)

    def test_missing_and_dynamic_inputs_keep_original_source(self):
        for command in (r"\input{missing}", r"\input{\chosen}"):
            with self.subTest(command=command):
                held = self.assert_held(self.original + command + "\n")
                self.assertTrue(held.error)

    def test_local_style_addition_does_not_skip_unknown_math(self):
        (self.project.root / "reference.sty").write_text("$z=x$\n")
        self.assert_held(self.original + "\\usepackage{reference}\n", {"z"})

    def test_conditional_input_does_not_make_unverified_source_reachable(self):
        (self.project.root / "reference.tex").write_text("$z=x$\n")
        self.assert_held(self.original + "\\InputIfFileExists{reference.tex}{}{}\n")

    def test_import_does_not_make_unverified_nested_source_reachable(self):
        (self.project.root / "parts").mkdir()
        (self.project.root / "parts" / "reference.tex").write_text("$z=x$\n")
        self.assert_held(self.original + "\\import{parts/}{reference.tex}\n")

    def test_requirepackage_does_not_make_unverified_local_style_reachable(self):
        (self.project.root / "reference.sty").write_text("$z=x$\n")
        self.assert_held(self.original + "\\RequirePackage{reference}\n")

    def test_new_dynamic_tex_construction_is_held(self):
        self.assert_held(self.original + "\\csname fresh\\endcsname\n", {r"\csname"})

    def test_stale_file_version_is_not_overwritten_or_proposed(self):
        stale = self.path.read_bytes()
        received = self.original + "A concurrent manual edit.\n"
        self.path.write_text(received)
        with mock.patch("research_access.write_bytes") as apply:
            with self.assertRaisesRegex(ValueError, "changed"):
                self.access.apply_write(self.path, "$z$", stale)
            apply.assert_not_called()
        self.assertEqual(self.path.read_text(), received)
        self.assertEqual(self.saved().get("proposals", []), [])

    def test_existing_file_cannot_be_created_again_with_absent_version(self):
        with self.assertRaisesRegex(ValueError, "changed"):
            self.access.apply_write(self.path, "$x$", None)
        self.assertEqual(self.path.read_text(), self.original)

    def test_cancelled_turn_does_not_save_source_or_pending_candidate(self):
        self.access.cancel = threading.Event()
        self.access.cancel.set()
        before = (self.project.root / RECORD).read_bytes()
        held = self.assert_held("$z$\n")
        self.assertIn("stopped", held.error)
        self.assertEqual((self.project.root / RECORD).read_bytes(), before)

    def test_stop_arriving_after_check_still_prevents_application(self):
        self.access.cancel = threading.Event()
        check = self.access.check_write

        def stop_after_check(path, text):
            check(path, text)
            self.access.cancel.set()

        with mock.patch.object(self.access, "check_write", side_effect=stop_after_check):
            self.assert_held("$x+x$\n")

    def test_agent_cannot_write_the_authoritative_record_directly(self):
        path = self.project.root / RECORD
        before = path.read_bytes()
        with self.assertRaisesRegex(NotationStop, "research interface"):
            self.access.apply_write(path, "{}", before)
        self.assertEqual(path.read_bytes(), before)

    def test_agent_cannot_write_case_variants_of_the_research_namespace(self):
        original = (self.project.root / RECORD).read_bytes()
        for relative in ("ReSeArCh/knowledge.json", "RESEARCH/first.md", "research/second.md"):
            with self.subTest(relative=relative):
                path = self.project.root / relative
                with self.assertRaisesRegex(NotationStop, "research interface"):
                    self.access.apply_write(path, "Discussion changed.", path.read_bytes() if path.exists() else None)
                if path.name != "knowledge.json":
                    self.assertFalse(path.exists())
                self.assertEqual((self.project.root / RECORD).read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
