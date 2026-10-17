"""Tests for the editor server's own logic (prism_local/server.py): saving and project
files, git state, symbols."""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import server  # noqa: E402
import project as projectmod  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/: tmpdirs, test_lifecycle
from tmpdirs import tmpdir  # noqa: E402
from test_lifecycle import HTTP, request  # noqa: E402


def project(files: dict) -> Path:
    root = tmpdir()
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    server.set_root(root)
    return server.PROJECT.root


class Imports(unittest.TestCase):
    def test_project_modules_are_all_loaded_at_the_start(self):
        """A server runs for days, and a self-update fast-forwards the code under it: a
        module of the project imported only at its first use would be the new version, next
        to the old versions of those it uses. Every one server.py imports inside a function
        is loaded by the time it has started."""
        import re
        prism = Path(server.__file__).parent
        mods = {p.stem for p in prism.glob("*.py")}
        late = set()
        for line in Path(server.__file__).read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s+(?:import (\w+)|from (\w+) import)", line)
            if m and (m.group(1) or m.group(2)) in mods:
                late.add(m.group(1) or m.group(2))
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import server; "
                "print(' '.join(sorted(sys.modules)))")
        loaded = set(subprocess.run([sys.executable, "-c", code, str(prism)], capture_output=True,
                                    text=True, check=True).stdout.split())
        self.assertEqual(late - loaded, set(), "imported only at their first use")
        self.assertTrue({"publish", "github", "settings"}.issubset(loaded))
        self.assertNotIn("hub", loaded, "the editor imports publishing without Home's server state")


class Config(unittest.TestCase):
    """The editor's use of the project's settings (projectconfig.py; tested there)."""

    def test_the_home_page_names_the_same_document_and_folder(self):
        """The Home page's card (title, thumbnail) and the editor read prism.json and guess
        the document alike: a \\documentclass commented out is none, an outdir outside the
        project is not used."""
        import hub
        root = project({"a.tex": "% \\documentclass{article}", "b.tex": "\\documentclass{article}",
                        "prism.json": '{"outdir": "..\\\\elsewhere"}'})
        pdf, main, outdir = hub.pdf_path(root)
        self.assertEqual((main, outdir), (server.PROJECT.config.main, server.PROJECT.config.outdir))
        self.assertEqual((main, outdir), ("b.tex", "build"))
        self.assertEqual(pdf, root / "build" / "b.pdf")

    def test_a_started_server_shows_the_pdf_built_last(self):
        doc = "\\documentclass{article}"
        root = project({"main.tex": doc, "notes.tex": doc, "sections/intro.tex": "chapter",
                        "build/main.pdf": "%PDF", "build/notes.pdf": "%PDF", "build/intro.pdf": "%PDF"})
        os.utime(root / "build/main.pdf", (1000, 1000))
        os.utime(root / "build/notes.pdf", (2000, 2000))
        server.PROJECT.show_last_built()
        self.assertEqual((server.PROJECT.config.main, server.PROJECT.config.pdf.name), ("notes.tex", "notes.pdf"))
        self.assertEqual(server.PROJECT.config.project_main, "main.tex", "prism.json's main is kept")
        os.utime(root / "build/main.pdf", (3000, 3000))
        os.utime(root / "build/intro.pdf", (4000, 4000))
        server.set_root(root)
        server.PROJECT.show_last_built()
        self.assertEqual(server.PROJECT.config.main, "main.tex", "a .tex that is no document is skipped")
        root = project({"main.tex": doc, "notes.tex": doc})
        server.PROJECT.show_last_built()
        self.assertEqual(server.PROJECT.config.main, "main.tex", "nothing built yet: the project's main")

    def test_changes_apply_without_a_restart(self):
        root = project({"main.tex": "", "prism.json": '{"engine": "pdflatex"}'})
        self.assertEqual(server.PROJECT.config.engine, "pdflatex")
        time.sleep(0.02)
        (root / "prism.json").write_text('{"engine": "lualatex"}', encoding="utf-8")
        self.assertEqual(server.PROJECT.config.engine, "lualatex")


DOC = "\\documentclass{article}\\begin{document}\\input{sec}\\end{document}"


class ShownDocument(unittest.TestCase):
    """Which document is shown (its PDF in the pane) and which a build builds."""

    def setUp(self):
        self.root = project({"main.tex": DOC, "sec.tex": "chapter", "notes.tex": "\\documentclass{article}"
                             "\\begin{document}\\input{part}\\end{document}", "part.tex": "notes' part",
                             "loose.tex": "in no document", "prism.json": '{"main": "main.tex"}'})

    def build(self, active, during=lambda cfg: None):
        """A build with a stand-in for the TeX run; during(cfg) runs while it builds."""
        class Fake:
            def __init__(self, root, main, outdir, mode, **kw):
                self.cfg_main = main

            def run(fake):
                during(projectmod.Config(self.root, main=fake.cfg_main))
                (self.root / "build").mkdir(exist_ok=True)
                (self.root / "build" / (Path(fake.cfg_main).stem + ".pdf")).write_bytes(b"%PDF new")
                return {"exit": 0, "diagnostics": [], "output": ""}
        with mock.patch.object(server.PROJECT, "builder", Fake):
            return server.PROJECT.build("draft", active=active)

    def test_a_chapter_builds_the_document_that_includes_it(self):
        self.assertEqual(self.build("sec.tex")["main"], "main.tex")
        self.assertEqual(self.build("part.tex")["main"], "notes.tex", "not prism.json's main")
        self.assertEqual(self.build("notes.tex")["main"], "notes.tex")
        self.assertEqual(self.build("loose.tex")["main"], "main.tex", "included nowhere: the main")

    def test_the_shown_document_changes_when_its_build_ends(self):
        (self.root / "build").mkdir()
        (self.root / "build" / "main.pdf").write_bytes(b"%PDF old")
        os.utime(self.root / "build" / "main.pdf", (1000, 1000))
        seen = {}

        def during(cfg):
            seen["shown"] = server.PROJECT.config.main
            cfg.pdf.write_bytes(b"%PDF half")                 # the engine's first run
            seen["pdf"] = server.PROJECT.pdf_state(cfg)
        r = self.build("notes.tex", during)
        self.assertEqual(seen["shown"], "main.tex", "while notes.tex builds, main.tex's PDF stays")
        self.assertEqual(server.PROJECT.config.main, "notes.tex")
        self.assertIsNotNone(r["pdf_mtime"])
        seen.clear()
        self.build("sec.tex", during)                     # rebuilding main.pdf, which was shown before
        self.assertEqual(seen["pdf"], 1000.0, "reported as it was until the build ends")
        self.assertNotEqual(server.PROJECT.pdf_state(server.PROJECT.config), 1000.0)

    def test_a_changed_prism_json_keeps_a_standalone_document_shown(self):
        self.build("notes.tex")
        time.sleep(0.02)
        (self.root / "prism.json").write_text('{"main": "main.tex", "engine": "lualatex"}', encoding="utf-8")
        self.assertEqual((server.PROJECT.config.main, server.PROJECT.config.engine), ("notes.tex", "lualatex"))
        self.build("main.tex")
        time.sleep(0.02)
        (self.root / "prism.json").write_text('{"main": "notes.tex"}', encoding="utf-8")
        self.assertEqual(server.PROJECT.config.main, "notes.tex", "showing the main: it follows prism.json")


