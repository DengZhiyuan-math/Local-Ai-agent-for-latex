"""Which files of a project are its sources (sources.py)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sources  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


class Sources(unittest.TestCase):
    def test_what_latex_writes_is_never_a_source(self):
        """Every auxiliary file the builder knows, glossaries and nomenclatures too; never an
        extension a source may have (a figure, a data file)."""
        for ext in (".aux", ".log", ".bbl", ".synctex.gz", ".run.xml", ".gls", ".glo", ".acn",
                    ".nls", ".nlo", ".loa", ".lol", ".brf", ".tdo", ".fdb_latexmk"):
            self.assertIn(ext, sources.AUX)
        for ext in (".pdf", ".png", ".jpg", ".eps", ".json", ".tex", ".bib", ".sty"):
            self.assertNotIn(ext, sources.AUX)

    def test_recordable(self):
        top = tmpdir()
        (top / "big.pdf").write_bytes(b"x" * 2048)
        (top / "fig.pdf").write_bytes(b"x")
        with mock.patch.object(sources, "MAX_FILE", 1024):
            yes = ["main.tex", "fig.pdf", "sub/gone.tex"]
            no = ["build/main.pdf", "main.gls", "main.tex.1.2.prism-tmp", "big.pdf", ".git/config"]
            for path in yes:
                self.assertTrue(sources.recordable(path, True, top, "build"), path)
            for path in no:
                self.assertFalse(sources.recordable(path, True, top, "build"), path)
        self.assertTrue(sources.recordable("main.bbl", False, top, "build"),
                        "one you committed yourself: its changes are recorded")
        self.assertFalse(sources.recordable("ex 1/out/x.pdf", True, top, "ex 1/out"), "a project's own output folder")

    def test_a_new_repository_ignores_what_is_never_a_source(self):
        own = sources.gitignore("out")
        self.assertEqual(own[0], "out/")
        self.assertIn("*.aux", own)
        self.assertIn("*.gls", own)
        self.assertIn("*.prism-tmp", own)
        self.assertNotIn(".git-prism-*/", own)
        self.assertIn(".git-prism-*/", sources.gitignore(shared=True))

    def test_git_status_read_once(self):
        out = "\0".join([" M main.tex", "?? 练习/ex 1/a b.tex", "R  new.tex", "old.tex", "D  gone.tex", ""])
        self.assertEqual(sources.parse_status(out), [
            (" M", "main.tex"), ("??", "练习/ex 1/a b.tex"), ("R ", "old.tex"), ("R ", "new.tex"),
            ("D ", "gone.tex")])


if __name__ == "__main__":
    unittest.main()
