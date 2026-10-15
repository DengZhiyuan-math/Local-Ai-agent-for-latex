"""Tests for the editor server's own logic (prism_local/server.py): saving and project
files, git state, symbols."""
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
sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/: tmpdirs, test_lifecycle
from tmpdirs import tmpdir  # noqa: E402


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
        server.show_last_built()
        self.assertEqual((server.PROJECT.config.main, server.PROJECT.config.pdf.name), ("notes.tex", "notes.pdf"))
        self.assertEqual(server.PROJECT.config.project_main, "main.tex", "prism.json's main is kept")
        os.utime(root / "build/main.pdf", (3000, 3000))
        os.utime(root / "build/intro.pdf", (4000, 4000))
        server.set_root(root)
        server.show_last_built()
        self.assertEqual(server.PROJECT.config.main, "main.tex", "a .tex that is no document is skipped")
        root = project({"main.tex": doc, "notes.tex": doc})
        server.show_last_built()
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
        """run_build with a stand-in for the TeX run; during(cfg) runs while it builds."""
        class Fake:
            def __init__(self, root, main, outdir, mode, **kw):
                self.cfg_main = main

            def run(fake):
                during(server.Config(self.root, main=fake.cfg_main))
                (self.root / "build").mkdir(exist_ok=True)
                (self.root / "build" / (Path(fake.cfg_main).stem + ".pdf")).write_bytes(b"%PDF new")
                return {"exit": 0, "diagnostics": [], "output": ""}
        with mock.patch.object(server.build, "Build", Fake):
            return server.run_build("draft", active=active)

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
            seen["pdf"] = server.pdf_state(cfg)
        r = self.build("notes.tex", during)
        self.assertEqual(seen["shown"], "main.tex", "while notes.tex builds, main.tex's PDF stays")
        self.assertEqual(server.PROJECT.config.main, "notes.tex")
        self.assertIsNotNone(r["pdf_mtime"])
        seen.clear()
        self.build("sec.tex", during)                     # rebuilding main.pdf, which was shown before
        self.assertEqual(seen["pdf"], 1000.0, "reported as it was until the build ends")
        self.assertNotEqual(server.pdf_state(server.PROJECT.config), 1000.0)

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
        self.assertEqual(server.symbols()["outline"][0]["title"], "Introduction")
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
            r, code = server.save_file("main.tex", "yours\n", None, True)
        self.assertEqual((code, r.get("busy")), (503, True), r)
        self.assertEqual((root / "main.tex").read_text(encoding="utf-8"), "one\n")
        r, code = server.save_file("main.tex", "yours\n", None, False)
        self.assertEqual(code, 200, r)

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
                self.assertRaisesRegex(server.FilesBusy, "agent"):
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

    def test_unchanged_files_are_not_read_again(self):
        root = project({"main.tex": "\\newtheorem{thm}{Theorem}\n\\input{a}\n",
                        "a.tex": "\\section{A}\\label{a}\n", "refs.bib": "@book{knuth,\n}\n"})
        first = server.symbols()
        self.assertEqual(server.document_order(), ["main.tex", "a.tex"])
        reads = []
        real = Path.read_text

        def counted(p, *a, **k):
            reads.append(p.name)
            return real(p, *a, **k)
        with mock.patch.object(Path, "read_text", counted):
            self.assertEqual(server.symbols(), first)
            self.assertEqual(server.document_order(), ["main.tex", "a.tex"])
            self.assertEqual(reads, [], "nothing changed: nothing is read")
            (root / "a.tex").write_text("\\section{B}\\label{b}\\input{c}\n", encoding="utf-8")
            (root / "c.tex").write_text("\\begin{thm}[C]\\end{thm}\n", encoding="utf-8")
            os.utime(root / "a.tex", ns=(1, 1))      # a new modification time, also on a coarse clock
            s = server.symbols()
            self.assertEqual(sorted(reads), ["a.tex", "c.tex"], "only the changed and the new file")
        self.assertEqual([l["label"] for l in s["labels"]], ["b"])
        self.assertEqual([(o["kind"], o["title"]) for o in s["outline"]], [("section", "B"), ("thm", "C")])
        self.assertEqual(server.document_order(), ["main.tex", "a.tex", "c.tex"])
        self.assertEqual(s["bibkeys"], [{"key": "knuth", "type": "book", "file": "refs.bib", "line": 1}])