class Files(unittest.TestCase):
    def test_tree_lists_assets_without_reading_them_as_source(self):
        project({"main.tex": "\\section{Introduction}", "figure.png": "",
                 "reference.pdf": "", ".env": "secret", "build/output.pdf": ""})
        self.assertEqual(set(server.PROJECT.list_files(editable_only=False)),
                         {"main.tex", "figure.png", "reference.pdf"})
        self.assertEqual(server.PROJECT.list_files(), ["main.tex"])
        self.assertEqual(server.PROJECT.symbols()["outline"][0]["title"], "Introduction")
        for bad in (".env", "build/output.pdf", "../outside.png", ".git/config"):
            with self.assertRaises(ValueError, msg=bad):
                server.PROJECT.resolve(bad, editable_only=False)

    def test_only_editable_files_inside_the_project(self):
        root = project({"main.tex": "", "fig.tikz": "", "build/main.log": "", "build/x.tex": "",
                        ".git/config": "", "notes/.hidden/x.tex": "", "img.png": ""})
        self.assertEqual(server.PROJECT.resolve("fig.tikz"), root / "fig.tikz")
        for bad in ("../main.tex", "/etc/passwd", "sub\\main.tex", "build/main.log", "build/x.tex",
                    ".git/config", "notes/.hidden/x.tex", "img.png", ""):
            with self.assertRaises(ValueError, msg=bad):
                server.PROJECT.resolve(bad)

    def test_one_walk_lists_what_list_files_lists(self):
        root = project({"main.tex": "", "a/b.tex": "", "a/fig.png": "", "build/x.tex": ""})
        self.assertEqual(server.PROJECT.list_both(), (server.PROJECT.list_files(editable_only=False), server.PROJECT.list_files()))
        self.assertEqual(server.PROJECT.list_both(), (["main.tex", "a/b.tex", "a/fig.png"], ["main.tex", "a/b.tex"]))
        if os.name == "nt":
            # A junction is no link to os.walk: what it leads to outside the project is
            # never listed (its files are resolved one by one), what it leads to inside is.
            import _winapi
            outside = tmpdir()
            (outside / "secret.tex").write_text("secret", encoding="utf-8")
            _winapi.CreateJunction(str(outside), str(root / "out"))
            _winapi.CreateJunction(str(root / "a"), str(root / "in"))
            self.assertEqual(server.PROJECT.list_both()[1], ["main.tex", "a/b.tex", "in/b.tex"])

    def test_no_save_while_the_agent_edits(self):
        """An Edit turn runs: the server holds every save, also from a page that did not see
        the turn start (another tab, a reload): its diff and Undo would take your text for
        the agent's. The page tries again; once the turn is over the save goes through."""
        root = project({"main.tex": "one\n"})
        with mock.patch.object(server.AGENT, "editing", return_value=True):
            r, code = server.PROJECT.save("main.tex", "yours\n", None, True)
        self.assertEqual((code, r.get("busy")), (503, True), r)
        self.assertEqual((root / "main.tex").read_text(encoding="utf-8"), "one\n")
        r, code = server.PROJECT.save("main.tex", "yours\n", None, False)
        self.assertEqual(code, 200, r)

    def test_save_keeps_the_files_line_ends(self):
        root = project({})
        (root / "crlf.tex").write_bytes(b"a\r\nb\r\n")
        (root / "lf.tex").write_bytes(b"a\nb\n")
        for name in ("crlf.tex", "lf.tex"):
            r, code = server.PROJECT.save(name, "a\nb\nc\n", projectmod.mtime(root / name), False)
            self.assertEqual(code, 200, r)
        self.assertEqual((root / "crlf.tex").read_bytes(), b"a\r\nb\r\nc\r\n")
        self.assertEqual((root / "lf.tex").read_bytes(), b"a\nb\nc\n")
        server.PROJECT.save("new.tex", "x\ny\n", None, False)
        self.assertEqual((root / "new.tex").read_bytes(), b"x\ny\n")

    @unittest.skipIf(os.name == "nt", "file modes: Linux and macOS")
    def test_save_keeps_the_files_permissions(self):
        root = project({"main.tex": "old\n"})
        (root / "main.tex").chmod(0o640)
        server.PROJECT.save("main.tex", "new\n", projectmod.mtime(root / "main.tex"), False)
        self.assertEqual((root / "main.tex").stat().st_mode & 0o777, 0o640)

    def test_save_refuses_to_overwrite_a_newer_file(self):
        root = project({"main.tex": "old\n"})
        loaded = projectmod.mtime(root / "main.tex")
        time.sleep(0.02)
        (root / "main.tex").write_text("changed on disk\n", encoding="utf-8")
        r, code = server.PROJECT.save("main.tex", "mine\n", loaded, False)
        self.assertEqual((code, r["conflict"]), (409, True))
        self.assertEqual((root / "main.tex").read_text(encoding="utf-8"), "changed on disk\n")
        r, code = server.PROJECT.save("main.tex", "mine\n", loaded, True)
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
        r, code = server.PROJECT.save("main.tex", "new\n", projectmod.mtime(root / "main.tex"), False)
        self.assertEqual(code, 200)
        self.assertEqual((root / "main.tex").read_text(encoding="utf-8"), "new\n")


