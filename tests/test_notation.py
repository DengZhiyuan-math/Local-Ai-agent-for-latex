"""Identifier spelling checks; no model, filesystem adoption or approval authority."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from notation import _canonical, math_fragments, math_notations


class CompleteNotation(unittest.TestCase):
    def names(self, text, **kwargs):
        return [s["canonical"] for s in math_notations(text, **kwargs)["symbols"]]

    def test_long_expression_is_existing_identifiers_and_operations(self):
        parsed = math_notations(r"$-\rho+2\ell-a+b-c+d-e$")
        self.assertEqual([s["canonical"] for s in parsed["symbols"]],
                         [r"\rho", r"\ell", "a", "b", "c", "d", "e"])
        self.assertEqual(parsed["gaps"], [])

    def test_complete_labels_stay_distinct_and_equivalent_spellings_match(self):
        self.assertEqual(self.names(r"$\ell+\ell_++\ell_{+}+x_i+x_{i}+x_\alpha+x_{\alpha}$"),
                         [r"\ell", r"\ell_+", r"\ell_+", "x_i", "x_i", r"x_\alpha", r"x_\alpha"])

    def test_prime_spellings_are_the_same_identifier(self):
        self.assertEqual(self.names(r"$f'+f′+f^\prime+f^{\prime}+f^{\prime\prime}+f''$"),
                         ["f'", "f'", "f'", "f'", "f''", "f''"])
        self.assertEqual(self.names(r"$x_i'+x'_i+x_{i}′$"), ["x'_i"] * 3)

    def test_label_superscripts_and_script_order_are_preserved(self):
        self.assertEqual(self.names(r"$x^++x^{+}+x^R+x^{\mathrm{in}}+x^+_i+x_i^+$"),
                         ["x^+", "x^+", "x^R", r"x^{\mathrm{in}}", "x_i^+", "x_i^+"])
        self.assertEqual(_canonical("x^+_i"), _canonical("x_i^+"))

    def test_powers_are_operations_not_new_identifiers(self):
        parsed = math_notations(r"$x^2+x^{n+1}+x^\alpha+x_i^{2n}+x^2_i$")
        self.assertEqual([s["canonical"] for s in parsed["symbols"]],
                         ["x", "x", "n", "x", r"\alpha", "x_i", "n", "x_i"])
        self.assertEqual(parsed["gaps"], [])

    def test_power_identifiers_retain_source_lines(self):
        parsed = math_notations("intro\n\\[\nx^{\ny+1}\n\\]")
        self.assertEqual([(s["notation"], s["line"]) for s in parsed["symbols"]], [("x", 3), ("y", 4)])

    def test_ensuremath_reads_complete_notation_and_nested_arguments(self):
        source = "intro\n\\ensuremath{\\ell_+ + \\frac{x_i}{y}}"
        parsed = math_notations(source)
        self.assertEqual([(s["canonical"], s["line"]) for s in parsed["symbols"]],
                         [(r"\ell_+", 2), ("x_i", 2), ("y", 2)])
        self.assertEqual(parsed["gaps"], [])
        self.assertEqual(math_fragments(source), [r"\ell_+ + \frac{x_i}{y}"])

    def test_standard_operators_and_layout_are_not_identifiers(self):
        parsed = math_notations(r"$\frac{x}{y}+\sin x+\int_a^b f+\sum_{j=1}^n z_j+\left(\quad t\right)$")
        self.assertEqual([s["canonical"] for s in parsed["symbols"]],
                         ["x", "y", "x", "a", "b", "f", "j", "n", "z_j", "t"])
        self.assertEqual(parsed["gaps"], [])

    def test_styles_and_simple_decorations_form_complete_identifiers(self):
        parsed = math_notations(r"$\mathrm{GL}_n+\mathbb{R}+\hat x+\hat{x}+\mathbf{x_i}+\hat{\ell_+}+\mathbf{\alpha_i}$")
        self.assertEqual([s["canonical"] for s in parsed["symbols"]],
                         [r"\mathrm{GL}_n", r"\mathbb{R}", r"\hat{x}", r"\hat{x}", r"\mathbf{x_i}",
                          r"\hat{\ell_+}", r"\mathbf{\alpha_i}"])
        self.assertEqual(parsed["gaps"], [])

    def test_styling_a_number_or_operator_does_not_introduce_an_identifier(self):
        parsed = math_notations(r"$\mathrm{2}+\mathbf{\nabla}+\boldsymbol{\sum}_{i=1}^n x_i$")
        self.assertEqual([s["canonical"] for s in parsed["symbols"]], ["i", "n", "x_i"])
        self.assertEqual(parsed["gaps"], [])

    def test_text_and_format_arguments_do_not_become_identifiers(self):
        parsed = math_notations(r"$x+\text{new names}+\label{label}+\phantom{hidden}+\color{red}y+\operatorname{sin}z$")
        self.assertEqual([s["canonical"] for s in parsed["symbols"]], ["x", "y", "z"])
        self.assertEqual(parsed["gaps"], [])

    def test_math_nested_in_text_arguments_keeps_identifiers_and_source_lines(self):
        parsed = math_notations("intro\n\\[\\text{ordinary words\n$z_i$ and \\mbox{\\(y\\)}}\\]")
        self.assertEqual([(s["canonical"], s["line"]) for s in parsed["symbols"]], [("z_i", 3), ("y", 3)])
        self.assertEqual(parsed["gaps"], [])
        self.assertEqual(self.names(r"$x+\text{words $z$}+\mbox{\ensuremath{y}}$"), ["x", "z", "y"])

    def test_unclosed_math_inside_text_is_a_gap(self):
        parsed = math_notations(r"\[\text{words \(z}\]")
        self.assertEqual(parsed["symbols"], [])
        self.assertTrue(parsed["gaps"])

    def test_array_and_alignment_parameters_are_layout_not_symbols(self):
        source = (r"$\begin{array}[t]{>{\mathrm{red}}cc} x&y\end{array}"
                  r"+\begin{alignedat}[b]{2}a&=b\end{alignedat}$")
        parsed = math_notations(source)
        self.assertEqual([s["canonical"] for s in parsed["symbols"]], ["x", "y", "a", "b"])
        self.assertEqual(parsed["gaps"], [])

    def test_outer_alignat_column_count_is_not_an_identifier(self):
        source = "\\begin{alignat*}{2}\nx&=y\n\\end{alignat*}"
        parsed = math_notations(source)
        self.assertEqual([(s["notation"], s["line"]) for s in parsed["symbols"]], [("x", 2), ("y", 2)])
        self.assertEqual(parsed["gaps"], [])
        self.assertEqual(math_fragments(source), ["{2}\nx&=y\n"])

    def test_malformed_known_layout_stays_a_gap_and_raw_math_is_retained(self):
        source = r"\begin{alignat*}new mathematics\end{alignat*}"
        self.assertEqual(math_fragments(source), ["new mathematics"])
        parsed = math_notations(source)
        self.assertEqual(parsed["symbols"], [])
        self.assertIn("layout argument", parsed["gaps"][0]["reason"])

    def test_starred_spacing_and_optional_layout_arguments_are_not_symbols(self):
        parsed = math_notations(r"$\hspace*{2pt}\hspace * {4pt}\vspace*{3pt}\rule[1pt]{2pt}{3pt}"
                               r"\color[RGB]{255,0,0}x+\textcolor[rgb]{red}{y}+\smash[t]{z}+\tag*{eq:layout}$")
        self.assertEqual([s["canonical"] for s in parsed["symbols"]], ["x", "y", "z"])
        self.assertEqual(parsed["gaps"], [])

    def test_unknown_environment_does_not_guess_argument_or_body_semantics(self):
        parsed = math_notations(r"$\begin{custom}{column names}x\end{custom}+y$")
        self.assertEqual([s["notation"] for s in parsed["symbols"]], ["y"])
        self.assertIn("unknown mathematical environment", parsed["gaps"][0]["reason"])

    def test_nested_unknown_environment_is_not_resumed_as_known_math(self):
        parsed = math_notations(r"$\begin{custom}\begin{custom}x\end{custom}hidden\end{custom}+y$")
        self.assertEqual([s["notation"] for s in parsed["symbols"]], ["y"])
        self.assertEqual(len(parsed["gaps"]), 1)

    def test_unknown_macro_is_a_gap_and_its_prose_arguments_are_not_adopted(self):
        parsed = math_notations(r"$\unknown{new names}+y$")
        self.assertEqual([s["notation"] for s in parsed["symbols"]], ["y"])
        self.assertEqual(parsed["gaps"][0]["notation"], r"\unknown{new names}")
        self.assertIn("unknown", parsed["gaps"][0]["reason"])

    def test_explicit_known_macro_names_are_classified_without_inventing_meaning(self):
        parsed = math_notations(r"$\sector_++\sector_{+}$", known_commands={r"\sector"})
        self.assertEqual([s["canonical"] for s in parsed["symbols"]], [r"\sector_+"] * 2)
        self.assertEqual(parsed["gaps"], [])
        self.assertNotIn("meaning", parsed["symbols"][0])

    def test_a_macro_group_is_not_assumed_to_be_visible_mathematics(self):
        parsed = math_notations(r"$\sector{hidden words}+y$", known_commands={"sector"})
        self.assertEqual([s["notation"] for s in parsed["symbols"]], ["y"])
        self.assertEqual(parsed["gaps"][0]["notation"], r"\sector{hidden words}")

    def test_compound_and_ambiguous_decorations_are_explicit_gaps(self):
        for source in (r"$x_{n+1}$", r"$x^{(r)}$", r"$x_i_j$", r"$x^2^3$", r"$\hat{x+y}$", r"$\overset{a}{x}$"):
            with self.subTest(source=source):
                parsed = math_notations(source)
                self.assertTrue(parsed["gaps"])
                self.assertEqual(parsed["symbols"], [])

    def test_greek_unicode_is_not_exempt_and_equivalent_glyphs_match(self):
        self.assertEqual(self.names(r"$\alpha+α+\ell+ℓ+\varepsilon+ε+\epsilon+ϵ$"),
                         [r"\alpha", r"\alpha", r"\ell", r"\ell", r"\varepsilon", r"\varepsilon", r"\epsilon", r"\epsilon"])

    def test_unresolved_unicode_decorations_do_not_collapse_to_the_base(self):
        for spelling in ("x₊", "x₀", "x⁺", "x″", "x̂", "x⃗"):
            with self.subTest(spelling=spelling):
                parsed = math_notations("$" + spelling + "$")
                self.assertEqual(parsed["symbols"], [])
                self.assertEqual(parsed["gaps"][0]["notation"], spelling)
        self.assertEqual(self.names("$x²$"), ["x"])

    def test_comments_and_markdown_percent_have_different_semantics(self):
        source = "50% progress; $z$\n$x$"
        self.assertEqual(math_fragments(source), ["x"])
        self.assertEqual(math_fragments(source, latex=False), ["z", "x"])
        self.assertEqual(self.names(source), ["x"])
        self.assertEqual(self.names(source, latex=False), ["z", "x"])

    def test_unclosed_math_is_retained_as_a_gap(self):
        source = "body\n\\(x+1"
        self.assertEqual(math_fragments(source), ["x+1"])
        self.assertEqual(math_notations(source)["symbols"], [])
        self.assertEqual(math_notations(source)["gaps"][0]["line"], 2)

    def test_mismatched_environment_star_does_not_close_the_span(self):
        parsed = math_notations(r"\begin{align*}x\end{align}")
        self.assertEqual(parsed["symbols"], [])
        self.assertIn("Unclosed", parsed["gaps"][0]["reason"])

    def test_occurrence_limit_stops_with_an_explicit_gap(self):
        with mock.patch("notation.MAX_NOTATIONS", 2):
            parsed = math_notations("$x+y+z$")
        self.assertEqual([s["notation"] for s in parsed["symbols"]], ["x", "y"])
        self.assertIn("limit", parsed["gaps"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
