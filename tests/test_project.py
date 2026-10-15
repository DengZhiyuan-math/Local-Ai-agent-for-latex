"""The project an editor works on (project.py), without the editor's server."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from project import Project  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import tmpdir  # noqa: E402

DOC = "\\documentclass{article}\\begin{document}x\\end{document}"


def make(files: dict) -> Project:
    root = tmpdir()
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return Project(root)


class Settings(unittest.TestCase):
    def test_a_changed_prism_json_applies_whoever_asks(self):
        """Not only a request: the agent and the sync read the settings as they are now."""
        p = make({"main.tex": DOC, "prism.json": '{"outdir": "out"}'})
        self.assertEqual(p.config.outdir, "out")
        time.sleep(0.02)
        (p.root / "prism.json").write_text('{"outdir": "pdf"}', encoding="utf-8")
        self.assertEqual(p.config.outdir, "pdf")

    def test_the_document_shown_changes_in_one_place(self):
        p = make({"main.tex": DOC, "notes.tex": DOC})
        self.assertEqual(p.config.main, "main.tex")
        cfg = p.show("notes.tex")
        self.assertIs(p.config, cfg, "unchanged settings: the same object")
        time.sleep(0.02)
        (p.root / "prism.json").write_text('{"engine": "lualatex"}', encoding="utf-8")
        self.assertEqual((p.config.main, p.config.engine), ("notes.tex", "lualatex"),
                         "a standalone document stays shown")
        p.show("main.tex")
        time.sleep(0.02)
        (p.root / "prism.json").write_text('{"main": "notes.tex"}', encoding="utf-8")
        self.assertEqual(p.config.main, "notes.tex", "showing the main: it follows prism.json")


class Files(unittest.TestCase):
    def test_the_files_shown_and_those_it_may_open(self):
        p = make({"main.tex": DOC, "sec/a.tex": "", "fig.png": "", "build/x.tex": "", ".hidden.tex": ""})
        self.assertEqual(p.list_files(), ["main.tex", "sec/a.tex"])
        self.assertEqual(p.list_files(editable_only=False), ["main.tex", "sec/a.tex", "fig.png"])
        self.assertEqual(p.resolve("sec/a.tex"), p.root / "sec/a.tex")
        for bad in ("build/x.tex", ".hidden.tex", "fig.png", "../x.tex"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                p.resolve(bad)


if __name__ == "__main__":
    unittest.main()