@unittest.skipUnless(shutil.which("git"), "needs git")
class FileMenu(unittest.TestCase):
    """The Files sidebar's menu: new, rename, copy and paste, delete to the Recycle Bin."""

    def files(self, action, **body):
        return server.Handler._files(None, "/api/files/" + action, body)

    def test_the_tree_shows_empty_folders_but_not_hidden_or_excluded_ones(self):
        root = project({"main.tex": "", "a/b.tex": "", "build/x.log": ""})
        for d in ("empty", "a/sub", ".hidden", "node_modules"):
            (root / d).mkdir()
        walked = []
        server.PROJECT.list_both(walked)
        self.assertEqual(sorted(walked), ["a", "a/sub", "empty"])

    def test_new_file_and_folder_with_names_that_work_everywhere(self):
        root = project({"main.tex": ""})
        self.assertEqual(self.files("new", folder="", name="figs", kind="folder"), {"path": "figs", "kind": "folder"})
        self.assertEqual(self.files("new", folder="figs", name="a.tex")["path"], "figs/a.tex")
        self.assertTrue((root / "figs/a.tex").is_file())
        for bad in ("a.tex", "x/y.tex", "con.tex", ".env", "end.", " lead", 'q"uote'):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                self.files("new", folder="figs", name=bad)

    def test_rename_a_file_a_folder_and_only_the_case(self):
        root = project({"main.tex": "", "a/b.tex": "", "a/c.tex": ""})
        self.assertEqual(self.files("rename", path="a/b.tex", name="d.tex"), {"path": "a/d.tex"})
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.files("rename", path="a/d.tex", name="c.tex")
        self.assertEqual(self.files("rename", path="a", name="chapters"), {"path": "chapters"})
        self.assertEqual(self.files("rename", path="main.tex", name="Main.tex"), {"path": "Main.tex"})
        self.assertIn("Main.tex", os.listdir(root))
        with self.assertRaises(ValueError):
            self.files("rename", path="", name="x")

    def test_paste_copies_beside_what_is_there_without_repository_metadata(self):
        root = project({"main.tex": "body", "figs/a.png": "png", "figs/.git/config": "repo"})
        r = self.files("paste", folder="", paths=["main.tex", "figs"])
        self.assertEqual(r, {"pasted": ["main-copy.tex", "figs-copy"], "skipped": []})
        self.assertEqual(self.files("paste", folder="", paths=["main.tex"])["pasted"], ["main-copy-2.tex"])
        self.assertEqual((root / "figs-copy/a.png").read_text(), "png")
        self.assertFalse((root / "figs-copy/.git").exists())
        r = self.files("paste", folder="figs", paths=["figs"])
        self.assertEqual((r["pasted"], len(r["skipped"])), ([], 1))

    def test_paste_takes_what_explorer_or_finder_copied(self):
        root = project({"main.tex": ""})
        outside = tmpdir()
        (outside / "fig.pdf").write_text("pdf")
        with mock.patch.object(server.desktop, "clipboard_files", return_value=[outside / "fig.pdf"]):
            self.assertEqual(self.files("paste", folder="")["pasted"], ["fig.pdf"])
        self.assertEqual((root / "fig.pdf").read_text(), "pdf")
        with mock.patch.object(server.desktop, "clipboard_files", return_value=[]), \
                self.assertRaisesRegex(ValueError, "holds no files"):
            self.files("paste", folder="")

    def test_copy_falls_back_to_the_page_where_the_system_has_no_file_clipboard(self):
        project({"main.tex": ""})
        with mock.patch.object(server.desktop, "copy_files") as copy:
            self.assertEqual(self.files("copy", paths=["main.tex"]), {"system": True})
        self.assertEqual(copy.call_args.args[0], [server.PROJECT.root / "main.tex"])
        with mock.patch.object(server.desktop, "copy_files", side_effect=server.desktop.Unsupported("no")):
            self.assertEqual(self.files("copy", paths=["main.tex"])["system"], False)

    def test_delete_goes_to_the_recycle_bin_and_never_takes_the_project(self):
        project({"main.tex": "", "a/b.tex": ""})
        with mock.patch.object(server.desktop, "trash") as trash:
            self.assertEqual(self.files("trash", paths=["a"]), {"trashed": ["a"]})
            trash.assert_called_once_with([server.PROJECT.root / "a"])
            with self.assertRaises(ValueError):
                self.files("trash", paths=[""])

    def test_only_what_the_sidebar_shows_and_never_during_an_agent_turn(self):
        root = project({"main.tex": "", "build/x.tex": ""})
        (root / ".git").mkdir()
        for rel in (".git", "build/x.tex", "../x", "missing.tex"):
            with self.subTest(rel=rel), self.assertRaises(ValueError):
                self.files("rename", path=rel, name="y.tex")
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(root / "build"), str(root / "link"))
            with self.assertRaises(ValueError):
                self.files("rename", path="link/x.tex", name="y.tex")
        with mock.patch.object(server.AGENT, "busy", return_value=True), \
                self.assertRaisesRegex(projectmod.FilesBusy, "agent"):
            self.files("new", folder="", name="new.tex")
        self.assertFalse((root / "new.tex").exists())


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

    def test_the_tree_asks_git_only_when_something_changed(self):
        """Every open page asks for the tree every two seconds: with no file and no ref
        changed since, git's status is not asked again."""
        import proc
        root = project({"main.tex": "a\n"})
        self.git(root, "init", "-q")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "init")
        listed = lambda: [("main.tex", server.mtime(root / "main.tex"))]     # noqa: E731
        self.assertEqual(server.git_status(listed()), {})
        with mock.patch.object(proc, "run", wraps=proc.run) as run:
            self.assertEqual(server.git_status(listed()), {})
        self.assertEqual(run.call_count, 0)
        (root / "main.tex").write_text("b\n", encoding="utf-8")
        os.utime(root / "main.tex", ns=(1, 1))                    # a new mtime, also on a coarse clock
        self.assertEqual(server.git_status(listed()), {"main.tex": "M"}, "an edit is seen")
        self.git(root, "commit", "-qam", "second")
        self.assertEqual(server.git_status(listed()), {}, "a commit is seen")

    def test_diff_panel_says_what_it_shows(self):
        root = tmpdir()
        (root / "main.tex").write_text("a\n", encoding="utf-8")
        server.set_root(root)
        self.assertEqual(server.project_diff("."), {"repo": False, "diff": ""})
        self.git(root, "init", "-q")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-qm", "first")
        (root / "main.tex").write_text("b\n", encoding="utf-8")
        self.git(root, "commit", "-qam", "Autosave: main.tex")
        # Everything committed (as with sync on): the last commit's changes, not an empty panel.
        d = server.project_diff(".")
        self.assertEqual((d["diff"], d["untracked"], d["last"]["message"]), ("", [], "Autosave: main.tex"))
        self.assertIn("+b", d["last"]["diff"])
        (root / "main.tex").write_text("c\n", encoding="utf-8")
        self.git(root, "add", "main.tex")                   # staged counts as uncommitted too
        (root / "new.tex").write_text("n\n", encoding="utf-8")
        d = server.project_diff(".")
        self.assertIn("+c", d["diff"])
        self.assertEqual((d["untracked"], d["last"]), (["new.tex"], None))


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
        s = server.PROJECT.symbols()
        self.assertEqual(s["env_titles"], {"thm": "Theorem", "lem": "Lemma", "rem*": "Remark",
                                           "assump": "Assumption", "claim": "Claim"})
        self.assertEqual([(o["kind"], o["title"]) for o in s["outline"]],
                         [("section", "Intro"), ("lem", ""), ("thm", "Main")])
        self.assertEqual({l["label"]: l["kind"] for l in s["labels"]},
                         {"sec:intro": "section", "lem:a": "lem"})

    def test_macros_for_the_agents_replies(self):
        project({"macros.sty": "\\newcommand{\\R}{\\mathbb{R}}\n\\DeclareMathOperator*{\\argmax}{arg\\,max}\n",
                 "main.tex": r"""\documentclass{article}
\usepackage{macros}
\newcommand{\norm}[1]{\left\| #1 \right\|}
\newcommand\abs[1]{%
  \lvert #1 \rvert}
\renewcommand{\R}{\mathbf{R}}
\def\E{\mathbb{E}}
\def\pair#1#2{(#1, #2)}
\DeclareMathOperator{\Tr}{Tr}
\DeclarePairedDelimiter{\ip}{\langle}{\rangle}
\newcommand{\opt}[2][1]{#1 + #2}
% \newcommand{\gone}{x}
\newcommand{\broken}{\x
\begin{document}\end{document}
"""})
        self.assertEqual(server.PROJECT.symbols()["katex_macros"], {
            "\\R": "\\mathbf{R}",                         # the .tex file's \renewcommand wins
            "\\argmax": "\\operatorname*{arg\\,max}",
            "\\norm": "\\left\\| #1 \\right\\|",
            "\\abs": "\\lvert #1 \\rvert",
            "\\E": "\\mathbb{E}",
            "\\pair": "(#1, #2)",
            "\\Tr": "\\operatorname{Tr}",
            "\\ip": "\\left\\langle #1 \\right\\rangle",
        })

    def test_standard_names_without_newtheorem(self):
        project({"main.tex": "\\documentclass{article}\\begin{document}\\end{document}\n"})
        self.assertEqual(server.PROJECT.symbols()["env_titles"]["lemma"], "Lemma")

    def test_section_titles(self):
        project({"main.tex": "\\begin{document}\n\\chapter{One}\n"
                             "\\section[Short]{Long $\\frac{a}{b^{2}}$}\n"
                             "\\subsection*{\\texorpdfstring{$\\mathbb{R}^d$}{Rd} and $\\SO(n)$}\n"
                             "\\end{document}\n"})
        self.assertEqual([(o["kind"], o["title"]) for o in server.PROJECT.symbols()["outline"]],
                         [("chapter", "One"), ("section", "Long a/b^2"),
                          ("subsection", "Rd and SO(n)")])

    def test_unchanged_files_are_not_read_again(self):
        root = project({"main.tex": "\\newtheorem{thm}{Theorem}\n\\input{a}\n",
                        "a.tex": "\\section{A}\\label{a}\n", "refs.bib": "@book{knuth,\n}\n"})
        first = server.PROJECT.symbols()
        self.assertEqual(server.PROJECT.document_order(), ["main.tex", "a.tex"])
        reads = []
        real = Path.read_text

        def counted(p, *a, **k):
            reads.append(p.name)
            return real(p, *a, **k)
        with mock.patch.object(Path, "read_text", counted):
            self.assertEqual(server.PROJECT.symbols(), first)
            self.assertEqual(server.PROJECT.document_order(), ["main.tex", "a.tex"])
            self.assertEqual(reads, [], "nothing changed: nothing is read")
            (root / "a.tex").write_text("\\section{B}\\label{b}\\input{c}\n", encoding="utf-8")
            (root / "c.tex").write_text("\\begin{thm}[C]\\end{thm}\n", encoding="utf-8")
            os.utime(root / "a.tex", ns=(1, 1))      # a new modification time, also on a coarse clock
            s = server.PROJECT.symbols()
            self.assertEqual(sorted(reads), ["a.tex", "c.tex"], "only the changed and the new file")
        self.assertEqual([l["label"] for l in s["labels"]], ["b"])
        self.assertEqual([(o["kind"], o["title"]) for o in s["outline"]], [("section", "B"), ("thm", "C")])
        self.assertEqual(server.PROJECT.document_order(), ["main.tex", "a.tex", "c.tex"])
        self.assertEqual(s["bibkeys"], [{"key": "knuth", "type": "book", "file": "refs.bib", "line": 1}])

    def test_the_order_is_worked_out_again_only_when_its_files_or_the_list_changed(self):
        """The tree asks for the order every two seconds: with the files listed and their
        mtimes the same, no name is looked for again (each one a resolve, slow on Windows)."""
        root = project({"main.tex": "\\input{a}\n\\input{later}\n", "a.tex": "x\n", "b.tex": "y\n"})
        P = server.PROJECT

        def order():
            listed = P.list_files(editable_only=False)
            return P.document_order(P.list_files(), {f: server.mtime(root / f) for f in listed})
        self.assertEqual(order(), ["main.tex", "a.tex", "b.tex"])
        with mock.patch.object(P, "resolve_tex_name", wraps=P.resolve_tex_name) as looked:
            self.assertEqual(order(), ["main.tex", "a.tex", "b.tex"])
            self.assertEqual(looked.call_count, 0, "nothing changed")
            (root / "b.tex").write_text("z\n", encoding="utf-8")
            os.utime(root / "b.tex", ns=(1, 1))
            self.assertEqual(order(), ["main.tex", "a.tex", "b.tex"])
            self.assertEqual(looked.call_count, 0, "a file outside the chain changed")
        (root / "a.tex").write_text("\\input{b}\n", encoding="utf-8")
        os.utime(root / "a.tex", ns=(2, 2))
        self.assertEqual(order(), ["main.tex", "a.tex", "b.tex"], "a file in it changed")
        (root / "b.tex").write_text("", encoding="utf-8")
        (root / "a.tex").write_text("x\n", encoding="utf-8")
        os.utime(root / "a.tex", ns=(3, 3))
        (root / "later.tex").write_text("", encoding="utf-8")
        self.assertEqual(order(), ["main.tex", "a.tex", "later.tex", "b.tex"], "a file it names appeared")
        self.assertEqual(P.document_order(), ["main.tex", "a.tex", "later.tex", "b.tex"], "without stamps: worked out")


