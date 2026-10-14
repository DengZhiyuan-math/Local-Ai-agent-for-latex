"""A project's settings (projectconfig.py): the defaults, overridden by its prism.json."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from projectconfig import Config  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


def project(files: dict) -> Path:
    root = tmpdir()
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


class ProjectConfig(unittest.TestCase):
    def test_a_bad_prism_json_is_reported_and_the_defaults_apply(self):
        cfg = Config(project({"main.tex": "", "prism.json": '{"engine": "xetex2", "outdir": "../x", '
                                                            '"build": ["pdflatex", "{main}"]}'}))
        self.assertIsNone(cfg.engine)
        self.assertEqual(cfg.outdir, "build")
        for part in ("outdir", "unknown engine 'xetex2'", "build must map"):
            self.assertIn(part, cfg.error, "every problem is reported")
        cfg = Config(project({"main.tex": "", "prism.json": "{not json"}))
        self.assertIn("cannot be used", cfg.error)
        self.assertEqual((cfg.main, cfg.modes), ("main.tex", ["draft", "strict"]))

    def test_the_document_when_it_is_not_main_tex(self):
        cfg = Config(project({"proof.tex": "\\documentclass{article}", "notes.tex": "no class here"}))
        self.assertEqual(cfg.main, "proof.tex", "guessed from \\documentclass")
        cfg = Config(project({"a.tex": "\\documentclass{article}", "paper.tex": "\\documentclass{article}",
                              "prism.json": '{"main": "paper.tex"}'}))
        self.assertEqual(cfg.main, "paper.tex", "prism.json wins over the guess")
        cfg = Config(project({"main.tex": "\\documentclass{article}", "a.tex": "\\documentclass{article}"}))
        self.assertEqual(cfg.main, "main.tex")
        cfg = Config(project({"a.tex": "% \\documentclass{article}", "b.tex": "\\documentclass{article}"}))
        self.assertEqual(cfg.main, "b.tex", "one commented out is none")

    def test_no_document_yet(self):
        """A new folder without a .tex: the editor still has a document to open (main.tex),
        the Home page none to show (named_main)."""
        cfg = Config(project({"notes.txt": ""}))
        self.assertEqual((cfg.main, cfg.named_main), ("main.tex", None))

    def test_a_tex_that_cannot_be_read_is_passed_over(self):
        """A .tex at the top that cannot be read (here a folder so named; also a broken link,
        a cloud placeholder offline) must not keep the editor from starting."""
        root = project({"a.tex/keep": "", "b.tex": "\\documentclass{article}"})
        self.assertTrue((root / "a.tex").is_dir())
        self.assertEqual(Config(root).main, "b.tex")

    def test_the_output_folder_stays_inside_the_project(self):
        for outdir, expected in (("out", "out"), ("out\\\\pdf/", "out/pdf"), ("../x", "build"),
                                 ("C:/x", "build"), ("/x", "x"), ("", "build")):
            cfg = Config(project({"main.tex": "", "prism.json": f'{{"outdir": "{outdir}"}}'}))
            self.assertEqual(cfg.outdir, expected, outdir)
            self.assertEqual(cfg.pdf, cfg.root / expected / "main.pdf")

    def test_a_document_opened_on_its_own(self):
        cfg = Config(project({"main.tex": "", "notes.tex": ""}), main="notes.tex")
        self.assertEqual((cfg.main, cfg.project_main, cfg.pdf.name), ("notes.tex", "main.tex", "notes.pdf"))


if __name__ == "__main__":
    unittest.main()
