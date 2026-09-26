"""Tests for the editor server's own logic (prism_local/server.py): saving and project
files, git state, symbols."""
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import server  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/: tmpdirs, test_lifecycle
from tmpdirs import tmpdir  # noqa: E402


def project(files: dict) -> Path:
    root = tmpdir()
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    server.set_root(root)
    return server.ROOT


class Config(unittest.TestCase):
    def test_a_bad_prism_json_is_reported_and_the_defaults_apply(self):
        project({"main.tex": "", "prism.json": '{"engine": "xetex2", "outdir": "../x", '
                                               '"build": ["pdflatex", "{main}"]}'})
        self.assertIsNone(server.CFG.engine)
        self.assertEqual(server.CFG.outdir, "build")
        for part in ("outdir", "unknown engine 'xetex2'", "build must map"):
            self.assertIn(part, server.CFG.error, "every problem is reported")
        project({"main.tex": "", "prism.json": "{not json"})
        self.assertIn("cannot be used", server.CFG.error)
        self.assertEqual((server.CFG.main, server.CFG.modes), ("main.tex", ["draft", "strict"]))

    def test_changes_apply_without_a_restart(self):
        root = project({"main.tex": "", "prism.json": '{"engine": "pdflatex"}'})
        self.assertEqual(server.CFG.engine, "pdflatex")
        time.sleep(0.02)
        (root / "prism.json").write_text('{"engine": "lualatex"}', encoding="utf-8")
        server.refresh_config()
        self.assertEqual(server.CFG.engine, "lualatex")


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

    @unittest.skipIf(os.name == "nt", "file modes: Linux and macOS")
    def test_save_keeps_the_files_permissions(self):
        root = project({"main.tex": "old\n"})
        (root / "main.tex").chmod(0o640)
        server.save_file("main.tex", "new\n", server.mtime(root / "main.tex"), False)
        self.assertEqual((root / "main.tex").stat().st_mode & 0o777, 0o640)

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
        top = tmpdir()
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

    def test_section_titles(self):
        project({"main.tex": "\\begin{document}\n\\chapter{One}\n"
                             "\\section[Short]{Long $\\frac{a}{b^{2}}$}\n"
                             "\\subsection*{\\texorpdfstring{$\\mathbb{R}^d$}{Rd} and $\\SO(n)$}\n"
                             "\\end{document}\n"})
        self.assertEqual([(o["kind"], o["title"]) for o in server.symbols()["outline"]],
                         [("chapter", "One"), ("section", "Long a/b^2"),
                          ("subsection", "Rd and SO(n)")])


@unittest.skipUnless(shutil.which("pdflatex") and not os.environ.get("PRISM_SKIP_TEX"),
                     "needs a TeX distribution")
class SyncTeX(unittest.TestCase):
    def test_source_to_pdf_and_back(self):
        import build
        root = project({})
        shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "minimal", root,
                        dirs_exist_ok=True, ignore=shutil.ignore_patterns("build"))
        server.set_root(root)
        r = build.Build(root, server.CFG.main, server.CFG.outdir).run()
        self.assertEqual(r["exit"], 0, r["output"][-2000:])
        sync = server.SyncTex()
        self.assertTrue(sync.load())
        line = next(n for n, t in enumerate((root / "sections/intro.tex").read_text(
            encoding="utf-8").splitlines(), 1) if len(t) > 40 and not t.startswith(("\\", "%")))
        box = sync.forward("sections/intro.tex", line)
        self.assertIsNotNone(box)
        back = sync.inverse(box["page"], box["x"] + 20, box["y"] + box["h"] / 2)
        self.assertEqual(back["file"], "sections/intro.tex")
        self.assertLessEqual(abs(back["line"] - line), 1)


class PlainText(unittest.TestCase):
    def test_titles_read_as_text(self):
        from texutil import plain_text
        cases = {
            r"Computation of $\sum\limits_{j=1}^{n}\langle f_j, g\rangle$":
                "Computation of Σ_j=1^n⟨ f_j, g⟩",
            r"\texorpdfstring{$\SO(n)$}{SO(n)} acting on $\mathbb{R}^d$": "SO(n) acting on ℝ^d",
            r"The \emph{main}~estimate\label{sec:main}": "The main estimate",
            r"Bounds for $\|\alpha\|_{L^2} \leq \varepsilon$": "Bounds for ‖α‖_L^2 ≤ ε",
            r"On convex sets\thanks{Funded by {X}.} \\[2pt] and cones": "On convex sets and cones",
        }
        for tex, text in cases.items():
            self.assertEqual(plain_text(tex), text, tex)


if __name__ == "__main__":
    unittest.main()
