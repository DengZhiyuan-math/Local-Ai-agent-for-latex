"""Tests for the editor server's own logic (prism_local/server.py): saving and project
files, git state, symbols."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
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


class Files(unittest.TestCase):
    def test_only_editable_files_inside_the_project(self):
        root = project({"main.tex": "", "fig.tikz": "", "build/main.log": "", "build/x.tex": "",
                        ".git/config": "", "notes/.hidden/x.tex": "", "img.png": ""})
        self.assertEqual(server.resolve("fig.tikz"), root / "fig.tikz")
        for bad in ("../main.tex", "/etc/passwd", "sub\\main.tex", "build/main.log", "build/x.tex",
                    ".git/config", "notes/.hidden/x.tex", "img.png", ""):
            with self.assertRaises(ValueError, msg=bad):
                server.resolve(bad)

    def test_save_keeps_the_files_line_ends(self):
        root = project({})
        (root / "crlf.tex").write_bytes(b"a\r\nb\r\n")
        (root / "lf.tex").write_bytes(b"a\nb\n")
        for name in ("crlf.tex", "lf.tex"):
            r, code = server.save_file(name, "a\nb\nc\n", server.mtime(root / name), False)
            self.assertEqual(code, 200, r)
        self.assertEqual((root / "crlf.tex").read_bytes(), b"a\r\nb\r\nc\r\n")
        self.assertEqual((root / "lf.tex").read_bytes(), b"a\nb\nc\n")
        server.save_file("new.tex", "x\ny\n", None, False)
        self.assertEqual((root / "new.tex").read_bytes(), b"x\ny\n")

    def test_save_refuses_to_overwrite_a_newer_file(self):
        root = project({"main.tex": "old\n"})
        loaded = server.mtime(root / "main.tex")
        time.sleep(0.02)
        (root / "main.tex").write_text("changed on disk\n", encoding="utf-8")
        r, code = server.save_file("main.tex", "mine\n", loaded, False)
        self.assertEqual((code, r["conflict"]), (409, True))
        self.assertEqual((root / "main.tex").read_text(encoding="utf-8"), "changed on disk\n")
        r, code = server.save_file("main.tex", "mine\n", loaded, True)
        self.assertEqual((root / "main.tex").read_text(encoding="utf-8"), "mine\n")

    @unittest.skipUnless(os.name == "nt", "Windows file sharing")
    def test_save_while_another_program_reads_the_file(self):
        root = project({"main.tex": "old\n"})
        reader = subprocess.Popen([sys.executable, "-c", "import sys,time; f=open(sys.argv[1]); "
                                   "print('open', flush=True); time.sleep(4)", str(root / "main.tex")],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(reader.stdout.close)
        self.addCleanup(reader.wait)
        reader.stdout.readline()               # the file is open now, as during a TeX run
        r, code = server.save_file("main.tex", "new\n", server.mtime(root / "main.tex"), False)
        self.assertEqual(code, 200)
        self.assertEqual((root / "main.tex").read_text(encoding="utf-8"), "new\n")


@unittest.skipUnless(shutil.which("git"), "needs git")
class GitState(unittest.TestCase):
    def git(self, cwd, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                       check=True, capture_output=True)

    def test_project_inside_a_larger_repository(self):
        top = Path(tempfile.mkdtemp())
        paper = top / "papers" / "我的 论文"
        paper.mkdir(parents=True)
        (paper / "main.tex").write_text("a\n", encoding="utf-8")
        (top / "README.md").write_text("r\n", encoding="utf-8")
        self.git(top, "init", "-q")
        self.git(top, "add", "-A")
        self.git(top, "commit", "-qm", "init")
        (paper / "main.tex").write_text("b\n", encoding="utf-8")
        (paper / "new file.tex").write_text("n\n", encoding="utf-8")
        (top / "README.md").write_text("changed\n", encoding="utf-8")
        server.set_root(paper)
        self.assertEqual(server.git_status(), {"main.tex": "M", "new file.tex": "??"})
        diff = server.git("diff", "--no-color", "--relative", "--", ".").stdout
        self.assertIn("a/main.tex", diff)
        self.assertNotIn("README", diff)


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