@unittest.skipUnless(shutil.which("pdflatex") and not os.environ.get("PRISM_SKIP_TEX"),
                     "needs a TeX distribution")
class Routes(unittest.TestCase):
    """The editor's HTTP routes, served in this process."""

    def setUp(self):
        import threading
        self.srv = server.httpbase.Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def ask(self, path, body=None):
        import json
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.url + path, None if body is None else json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "X-Prism-Local": "1"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

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
            self.assertEqual(server.upload_name(given), want, given)
        self.assertLessEqual(len(server.upload_name("x" * 300 + ".pdf")), 104)

    def test_same_file_once_other_file_numbered(self):
        root = project({"main.tex": "x"})
        self.assertEqual(server.save_upload("n.pdf", b"one"), "prism-uploads/n.pdf")
        self.assertEqual(server.save_upload("n.pdf", b"one"), "prism-uploads/n.pdf")
        self.assertEqual(server.save_upload("n.pdf", b"two"), "prism-uploads/n-2.pdf")
        self.assertEqual((root / "prism-uploads" / "n-2.pdf").read_bytes(), b"two")
        with self.assertRaises(ValueError):
            server.save_upload("big.bin", b"0" * (server.MAX_UPLOAD + 1))

    def test_project_import_preserves_existing_files_and_bytes(self):
        root = project({"main.tex": "original", "folder.txt/keep.txt": "keep"})
        self.assertEqual(server.save_upload("main.tex", b"original", folder=""), "main.tex")
        self.assertEqual(server.save_upload("main.tex", b"new\r\n", folder=""), "main-2.tex")
        self.assertEqual((root / "main.tex").read_bytes(), b"original")
        self.assertEqual((root / "main-2.tex").read_bytes(), b"new\r\n")
        self.assertEqual(server.save_upload("folder.txt", b"file", folder=""), "folder-2.txt")
        self.assertEqual(server.save_upload("../figure.png", b"\x00\xffPNG", folder=""), "figure.png")
        self.assertEqual((root / "figure.png").read_bytes(), b"\x00\xffPNG")

    @unittest.skipIf(os.name == "nt", "needs symlinks")
    def test_upload_cannot_follow_a_link_outside_the_project(self):
        root = project({})
        outside = tmpdir()
        (outside / "image.png").write_bytes(b"outside")
        (root / "prism-uploads").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            server.save_upload("new.png", b"new")
        (root / "image.png").symlink_to(outside / "image.png")
        with self.assertRaises(ValueError):
            server.save_upload("image.png", b"new", folder="")
        self.assertEqual((outside / "image.png").read_bytes(), b"outside")
        self.assertFalse((outside / "new.png").exists())

    def test_concurrent_imports_do_not_overwrite_each_other(self):
        from concurrent.futures import ThreadPoolExecutor
        root = project({})
        contents = [bytes([n]) for n in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(lambda data: server.save_upload("figure.png", data, folder=""), contents))
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
        rel = server.save_upload("notes.pdf", b"%PDF-1.4")
        server.sync_upload(rel)
        self.assertEqual(server.UPLOAD_SYNC[rel]["state"], "synced", server.UPLOAD_SYNC[rel])
        self.assertEqual(self.git(root, "show", "--name-only", "--format=", "HEAD"), "prism-uploads/notes.pdf")
        self.assertIn("main.tex", self.git(root, "status", "--porcelain"))
        self.assertEqual(self.git(bare, "rev-parse", "main"), self.git(root, "rev-parse", "HEAD"))

    def test_no_remote_commits_locally(self):
        root = project({"main.tex": "x\n"})
        self.repo(root)
        rel = server.save_upload("a.txt", b"a")
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
        rel = server.save_upload("a.txt", b"a")
        server.sync_upload(rel)
        self.assertEqual(server.UPLOAD_SYNC[rel]["state"], "local")
        self.assertEqual(self.git(outer, "rev-list", "--count", "HEAD"), "1", "nothing committed outside")

    def test_not_a_repository(self):
        project({"main.tex": "x\n"})
        rel = server.save_upload("a.txt", b"a")
        server.sync_upload(rel)
        self.assertEqual(server.UPLOAD_SYNC[rel]["state"], "local")


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
