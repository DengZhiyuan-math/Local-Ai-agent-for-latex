"""Conservative interpretation of original ordinary human notation instructions."""
from pathlib import Path
import os
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))


class ResearchLanguage(unittest.TestCase):
    def test_long_prime_suffix_completes_in_a_bounded_child_process(self):
        source = Path(__file__).resolve().parents[1] / "prism_local"
        code = ("from research_language import parse_actions; "
                "result = parse_actions(\"Use a\" + \"'\" * 48 + \" not a complete instruction\", []); "
                "assert result['actions'] == []; assert result['clarifications']")
        completed = subprocess.run([sys.executable, "-c", code],
            env={**os.environ, "PYTHONPATH": str(source)}, capture_output=True, text=True, timeout=2)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def proposals(self):
        return [{"canonical": n, "notation": n, "needs_details": False,
                 "definition": "The sum.", "scope": "The proof.",
                 "source_expression": "x+x", "status": "pending"} for n in ("a", "b", "c")]

    def test_direct_adoption_has_its_explicit_expression_and_no_proposal_target(self):
        from research_language import parse_actions
        for text in ("Use $h$ for $x+x$.", "用 $h$ 表示 $x+x$。"):
            with self.subTest(text=text):
                self.assertEqual(parse_actions(text, []), {"actions": [{
                    "kind": "adopt", "notation": "h", "definition": "x+x",
                    "source_expression": "x+x", "scope": "project"}], "clarifications": []})

    def test_batch_actions_are_separate_and_do_not_adopt_unmentioned_items(self):
        from research_language import parse_actions
        result = parse_actions("Approve $a$, reject $b$, and use $d$ instead of $c$.", self.proposals())
        self.assertEqual(result, {"actions": [
            {"kind": "approve", "target": "a"},
            {"kind": "reject", "target": "b", "rejection_scope": "name"},
            {"kind": "replace", "target": "c", "notation": "d",
             "definition": "The sum.", "scope": "The proof.", "source_expression": "x+x"}],
            "clarifications": []})

    def test_targeted_chinese_and_ordered_item_replies_select_only_their_item(self):
        from research_language import parse_actions
        result = parse_actions("第一项同意；拒绝第二项；$c$ 改用 $d$。", self.proposals())
        self.assertEqual([a["kind"] for a in result["actions"]], ["approve", "reject", "replace"])
        self.assertEqual([a["target"] for a in result["actions"]], ["a", "b", "c"])
        self.assertEqual(result["clarifications"], [])

    def test_only_ambiguous_part_is_left_for_clarification(self):
        from research_language import parse_actions
        result = parse_actions("Approve $a$; maybe reject $b$; use $d$ instead of $c$.", self.proposals())
        self.assertEqual([a["target"] for a in result["actions"]], ["a", "c"])
        self.assertEqual(len(result["clarifications"]), 1)
        for text in ("Approve $a$ if the estimate works.", "如果估计成立，同意 $a$。", "Approve $a$ or $b$?"):
            with self.subTest(text=text):
                result = parse_actions(text, self.proposals())
                self.assertEqual(result["actions"], [])
                self.assertTrue(result["clarifications"])

    def test_quoted_reported_or_model_text_and_unrelated_work_do_not_authorize(self):
        from research_language import parse_actions
        for text in ('"Approve $a$; reject $b$."', '> Approve $a$\n> Reject $b$',
                     '"\nApprove $a$;\nReject $b$;\nUse $d$ instead of $c$.\n"',
                     '「同意 $a$；拒绝 $b$；同意 $c$。」',
                     'The document says: Approve $a$; reject $b$.',
                     '模型说：同意 $a$；拒绝 $b$。',
                     '```\nApprove $a$\nReject $b$\n```',
                     'The proof needs another estimate.', 'Yes', 'No'):
            with self.subTest(text=text):
                self.assertIsNone(parse_actions(text, self.proposals()))

    def test_structural_quotations_cannot_be_actions_or_plain_replies(self):
        from research_decisions import parse_reply
        from research_language import parse_actions
        wrappers = (
            "~~~text\n{}\n~~~", "~~~~~\n{}\n~~~~~", "   ~~~\n{}\n   ~~~",
            "    {}", "\t{}", " \t{}", "> Quoted instruction:\n{}",
            "<blockquote>\n{}\n</blockquote>", "<pre>\n{}\n</pre>",
            "<code>\n{}\n</code>", "<q>\n{}\n</q>", "<!--\n{}\n-->")
        for wrapper in wrappers:
            with self.subTest(wrapper=wrapper):
                self.assertIsNone(parse_actions(wrapper.format("Use $h$ for $x+x$."), []))
                self.assertIsNone(parse_actions(wrapper.format("Approve $a$."), self.proposals()))
                self.assertIsNone(parse_reply(wrapper.format("Yes")))
                self.assertIsNone(parse_reply(wrapper.format("No")))
        self.assertEqual(parse_reply("   Yes\n"), "approved")
        self.assertEqual(parse_actions("   Use $h$ for $x+x$.", [])["actions"][0]["notation"], "h")

    def test_conditional_scope_cannot_be_evaded_by_a_clause_separator(self):
        from research_language import parse_actions
        for text in ('If the estimate is valid; approve $a$; use $d$ instead of $c$.',
                     '如果估计成立；同意 $a$；拒绝 $b$。'):
            with self.subTest(text=text):
                result = parse_actions(text, self.proposals())
                self.assertEqual(result["actions"], [])
                self.assertTrue(result["clarifications"])

    def test_direct_adoption_does_not_require_math_delimiters_or_a_special_reply(self):
        from research_language import parse_actions
        for text, definition in (("用 h 表示 x+x", "x+x"), ("Use h for x+x.", "x+x"),
                                 ("Use h for $x+x$.", "x+x"),
                                 ("Use $h$ to denote the error term.", "the error term"),
                                 ("Use h to denote the error term.", "the error term")):
            with self.subTest(text=text):
                self.assertEqual(parse_actions(text, []), {"actions": [{"kind": "adopt",
                    "notation": "h", "definition": definition, "source_expression": definition,
                    "scope": "project"}], "clarifications": []})
        for text in ("Use h.", "用 h 表示", "Use h for", "Use h for x+x if it works."):
            with self.subTest(text=text):
                result = parse_actions(text, [])
                self.assertEqual(result["actions"], [])
                self.assertTrue(result["clarifications"])
        for text in ('"Use h for x+x."', 'The model says use h for x+x.'):
            self.assertIsNone(parse_actions(text, []))

    def test_unreviewed_or_incomplete_item_does_not_supply_its_missing_definition(self):
        from research_language import parse_actions
        proposals = self.proposals()
        proposals[0]["needs_details"] = True
        for text in ('Approve $a$.', 'Approve $q$.', 'Use $q$ instead of $a$.', 'Use $q$.'):
            with self.subTest(text=text):
                result = parse_actions(text, proposals)
                self.assertEqual(result["actions"], [])
                self.assertTrue(result["clarifications"])

    def test_rejecting_the_name_differs_from_keeping_its_expression_expanded(self):
        from research_language import parse_actions
        ordinary = parse_actions("Reject $a$.", self.proposals())["actions"][0]
        self.assertEqual(ordinary["rejection_scope"], "name")
        self.assertNotIn("source_expression", ordinary)
        for text in ("Reject $a$ and keep its expression expanded.",
                     "拒绝 $a$，保持原式展开。"):
            with self.subTest(text=text):
                result = parse_actions(text, self.proposals())
                self.assertEqual(result, {"actions": [{"kind": "reject", "target": "a",
                    "rejection_scope": "expression", "source_expression": "x+x"}], "clarifications": []})

    def test_only_an_explicit_reopen_is_an_action(self):
        from research_language import parse_actions
        for text in ("Reopen $a$.", "重新考虑 $a$。"):
            with self.subTest(text=text):
                self.assertEqual(parse_actions(text, self.proposals()),
                                 {"actions": [{"kind": "reopen", "target": "a"}], "clarifications": []})
        self.assertIsNone(parse_actions("This has been rejected before.", self.proposals()))

    def test_finite_family_records_only_the_explicit_indices(self):
        from research_language import family_matches, parse_actions
        for text in ("Use $a_i$ for $x+x$ with i in {1,2,3}.",
                     "用 $a_i$ 表示 $x+x$，其中 i 属于 {1,2,3}。"):
            with self.subTest(text=text):
                result = parse_actions(text, [])
                self.assertEqual(result, {"actions": [{"kind": "family", "notation": "a_i",
                    "definition": "x+x", "source_expression": "x+x", "scope": "project",
                    "rule": {"base": "a", "indices": ["1", "2", "3"]}}], "clarifications": []})
                rule = result["actions"][0]["rule"]
                for notation in ("a_1", "a_{2}", "a_3"):
                    self.assertTrue(family_matches(notation, rule))
                for notation in ("a", "a_i", "a_4", "b_1", "a_1^2", "a'_1", "a_{1+2}"):
                    self.assertFalse(family_matches(notation, rule))

    def test_family_rule_cannot_become_a_regex_or_wildcard(self):
        from research_language import family_matches, parse_actions
        for text in ("Use $a_i$ for $x+x$ with i in {*}",
                     "Use $a_i$ for $x+x$ with j in {1,2}",
                     "Use $a_i$ for $x+x$ with i in {1,2,...}"):
            with self.subTest(text=text):
                result = parse_actions(text, [])
                self.assertEqual(result["actions"], [])
                self.assertTrue(result["clarifications"])
        for rule in ({"base": ".*", "indices": ["1"]}, {"base": "a", "indices": [".*"]},
                     {"base": "a", "indices": []}, {"base": "a", "indices": "123"}):
            self.assertFalse(family_matches("a_1", rule))

    def test_explicit_expression_refusal_does_not_need_a_proposal(self):
        from research_language import parse_actions
        for text in ("Keep $x+x$ expanded; do not abbreviate it.",
                     "保持 $x+x$ 展开，不要简称。", "Do not abbreviate $x+x$."):
            with self.subTest(text=text):
                self.assertEqual(parse_actions(text, []), {"actions": [{
                    "kind": "refuse_expression", "source_expression": "x+x"}], "clarifications": []})
        result = parse_actions("Keep $x+x$ expanded if the proof is short.", [])
        self.assertEqual(result["actions"], [])
        self.assertTrue(result["clarifications"])

    def test_explicit_name_negation_is_retained_for_conflict_resolution(self):
        from research_language import parse_actions
        result = parse_actions("Use $h$ for $x+x$; don't use $h$ after all.", [])
        self.assertEqual([a["kind"] for a in result["actions"]], ["adopt", "refuse_name"])
        self.assertEqual(result["actions"][1], {"kind": "refuse_name", "target": "h", "notation": "h"})
        self.assertEqual(result["clarifications"], [])
        for text in ("Approve $a$; don't approve $a$.",
                     "Approve $a$, do not approve $a$ after all.",
                     "同意 $a$；最终不用 $a$。"):
            with self.subTest(text=text):
                result = parse_actions(text, self.proposals())
                self.assertEqual(result["actions"], [
                    {"kind": "approve", "target": "a"},
                    {"kind": "reject", "target": "a", "rejection_scope": "name"}])
                self.assertEqual(result["clarifications"], [])

    def test_clear_negative_verbs_reject_existing_items_or_refuse_the_exact_new_name(self):
        from research_language import parse_actions
        for text in ("Don't use $a$ after all.", "Do not adopt a.", "Don't approve item 1.", "最终不用 $a$。"):
            with self.subTest(text=text):
                self.assertEqual(parse_actions(text, self.proposals()), {"actions": [{
                    "kind": "reject", "target": "a", "rejection_scope": "name"}], "clarifications": []})
        for text in ("Don't use h after all.", "Do not adopt $h$.", "最终不用 $h$。"):
            with self.subTest(text=text):
                self.assertEqual(parse_actions(text, []), {"actions": [{
                    "kind": "refuse_name", "target": "h", "notation": "h"}], "clarifications": []})
        for text in ('"Do not adopt $h$."', 'The document says: do not use h.'):
            self.assertIsNone(parse_actions(text, []))
        conditional = parse_actions("Don't use h if the estimate fails.", [])
        self.assertEqual(conditional["actions"], [])
        self.assertTrue(conditional["clarifications"])


if __name__ == "__main__":
    unittest.main()
