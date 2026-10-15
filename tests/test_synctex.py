"""SyncTeX lookups (synctex.py): source to PDF and back, without the editor's server."""
import gzip
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import synctex  # noqa: E402
from projectconfig import Config  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import tmpdir  # noqa: E402

SP = 1 / synctex.SP_TO_BP          # scaled points in one PDF point


def write_synctex(path: Path, inputs: dict[int, str], records: list[str]) -> Path:
    """A .synctex.gz holding `inputs` (tag -> path) and `records` (lines as written)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(f"Input:{tag}:{name}\n" for tag, name in inputs.items())
    text += "Unit:1\nMagnification:1000\n" + "".join(line + "\n" for line in records)
    path.write_bytes(gzip.compress(text.encode()))
    return path


def x(tag: int, line: int, px: float, py: float) -> str:
    """A glyph run of input `tag`'s `line`, at (px, py) in PDF points."""
    return f"x{tag},{line}:{round(px * SP)},{round(py * SP)}"


class SourceAndBack(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pdflatex") and not os.environ.get("PRISM_SKIP_TEX"),
                         "needs a TeX distribution")
    def test_a_real_build_goes_from_source_to_pdf_and_back(self):
        import build
        root = tmpdir()
        shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "minimal", root,
                        dirs_exist_ok=True, ignore=shutil.ignore_patterns("build"))
        cfg = Config(root)
        r = build.Build(root, cfg.main, cfg.outdir).run()
        self.assertEqual(r["exit"], 0, r["output"][-2000:])
        records = synctex.SyncIndex().lookup(cfg.synctex, root.resolve())
        line = next(n for n, t in enumerate((root / "sections/intro.tex").read_text(
            encoding="utf-8").splitlines(), 1) if len(t) > 40 and not t.startswith(("\\", "%")))
        box = records.forward("sections/intro.tex", line)
        self.assertIsNotNone(box)
        back = records.inverse(box["page"], box["x"] + 20, box["y"] + box["h"] / 2, cfg.outdir)
        self.assertEqual(back["file"], "sections/intro.tex")
        self.assertLessEqual(abs(back["line"] - line), 1)

    def test_forward_and_inverse_on_a_written_file(self):
        root = tmpdir().resolve()
        path = write_synctex(root / "build/main.synctex.gz", {1: "main.tex"},
                             ["{1", x(1, 3, 100, 200), x(1, 4, 100, 214), "}1"])
        records = synctex.SyncIndex().lookup(path, root)
        self.assertEqual(records.forward("main.tex", 4)["page"], 1)
        self.assertEqual(records.inverse(1, 101, 213, "build"), {"file": "main.tex", "line": 4})


class GeneratedFiles(unittest.TestCase):
    """A click on text LaTeX read back from the build folder goes where it is edited."""

    def setUp(self):
        self.root = tmpdir().resolve()
        (self.root / "refs.bib").write_text("% refs\n@article {knuth84,\n  title={TeX}\n}\n", encoding="utf-8")
        (self.root / "build").mkdir()
        (self.root / "build/main.bbl").write_text(
            "\\begin{thebibliography}{1}\n\n\\bibitem{knuth84}\nD. Knuth.\n", encoding="utf-8")
        # (page, kind, file, line, x, y, w, h, d): a .bbl line, a .toc line, a main.tex line.
        self.records = synctex.Records(self.root, [(1, "x", "build/main.bbl", 4, 100.0, 100.0, 0, 0, 0),
                                                   (2, "x", "build/main.toc", 2, 100.0, 100.0, 0, 0, 0),
                                                   (2, "x", "main.tex", 3, 100.0, 300.0, 0, 0, 0)])

    def test_bibliography_goes_to_the_bib_entry(self):
        self.assertEqual(self.records.inverse(1, 100, 100, "build", lambda: ["refs.bib"]),
                         {"file": "refs.bib", "line": 2})

    def test_contents_go_to_the_nearest_source_line(self):
        self.assertEqual(self.records.inverse(2, 100, 100, "build"), {"file": "main.tex", "line": 3})


class Index(unittest.TestCase):
    def test_switching_documents_even_when_timestamps_match(self):
        root, index = tmpdir().resolve(), synctex.SyncIndex()
        for main in ("main.tex", "other.tex"):
            path = write_synctex(root / "build" / (Path(main).stem + ".synctex.gz"), {1: main},
                                 ["{1", x(1, 1, 100, 100), "}1"])
            os.utime(path, ns=(1_000_000_000, 1_000_000_000))
            self.assertIsNotNone(index.lookup(path, root).forward(main, 1))

    def test_a_changed_file_is_read_again_and_an_unchanged_one_is_not(self):
        root, index = tmpdir().resolve(), synctex.SyncIndex()
        path = write_synctex(root / "build/main.synctex.gz", {1: "main.tex"}, ["{1", x(1, 1, 100, 100), "}1"])
        first = index.lookup(path, root)
        self.assertIs(index.lookup(path, root), first)
        write_synctex(path, {1: "main.tex"}, ["{1", x(1, 1, 100, 100), x(1, 2, 100, 120), "}1"])
        self.assertIsNotNone(index.lookup(path, root).forward("main.tex", 2))

    def test_no_file_yet_says_build_first(self):
        root = tmpdir().resolve()
        with self.assertRaisesRegex(synctex.NoData, "build first"):
            synctex.SyncIndex().lookup(root / "build/main.synctex.gz", root)

    def test_damaged_data_says_build_again_and_is_read_once(self):
        """A click while the engine still writes the file, or after a crash: a truncated or
        damaged .synctex.gz is "build again", not a server error, and not read on each click."""
        root = tmpdir().resolve()
        path = root / "build/main.synctex.gz"
        path.parent.mkdir()
        whole = gzip.compress(("Input:1:main.tex\n{1\n" + "x1,3:100,200\n" * 5000).encode())
        for name, data in (("truncated", whole[:len(whole) // 2]), ("not gzip", b"garbage" * 100)):
            with self.subTest(name):
                path.write_bytes(data)
                index = synctex.SyncIndex()
                with self.assertRaisesRegex(synctex.NoData, "build again"):
                    index.lookup(path, root)
                with mock.patch.object(synctex.gzip, "open", side_effect=AssertionError("read again")), \
                        self.assertRaisesRegex(synctex.NoData, "build again"):
                    index.lookup(path, root)
        path.write_bytes(gzip.compress(b"Input:1:main.tex\n{oops\n{1\nx1,3:100,200\n"))
        records = synctex.SyncIndex().lookup(path, root)    # a damaged line is left out
        self.assertEqual([r[:4] for r in records.recs], [(1, "x", "main.tex", 3)])


if __name__ == "__main__":
    unittest.main()
