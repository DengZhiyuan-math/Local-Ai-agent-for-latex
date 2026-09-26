"""Tests for the editor server's own logic (prism_local/server.py): project files, symbols."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import server  # noqa: E402


def project(files: dict) -> Path:
    root = Path(tempfile.mkdtemp())
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    server.set_root(root)
    return server.ROOT


class Symbols(unittest.TestCase):
    def test_theorem_names_and_their_titles(self):
        project({"main.tex": r"""\documentclass{amsart}
\newtheorem{thm}{Theorem}[section]
\newtheorem{lem}[thm]{Lemma}
\newtheorem*{rem*}{Remark}
\declaretheorem[name=Assumption, numberwithin=section]{assump}
\declaretheorem{claim}
% \newtheorem{old}{Old}
\begin{document}
\section{Intro}\label{sec:intro}
\begin{lem}\label{lem:a}
\end{lem}
\begin{thm}[Main]
\end{thm}
\end{document}
"""})
        s = server.symbols()
        self.assertEqual(s["env_titles"], {"thm": "Theorem", "lem": "Lemma", "rem*": "Remark",
                                           "assump": "Assumption", "claim": "Claim"})
        self.assertEqual([(o["kind"], o["title"]) for o in s["outline"]],
                         [("section", "Intro"), ("lem", ""), ("thm", "Main")])
        self.assertEqual({l["label"]: l["kind"] for l in s["labels"]},
                         {"sec:intro": "section", "lem:a": "lem"})

    def test_standard_names_without_newtheorem(self):
        project({"main.tex": "\\documentclass{article}\\begin{document}\\end{document}\n"})
        self.assertEqual(server.symbols()["env_titles"]["lemma"], "Lemma")


if __name__ == "__main__":
    unittest.main()
