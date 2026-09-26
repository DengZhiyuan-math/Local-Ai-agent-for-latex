"""Tests for building (prism_local/build.py).

The unit tests need nothing installed. The integration tests build small documents
with the TeX distribution on PATH (MiKTeX or TeX Live); they are skipped without one,
or when PRISM_SKIP_TEX=1. Each checks what a reader of the PDF would see after ONE
build: resolved citations and references, a bibliography, an index.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import build  # noqa: E402

HAVE_TEX = bool(shutil.which("pdflatex")) and not os.environ.get("PRISM_SKIP_TEX")
BIB = ("@article{knuth84, author = {Donald E. Knuth}, title = {Literate Programming},\n"
       "  journal = {The Computer Journal}, year = {1984}, volume = {27}, pages = {97--111}}\n")


def project(files: dict, name: str = "p") -> Path:
    root = Path(tempfile.mkdtemp()) / name
    root.mkdir(parents=True)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root.resolve()


def has_package(name: str) -> bool:
    exe = shutil.which("kpsewhich")
    if not exe:
        return False
    r = subprocess.run([exe, name], capture_output=True, text=True)
    return bool(r.stdout.strip())


class Engine(unittest.TestCase):
    def detect(self, tex, configured=None, extra=None):
        root = project({"main.tex": tex, **(extra or {})})
        return build.detect_engine(root, "main.tex", configured)

    def test_plain_documents_use_pdflatex(self):
        self.assertEqual(self.detect("\\documentclass{amsart}\n\\usepackage{amsmath}\n"),
                         ("pdflatex", "default"))

    def test_magic_comment(self):
        self.assertEqual(self.detect("% !TEX program = xelatex\n\\documentclass{article}")[0],
                         "xelatex")
        self.assertEqual(self.detect("%!TeX TS-program = LuaLaTeX\n\\documentclass{article}")[0],
                         "lualatex")

    def test_packages_that_need_another_engine(self):
        self.assertEqual(self.detect("\\documentclass{article}\\usepackage[no-math]{fontspec}"),
                         ("xelatex", "fontspec"))
        self.assertEqual(self.detect("\\documentclass[UTF8]{ctexart}")[0], "xelatex")
        self.assertEqual(self.detect("\\documentclass{article}\\usepackage{amsmath,xeCJK}")[0],
                         "xelatex")
        self.assertEqual(self.detect("\\documentclass{article}\\usepackage{luatexja}")[0],
                         "lualatex")
        self.assertEqual(self.detect("\\documentclass{article}\\usepackage{fontspec}"
                                     "\\usepackage{luacode}")[0], "lualatex")

    def test_commented_out_and_body_packages_do_not_count(self):
        self.assertEqual(self.detect("\\documentclass{article}\n% \\usepackage{fontspec}\n"
                                     "\\begin{document}\\usepackage{fontspec}")[0], "pdflatex")

    def test_the_projects_own_class_is_read(self):
        self.assertEqual(self.detect("\\documentclass{mine}", extra={
            "mine.cls": "\\LoadClass{article}\\RequirePackage{unicode-math}"})[0], "xelatex")

    def test_prism_json_wins(self):
        self.assertEqual(self.detect("\\documentclass{article}\\usepackage{fontspec}", "lualatex"),
                         ("lualatex", "prism.json"))


class OutputFolder(unittest.TestCase):
    def test_clean_removes_only_what_builds_leave(self):
        root = project({"main.tex": "x", "refs.bib": "x", "mystyle.ist": "x",
                        "build/main.aux": "", "build/main.toc": "", "build/main.log": "",
                        "build/main.pdf": "%PDF", "build/main.synctex.gz": "",
                        "build/chap/c.aux": "", "build/main.prism-build.json": "{}",
                        "build/main.ist": "", "build/notes.txt": "keep"})
        build.clean(root / "build", "main", False)
        left = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        self.assertEqual(left, ["build/main.pdf", "build/main.synctex.gz", "build/notes.txt",
                                "main.tex", "mystyle.ist", "refs.bib"])

    def test_feedback_ignores_logs_and_the_pdf(self):
        root = project({"build/main.aux": "a", "build/main.log": "l", "build/main.pdf": "p"})
        state = build.feedback_state(root / "build", False)
        self.assertEqual(list(state), ["main.aux"])
        (root / "build/main.log").write_text("changed", encoding="utf-8")
        self.assertEqual(build.feedback_state(root / "build", False), state)

    def test_broken_aux_is_recognised(self):
        root = project({"build/main.aux": "", "sections/intro.tex": ""})
        out = root / "build"
        truncated = ("(./build/main.aux)\nRunaway argument?\n{{1}{1} \n"
                     "main.tex:1: File ended while scanning use of \\@newl@bel.\n")
        self.assertTrue(build.broken_feedback(truncated, out, root))
        bad_toc = "(./main.tex (./build/main.toc\n./build/main.toc:3: Undefined control sequence.\n"
        self.assertTrue(build.broken_feedback(bad_toc, out, root))
        own_error = ("(./main.tex (./sections/intro.tex\n"
                     "./sections/intro.tex:3: Undefined control sequence.\n")
        self.assertFalse(build.broken_feedback(own_error, out, root))

    def test_mirror_dirs_for_included_files(self):
        root = project({"main.tex": "", "chapters/one.tex": "", "figures/a.png": ""})
        build.mirror_dirs(root, root / "build", "build")
        self.assertTrue((root / "build" / "chapters").is_dir())
        self.assertFalse((root / "build" / "figures").exists())


class Parsing(unittest.TestCase):
    def test_file_line_errors(self):
        root = project({"main.tex": "", "my chapter.tex": "", "sub/a.tex": ""})
        out = ("./main.tex:3: Undefined control sequence.\n"
               "./my chapter.tex:7: Missing $ inserted.\n"
               "./sub/a.tex:1: LaTeX Error: File `x.sty' not found.\n"
               "D:/MiKTeX/tex/latex/foo/foo.sty:12: Something broke.\n"
               "l.3 \\foo\n")
        d = build.parse_stdout(out, root)
        self.assertEqual([(x["file"], x["line"]) for x in d],
                         [("main.tex", 3), ("my chapter.tex", 7), ("sub/a.tex", 1), (None, 12)])

    def test_log_warnings_are_attributed_to_source_files(self):
        root = project({"main.tex": "", "sections/intro.tex": "", "build/main.log": (
            "(./main.tex (./sections/intro.tex\n"
            "LaTeX Warning: Reference `sec:x' on page 1 undefined on input line 7.\n"
            ") (./build/main.bbl\nLaTeX Warning: Citation `k' on page 2 undefined on input line 3.\n"
            ")\nLaTeX Warning: There were undefined references.\n"
            "LaTeX Warning: Label(s) may have changed. Rerun to get cross-references right.\n)")})
        d = build.parse_log_warnings(root / "build/main.log", root)
        self.assertEqual([(x["file"], x["line"], x["message"][:9]) for x in d],
                         [("sections/intro.tex", 7, "Reference"), (None, 3, "Citation "),
                          (None, None, "There wer")])

    def test_bibtex_messages(self):
        root = project({"refs.bib": ""})
        out = ("I was expecting a `,' or a `}'---line 5 of file ../refs.bib\n"
               "Warning--empty journal in knuth84\n"
               "I couldn't open style file mystyle.bst\n")
        d = build.parse_bibtex(out, root, root / "build")
        self.assertEqual([(x["severity"], x["file"], x["line"]) for x in d],
                         [("error", "refs.bib", 5), ("warning", None, None), ("error", None, None)])

    def test_biber_messages(self):
        d = build.parse_biber("INFO - ok\nWARN - Duplicate entry key 'a'\nERROR - Cannot find 'x.bib'!\n")
        self.assertEqual([x["severity"] for x in d], ["warning", "error"])

    def test_aux_citations_follow_included_files(self):
        root = project({"build/main.aux": "\\citation{a}\n\\@input{chapters/one.aux}\n"
                                          "\\bibdata{refs}\n\\bibstyle{plain}\n",
                        "build/chapters/one.aux": "\\citation{b,c}\n"})
        cites, data, style = build._read_aux(root / "build", "main.aux", set())
        self.assertEqual((cites, data, style), ({"a", "b", "c"}, ["refs"], ["plain"]))


class Platform(unittest.TestCase):
    def test_shell_commands_never_run_in_wsl(self):
        if os.name != "nt":
            self.assertEqual(build.shell_argv("echo hi")[:2], ["bash", "-c"])
            return
        try:
            argv = build.shell_argv("echo hi")
        except build.BuildError as e:
            self.assertIn("Git for Windows", str(e))
            return
        self.assertNotIn("system32", argv[0].lower())
        self.assertNotIn("windowsapps", argv[0].lower())

    def test_missing_engine_is_explained(self):
        root = project({"main.tex": "\\documentclass{article}\\begin{document}x\\end{document}"})
        with mock.patch("build.shutil.which", return_value=None):
            r = build.Build(root, "main.tex", "build").run()
        self.assertEqual(r["exit"], 127)
        self.assertIn("pdflatex was not found", r["output"])
        self.assertEqual(r["diagnostics"][0]["severity"], "error")

    @unittest.skipUnless(os.name == "nt", "Windows code pages")
    def test_ascii_alias_for_names_the_code_page_cannot_spell(self):
        root = project({"main.tex": "x"}, name="论文 草稿")
        if build.ansi_safe(root):
            self.skipTest("this code page can spell Chinese")
        alias = build.ascii_alias(root)
        self.addCleanup(os.rmdir, alias)       # the junction only, never its target
        self.assertTrue(build.ansi_safe(alias))
        self.assertEqual((alias / "main.tex").read_text(encoding="utf-8"), "x")
        self.assertEqual(build.ascii_alias(root), alias, "the junction is reused")


@unittest.skipUnless(HAVE_TEX, "needs a TeX distribution (pdflatex on PATH)")
class RealBuilds(unittest.TestCase):
    """Builds with the TeX distribution on PATH, as a user's project would be built."""

    def run_build(self, files, name="p", root=None, **kw):
        root = root or project(files, name)
        alias = build.alias_path(root)
        if not build.ansi_safe(root) and alias:  # remove the junction biber may get (only the link)
            self.addCleanup(lambda: os.path.lexists(alias) and os.rmdir(alias))
        b = build.Build(root, "main.tex", "build", kw.pop("mode", "draft"), **kw)
        r = b.run()
        log = root / "build" / "main.log"
        return r, root, log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""

    def assertReadable(self, r, log):
        """Built, with every citation and reference resolved and nothing left to rerun."""
        self.assertEqual(r["exit"], 0, r["output"][-3000:])
        self.assertNotRegex(log, r"Citation .* undefined")
        self.assertNotRegex(log, r"Reference .* undefined")
        self.assertNotIn("Rerun to get", log)
        self.assertNotIn("Please rerun", log)

    def test_bibtex_and_cross_references(self):
        r, root, log = self.run_build({"main.tex": r"""\documentclass{article}
\begin{document}
\section{One}\label{sec:one}
See Section~\ref{sec:one} and \cite{knuth84}.
\bibliographystyle{plain}\bibliography{refs}
\end{document}
""", "refs.bib": BIB})
        self.assertReadable(r, log)
        self.assertIn("bibtex main", r["steps"])
        self.assertIn("Knuth", (root / "build/main.bbl").read_text(encoding="utf-8"))
        self.assertTrue((root / "build/main.synctex.gz").exists())

        # The next build of an unchanged document is one pass, without bibtex.
        r = build.Build(root, "main.tex", "build").run()
        self.assertEqual((r["exit"], r["passes"], r["steps"]), (0, 1, ["pdflatex (pass 1)"]))

        # A new citation is resolved by one build.
        main = root / "main.tex"
        (root / "refs.bib").write_text(BIB + "@book{lamport94, author={Leslie Lamport}, "
                                             "title={LaTeX}, publisher={AW}, year={1994}}\n",
                                       encoding="utf-8")
        main.write_text(main.read_text(encoding="utf-8").replace("\\cite{knuth84}",
                                                                 "\\cite{knuth84,lamport94}"),
                        encoding="utf-8")
        r, _, log = self.run_build(None, root=root)
        self.assertReadable(r, log)

    @unittest.skipUnless(shutil.which("biber"), "needs biber")
    def test_biblatex_with_biber(self):
        r, root, log = self.run_build({"main.tex": r"""\documentclass{article}
\usepackage[backend=biber]{biblatex}
\addbibresource{refs.bib}
\begin{document}
Text \cite{knuth84}.
\printbibliography
\end{document}
""", "refs.bib": BIB})
        self.assertReadable(r, log)
        self.assertIn("biber main", r["steps"])

    def test_the_projects_own_bst_and_included_subfolders(self):
        bst = subprocess.run(["kpsewhich", "plain.bst"], capture_output=True, text=True).stdout.strip()
        if not bst:
            self.skipTest("plain.bst not found")
        files = {"main.tex": r"""\documentclass{article}
\begin{document}
\include{chapters/one}
\bibliographystyle{mystyle}\bibliography{refs}
\end{document}
""", "chapters/one.tex": "\\section{One}\\label{one}\nSee \\ref{one} and \\cite{knuth84}.\n",
                 "refs.bib": BIB, "mystyle.bst": Path(bst).read_text(encoding="latin-1")}
        r, root, log = self.run_build(files)
        self.assertReadable(r, log)

    @unittest.skipUnless(shutil.which("xelatex"), "needs xelatex")
    def test_fontspec_documents_are_built_with_xelatex(self):
        r, root, log = self.run_build({"main.tex": "\\documentclass{article}\\usepackage{fontspec}"
                                                   "\\begin{document}Grüße\\end{document}\n"})
        self.assertEqual((r["exit"], r["engine"]), (0, "xelatex"), r["output"][-2000:])
        self.assertTrue((root / "build/main.pdf").exists())

    @unittest.skipUnless(shutil.which("xelatex") and has_package("ctex.sty"), "needs xelatex and ctex")
    def test_ctex_in_a_chinese_folder(self):
        """Fails on a TeX distribution whose ctex is newer than its LaTeX kernel ("Support
        package `expl3' too old"): update the distribution."""
        r, root, log = self.run_build({"main.tex": "\\documentclass{ctexart}\n\\begin{document}\n"
                                                   "\\section{引言}\\label{s}\n见第~\\ref{s}~节。\n"
                                                   "\\end{document}\n"}, name="中文 路径")
        self.assertReadable(r, log)
        self.assertEqual(r["engine"], "xelatex")

    def test_bibtex_in_a_chinese_folder(self):
        r, root, log = self.run_build({"main.tex": "\\documentclass{article}\\begin{document}\n"
                                                   "\\section{A}\\label{a} See \\ref{a} and "
                                                   "\\cite{knuth84}.\n\\bibliographystyle{plain}"
                                                   "\\bibliography{refs}\\end{document}\n",
                                       "refs.bib": BIB}, name="中文 路径")
        self.assertReadable(r, log)
        self.assertTrue((root / "build/main.synctex.gz").exists())

    @unittest.skipUnless(shutil.which("biber"), "needs biber")
    def test_biber_in_a_chinese_folder(self):
        r, root, log = self.run_build({"main.tex": "\\documentclass{article}\\usepackage[backend=biber]"
                                                   "{biblatex}\\addbibresource{refs.bib}\\begin{document}"
                                                   "\\cite{knuth84}\\printbibliography\\end{document}\n",
                                       "refs.bib": BIB}, name="论文 草稿")
        self.assertReadable(r, log)

    def test_index_glossary_and_nomenclature(self):
        if not has_package("glossaries.sty") or not has_package("nomencl.sty"):
            self.skipTest("needs glossaries and nomencl")
        r, root, log = self.run_build({"main.tex": r"""\documentclass{article}
\usepackage{makeidx}\makeindex
\usepackage{glossaries}\makeglossaries
\usepackage{nomencl}\makenomenclature
\newglossaryentry{tex}{name=TeX,description={a typesetting system}}
\begin{document}
Word\index{word}. \gls{tex}. $c$\nomenclature{$c$}{speed of light}
\printindex
\printglossaries
\printnomenclature
\end{document}
"""})
        self.assertReadable(r, log)
        for f in ("main.ind", "main.gls", "main.nls"):
            self.assertTrue((root / "build" / f).exists(), f)

    def test_a_broken_aux_from_an_earlier_build_is_repaired(self):
        r, root, log = self.run_build({"main.tex": "\\documentclass{article}\\begin{document}"
                                                   "\\section{A}\\label{a} See \\ref{a}.\\end{document}\n",
                                       "build/main.aux": "\\relax\n\\newlabel{a}{{1}{1}\n"})
        self.assertReadable(r, log)
        self.assertIn("left by an earlier build", r["output"])

    def test_errors_draft_goes_on_and_strict_stops(self):
        files = {"main.tex": "\\documentclass{article}\\begin{document}\nHello \\nosuchmacro.\n"
                             "\\end{document}\n"}
        r, root, _ = self.run_build(files)
        self.assertNotEqual(r["exit"], 0)
        self.assertTrue((root / "build/main.pdf").exists(), "draft still makes a PDF")
        err = next(d for d in r["diagnostics"] if d["severity"] == "error")
        self.assertEqual((err["file"], err["line"]), ("main.tex", 2))
        r, _, _ = self.run_build(files, mode="strict")
        self.assertNotEqual(r["exit"], 0)
        self.assertEqual(r["passes"], 1)

    def test_stop_and_time_limit(self):
        loop = {"main.tex": "\\documentclass{article}\\begin{document}\\def\\x{\\x}\\x\\end{document}\n"}
        root = project(loop)
        b = build.Build(root, "main.tex", "build")
        threading.Timer(1.5, b.stop).start()
        t0 = time.monotonic()
        r = b.run()
        self.assertLess(time.monotonic() - t0, 20)
        self.assertTrue(r["cancelled"])
        self.assertIn("stopped", r["output"])
        self.assertEqual(r["diagnostics"], [], "a half-written log means nothing")
        r = build.Build(project(loop), "main.tex", "build", timeout=2).run()
        self.assertTrue(r["timed_out"])
        self.assertFalse(r["cancelled"])
        self.assertIn("the last lines of main.log", r["output"])


if __name__ == "__main__":
    unittest.main()