def copy_example(name="my paper") -> Path:
    """A copy of examples/minimal to work in."""
    root = tmpdir() / name
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "minimal", root)
    return root


class Routes(unittest.TestCase):
    """The editor's HTTP routes, served in this process."""

    def setUp(self):
        import threading
        self.srv = server.httpbase.Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def ask(self, path, body=None, headers=None):
        import json
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.url + path, None if body is None else json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "X-Prism-Local": "1", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_every_editor_request_is_bound_to_its_project(self):
        root = project({"main.tex": "original\n"})
        key = server.registry.project_key(server.PROJECT.root)
        self.assertEqual(self.ask("/api/tree")[1]["project_key"], key)
        for path, body in (("/api/tree", None), ("/api/file?path=main.tex", None),
                           ("/api/file", {"path": "main.tex", "content": "wrong project"}),
                           ("/api/config", {"main": "other.tex"})):
            with self.subTest(path=path):
                status, r = self.ask(path, body, {"X-Prism-Project": "another-project"})
                self.assertEqual(status, 409)
                self.assertTrue(r["other_project"])
        self.assertEqual((server.PROJECT.root / "main.tex").read_text(), "original\n")
        self.assertEqual(self.ask("/api/tree", headers={"X-Prism-Project": key})[0], 200)

    def test_a_pdf_or_asset_of_another_project_is_refused(self):
        """A pop-out PDF or a figure opened in a new tab names its project too (the query,
        where a page cannot add a header): another project on the port refuses them."""
        root = project({"main.tex": "x\n", "figure.png": "png"})
        key = server.registry.project_key(root)
        server.PROJECT.config.pdf.parent.mkdir(parents=True, exist_ok=True)
        server.PROJECT.config.pdf.write_bytes(b"%PDF-1.4 mine")
        # The pop-out PDF's poll says whose PDF it is (a reloaded pop-out learns it there).
        self.assertEqual(self.ask("/api/pdfstat")[1]["project_key"], key)
        for path in ("/pdf", "/api/asset?path=figure.png"):
            sep = "&" if "?" in path else "?"
            with self.subTest(path=path):
                status, r = self.ask(f"{path}{sep}project_key=another-project")
                self.assertEqual(status, 409)
                self.assertTrue(r["other_project"])
                self.assertEqual(self.ask(path, headers={"X-Prism-Project": "another-project"})[0], 409)
                with HTTP.open(f"{self.url}{path}{sep}project_key={key}", timeout=5) as response:
                    self.assertIn(response.read(), (b"%PDF-1.4 mine", b"png"))

    def test_compile_active_document_and_follow_its_pdf(self):
        root = copy_example()
        # A deterministic custom builder checks routing independently of installed TeX.
        script = ("import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                  "out=pathlib.Path(sys.argv[2]); out.mkdir(exist_ok=True); "
                  "sys.exit(1) if p.stem=='broken' else "
                  "(out/(p.stem+'.pdf')).write_bytes(p.name.encode())")
        config = json.dumps({"main": "main.tex", "build": {
            "draft": [sys.executable, "-c", script, "{main}", "{outdir}"]}})
        (root / "prism.json").write_text(config, encoding="utf-8")
        (root / "中文稿.tex").write_text(r"\documentclass{ctexart}", encoding="utf-8")
        (root / "chapter.tex").write_text("% \\documentclass{article}\n\\section{Chapter}", encoding="utf-8")
        (root / "broken.tex").write_text(r"\documentclass{article}", encoding="utf-8")
        server.set_root(root)
        url = self.url
        headers = {"Content-Type": "application/json", "X-Prism-Local": "1"}

        def compile(active=None):
            return request(url, "/api/build", json.dumps({"mode": "draft", "active": active}).encode(), headers)

        def pdf():
            with HTTP.open(url + "/pdf", timeout=5) as response:
                return response.read()

        for active, main in ((None, "main.tex"), ("中文稿.tex", "中文稿.tex"),
                             ("chapter.tex", "main.tex"), ("refs.bib", "main.tex")):
            status, result = compile(active)
            self.assertEqual(status, 200, result)
            self.assertEqual((result["exit"], result["main"]), (0, main))
            self.assertEqual(pdf(), main.encode())
            self.assertEqual(request(url, "/api/pdfstat")[1]["main"], main)
            self.assertEqual(request(url, "/api/tree")[1]["pdf_main"], main)
            self.assertEqual(request(url, "/api/config")[1]["main"], "main.tex")
            self.assertEqual(request(url, "/pdf?main=another.tex")[0], 409)
        # No active file (the agent's compile tool): the document shown now, not main.tex.
        compile("中文稿.tex")
        status, result = compile(None)
        self.assertEqual((status, result["main"]), (200, "中文稿.tex"))
        # ... unless another document was edited after its PDF (the agent changed it).
        later = time.time() + 5
        os.utime(root / "main.tex", (later, later))
        status, result = compile(None)
        self.assertEqual((status, result["main"]), (200, "main.tex"))
        status, result = compile("broken.tex")
        self.assertEqual((status, result["exit"], result["main"], result["pdf_mtime"]),
                         (200, 1, "broken.tex", None))
        self.assertEqual(request(url, "/api/pdfstat")[1], {"main": "broken.tex", "mtime": None,
                                                           "project_key": server.registry.project_key(root)})
        self.assertEqual(request(url, "/pdf")[0], 404)
        for bad in ("../outside.tex", "/tmp/outside.tex", "build/main.tex", 123):
            self.assertEqual(compile(bad)[0], 400, bad)
        self.assertEqual(compile("missing.tex")[0], 404)
        self.assertEqual((root / "prism.json").read_text(encoding="utf-8"), config)


    def test_import_files_through_http_and_open_assets(self):
        import base64
        root = copy_example()
        original = (root / "main.tex").read_bytes()
        server.set_root(root)
        url = self.url
        headers = {"Content-Type": "application/json", "X-Prism-Local": "1"}

        def upload(name, data, destination="project"):
            return request(url, "/api/upload", json.dumps({"name": name,
                           "data": base64.b64encode(data).decode(), "destination": destination}).encode(), headers)

        code, imported = upload("main.tex", b"new\r\n")
        self.assertEqual((code, imported["path"]), (200, "main-2.tex"))
        self.assertEqual((root / "main.tex").read_bytes(), original)
        self.assertEqual((root / "main-2.tex").read_bytes(), b"new\r\n")
        self.assertNotIn("sync", imported, "project imports use normal autosave sync")
        for name, data, ctype in (("figure.png", b"\x00\xffPNG", "image/png"),
                                  ("reference.pdf", b"%PDF-1.4", "application/pdf"),
                                  ("page.html", b"<script>window.test=true</script>", "application/octet-stream")):
            self.assertEqual(upload(name, data)[0], 200)
            with HTTP.open(url + "/api/asset?path=" + name, timeout=5) as response:
                self.assertEqual(response.read(), data)
                self.assertEqual(response.headers["Content-Type"], ctype)
                if name.endswith(".html"):
                    self.assertTrue(response.headers["Content-Disposition"].startswith("attachment;"))
        files = {f["path"]: f for f in request(url, "/api/tree")[1]["files"]}
        self.assertTrue(files["main-2.tex"]["editable"])
        self.assertFalse(files["figure.png"]["editable"])
        self.assertFalse(files["reference.pdf"]["editable"])
        self.assertEqual(request(url, "/api/file?path=figure.png")[0], 400)
        for path in ("../outside.png", ".git/config", "build/main.pdf"):
            self.assertEqual(request(url, "/api/asset?path=" + path)[0], 400)
        self.assertEqual(request(url, "/api/asset?path=figure.png",
                                 headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(upload("bad.txt", b"bad", destination="outside")[0], 400)
        self.assertFalse((root / "bad.txt").exists())
        self.assertEqual(request(url, "/api/upload", b'{"name":"bad.txt","data":"!"}', headers)[0], 400)
        self.assertEqual(upload("chat.txt", b"attached", destination="attachment")[1]["path"],
                         "prism-uploads/chat.txt")

    def test_a_hidden_page_asks_for_a_turns_events_without_the_server_waiting(self):
        """wait=0 (a hidden page): the server answers at once, so the request holds none of
        the few connections a browser keeps to it; without it, up to 20 s (at most 20)."""
        root = project({"main.tex": "x\n"})
        key = server.registry.project_key(root)
        waits = []
        with mock.patch.object(server.AGENT, "events", lambda job, after, wait: waits.append(wait) or ([], False)):
            for q in ("&wait=0", "", "&wait=600", "&wait=-1"):
                code, r = self.ask(f"/api/agent/events?job=1&after=0&project_key={key}{q}")
                self.assertEqual((code, r), (200, {"events": [], "done": False}))
            self.assertEqual(self.ask(f"/api/agent/events?job=1&wait=soon&project_key={key}")[0], 400)
        self.assertEqual(waits, [0.0, 20.0, 20.0, 0.0])

    def test_synctex_says_what_to_do_until_there_is_data(self):
        import gzip
        root = project({"main.tex": "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n"})
        self.assertEqual(self.ask("/api/synctex/forward?file=main.tex&line=3"),
                         (404, {"error": "no synctex data; build first"}))
        sp = 1 / server.synctex.SP_TO_BP
        (root / "build").mkdir()
        server.PROJECT.config.synctex.write_bytes(gzip.compress(
            f"Input:1:main.tex\nUnit:1\nMagnification:1000\n{{1\nx1,3:{round(100 * sp)},{round(200 * sp)}\n}}1\n".encode()))
        code, box = self.ask("/api/synctex/forward?file=main.tex&line=3")
        self.assertEqual((code, box["page"], box["line"]), (200, 1, 3))
        self.assertEqual(self.ask("/api/synctex/inverse?page=1&x=101&y=199"),
                         (200, {"file": "main.tex", "line": 3}))


class Uploads(unittest.TestCase):
    """Files added to an agent message are saved under prism-uploads/."""

    def test_an_upload_waits_for_another_window_using_the_repository(self):
        """Another prism-local window is changing the repository: the upload is not "failed"
        but kept here, and the next autosave commits it."""
        from repository import RepoBusy
        project({"main.tex": ""})
        with mock.patch.object(server.GITSYNC, "record_file", side_effect=RepoBusy("busy")), \
                mock.patch.object(server.GITSYNC, "touched") as touched:
            server.sync_upload("prism-uploads/a.pdf")
        self.assertEqual(server.UPLOAD_SYNC["prism-uploads/a.pdf"]["state"], "local")
        touched.assert_called_once()

    def test_names_are_made_safe(self):
        cases = {"notes.pdf": "notes.pdf", "../../etc/passwd": "passwd",
                 "a<b>:c?.tex": "a_b__c_.tex", "CON.txt": "CON_.txt", "  .hidden": "hidden",
                 "C:\\Users\\x\\scan 1.png": "scan 1.png", "": "file"}
        for given, want in cases.items():
            self.assertEqual(projectmod.upload_name(given), want, given)
        self.assertLessEqual(len(projectmod.upload_name("x" * 300 + ".pdf")), 104)

    def test_same_file_once_other_file_numbered(self):
        root = project({"main.tex": "x"})
        self.assertEqual(server.PROJECT.upload("n.pdf", b"one"), "prism-uploads/n.pdf")
        self.assertEqual(server.PROJECT.upload("n.pdf", b"one"), "prism-uploads/n.pdf")
        self.assertEqual(server.PROJECT.upload("n.pdf", b"two"), "prism-uploads/n-2.pdf")
        self.assertEqual((root / "prism-uploads" / "n-2.pdf").read_bytes(), b"two")
        with self.assertRaises(ValueError):
            server.PROJECT.upload("big.bin", b"0" * (projectmod.MAX_UPLOAD + 1))

    def test_project_import_preserves_existing_files_and_bytes(self):
        root = project({"main.tex": "original", "folder.txt/keep.txt": "keep"})
        self.assertEqual(server.PROJECT.upload("main.tex", b"original", folder=""), "main.tex")
        self.assertEqual(server.PROJECT.upload("main.tex", b"new\r\n", folder=""), "main-2.tex")
        self.assertEqual((root / "main.tex").read_bytes(), b"original")
        self.assertEqual((root / "main-2.tex").read_bytes(), b"new\r\n")
        self.assertEqual(server.PROJECT.upload("folder.txt", b"file", folder=""), "folder-2.txt")
        self.assertEqual(server.PROJECT.upload("../figure.png", b"\x00\xffPNG", folder=""), "figure.png")
        self.assertEqual((root / "figure.png").read_bytes(), b"\x00\xffPNG")

    @unittest.skipIf(os.name == "nt", "needs symlinks")
    def test_upload_cannot_follow_a_link_outside_the_project(self):
        root = project({})
        outside = tmpdir()
        (outside / "image.png").write_bytes(b"outside")
        (root / "prism-uploads").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            server.PROJECT.upload("new.png", b"new")
        (root / "image.png").symlink_to(outside / "image.png")
        with self.assertRaises(ValueError):
            server.PROJECT.upload("image.png", b"new", folder="")
        self.assertEqual((outside / "image.png").read_bytes(), b"outside")
        self.assertFalse((outside / "new.png").exists())

    def test_concurrent_imports_do_not_overwrite_each_other(self):
        from concurrent.futures import ThreadPoolExecutor
        root = project({})
        contents = [bytes([n]) for n in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(lambda data: server.PROJECT.upload("figure.png", data, folder=""), contents))
        self.assertEqual(len(set(paths)), len(contents))
        self.assertEqual([(root / p).read_bytes() for p in paths], contents)


@unittest.skipUnless(shutil.which("git"), "needs git")
class UploadsToGitHub(unittest.TestCase):
    """An upload is committed alone in the project's own repository and pushed."""

    def git(self, cwd, *args):
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def repo(self, root):
        self.git(root, "init", "-q", "-b", "main")
        self.git(root, "config", "user.email", "t@example.com")
        self.git(root, "config", "user.name", "T")
        self.git(root, "config", "commit.gpgsign", "false")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-q", "-m", "init")

    def test_commit_only_the_upload_and_push(self):
        root = project({"main.tex": "one\n"})
        self.repo(root)
        bare = tmpdir()
        self.git(bare, "init", "-q", "--bare")
        self.git(root, "remote", "add", "origin", str(bare))
        server.GITSYNC.bind_target()
        (root / "main.tex").write_text("two\n", encoding="utf-8")      # your edit, not committed
        rel = server.PROJECT.upload("notes.pdf", b"%PDF-1.4")
        server.sync_upload(rel)
        self.assertEqual(server.UPLOAD_SYNC[rel]["state"], "synced", server.UPLOAD_SYNC[rel])
        self.assertEqual(self.git(root, "show", "--name-only", "--format=", "HEAD"), "prism-uploads/notes.pdf")
        self.assertIn("main.tex", self.git(root, "status", "--porcelain"))
        self.assertEqual(self.git(bare, "rev-parse", "main"), self.git(root, "rev-parse", "HEAD"))

    def test_no_remote_commits_locally(self):
        root = project({"main.tex": "x\n"})
        self.repo(root)
        rel = server.PROJECT.upload("a.txt", b"a")
        server.sync_upload(rel)
        self.assertEqual(server.UPLOAD_SYNC[rel]["state"], "local")
        self.assertIn("no remote", server.UPLOAD_SYNC[rel]["message"])
        self.assertEqual(self.git(root, "show", "--name-only", "--format=", "HEAD"), "prism-uploads/a.txt")

    def test_never_in_a_surrounding_repository(self):
        outer = tmpdir()
        (outer / "paper").mkdir()
        (outer / "paper" / "main.tex").write_text("x\n", encoding="utf-8")
        self.repo(outer)
        server.set_root(outer / "paper")
        rel = server.PROJECT.upload("a.txt", b"a")
        server.sync_upload(rel)
        self.assertEqual(server.UPLOAD_SYNC[rel]["state"], "local")
        self.assertEqual(self.git(outer, "rev-list", "--count", "HEAD"), "1", "nothing committed outside")

    def test_not_a_repository(self):
        project({"main.tex": "x\n"})
        rel = server.PROJECT.upload("a.txt", b"a")
        server.sync_upload(rel)
        self.assertEqual(server.UPLOAD_SYNC[rel]["state"], "local")


@unittest.skipUnless(shutil.which("git"), "needs git")
class UploadsTakenBack(unittest.TestCase):
    """A chip removed from the message box takes its upload back (/api/upload/cancel): the
    file goes, unless a message sent it, it was there before, or it changed since."""

    setUp = Routes.setUp
    ask = Routes.ask
    git = UploadsToGitHub.git
    repo = UploadsToGitHub.repo

    def upload(self, upload_id, name, data):
        import base64
        return self.ask("/api/upload", {"name": name, "data": base64.b64encode(data).decode(),
                                        "destination": "attachment", "upload_id": upload_id})

    def synced(self, rel):
        for _ in range(200):
            if server.UPLOAD_SYNC.get(rel, {}).get("state") != "syncing":
                return server.UPLOAD_SYNC.get(rel)
            time.sleep(0.05)
        self.fail("the upload's commit did not end")

    def pushed_project(self):
        root = project({"main.tex": "x\n"})
        self.repo(root)
        bare = tmpdir()
        self.git(bare, "init", "-q", "--bare")
        self.git(root, "remote", "add", "origin", str(bare))
        server.GITSYNC.bind_target()
        return root, bare

    def test_a_committed_upload_taken_back_is_deleted_by_the_next_commit(self):
        root, bare = self.pushed_project()
        code, r = self.upload("page-1", "notes.txt", b"notes")
        self.assertEqual((code, r["path"]), (200, "prism-uploads/notes.txt"))
        self.assertEqual(self.synced(r["path"])["state"], "synced")
        self.assertEqual(self.ask("/api/upload/cancel", {"upload_id": "page-1"}),
                         (200, {"path": "prism-uploads/notes.txt", "deleted": True}))
        self.assertFalse((root / "prism-uploads" / "notes.txt").exists())
        server.GITSYNC.commit_all("Autosave")       # the deletion, as any other change
        self.assertEqual(self.git(root, "show", "--name-status", "--format=", "HEAD"),
                         "D\tprism-uploads/notes.txt")
        self.assertEqual(self.git(root, "rev-list", "--count", "HEAD"), "3", "history kept as it was")

    def test_an_upload_taken_back_before_its_commit_is_never_committed(self):
        root, bare = self.pushed_project()
        with mock.patch.object(server, "sync_upload") as later:
            code, r = self.upload("page-2", "draft.txt", b"draft")
        self.assertEqual(self.ask("/api/upload/cancel", {"upload_id": "page-2"})[1]["deleted"], True)
        server.sync_upload(*later.call_args.args)
        self.assertFalse((root / "prism-uploads" / "draft.txt").exists())
        self.assertEqual(self.git(root, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(self.git(bare, "branch", "--list"), "", "nothing pushed")

    def test_an_upload_taken_back_before_it_arrives_is_not_written(self):
        root = project({"main.tex": "x\n"})
        self.assertEqual(self.ask("/api/upload/cancel", {"upload_id": "page-3"}),
                         (200, {"path": None, "deleted": False}))
        self.assertEqual(self.upload("page-3", "late.txt", b"late")[0], 409)
        self.assertFalse((root / "prism-uploads" / "late.txt").exists())

    def test_a_file_a_message_sent_is_never_deleted(self):
        root = project({"main.tex": "x\n"})
        key = server.registry.project_key(root)
        with mock.patch.object(server, "sync_upload"):
            rel = self.upload("page-4", "sent.txt", b"sent")[1]["path"]
            with mock.patch.object(server.AGENT, "start", return_value={"job": 1}):
                self.assertEqual(self.ask("/api/agent", {"prompt": "read it", "project_key": key,
                                                         "attachments": [rel]})[0], 200)
            self.assertEqual(self.ask("/api/upload/cancel", {"upload_id": "page-4"})[0], 409)
            self.assertTrue((root / rel).exists())
            # The same file added again (the sent one, reused) and taken back: it stays too.
            self.assertEqual(self.upload("page-5", "sent.txt", b"sent")[1]["path"], rel)
            self.assertEqual(self.ask("/api/upload/cancel", {"upload_id": "page-5"})[0], 409)
            self.assertTrue((root / rel).exists())

    def test_a_message_cannot_send_a_file_taken_back(self):
        root = project({"main.tex": "x\n"})
        key = server.registry.project_key(root)
        with mock.patch.object(server, "sync_upload"):
            rel = self.upload("page-6", "gone.txt", b"gone")[1]["path"]
            self.ask("/api/upload/cancel", {"upload_id": "page-6"})
            with mock.patch.object(server.AGENT, "start", return_value={"job": 1}) as start:
                self.assertEqual(self.ask("/api/agent", {"prompt": "read it", "project_key": key,
                                                         "attachments": [rel]})[0], 409)
            start.assert_not_called()

    def test_a_file_there_before_or_changed_since_stays(self):
        root = project({"main.tex": "x\n", "prism-uploads/old.txt": "old"})
        with mock.patch.object(server, "sync_upload"):
            self.assertEqual(self.upload("page-7", "old.txt", b"old")[1]["path"], "prism-uploads/old.txt")
            self.assertEqual(self.ask("/api/upload/cancel", {"upload_id": "page-7"})[1]["deleted"], False)
            self.assertEqual((root / "prism-uploads" / "old.txt").read_text(), "old")
            rel = self.upload("page-8", "edited.txt", b"before")[1]["path"]
            (root / rel).write_text("edited since", encoding="utf-8")
            self.assertEqual(self.ask("/api/upload/cancel", {"upload_id": "page-8"})[1]["deleted"], False)
            self.assertEqual((root / rel).read_text(), "edited since")


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
