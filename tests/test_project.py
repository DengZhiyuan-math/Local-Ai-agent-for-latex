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


class Writes(unittest.TestCase):
    def test_a_project_on_its_own_saves_and_tells_whoever_listens(self):
        p = make({"main.tex": "one\r\n"})
        changed = []
        p.on_change = lambda: changed.append(1)
        r, code = p.save("main.tex", "two\n", None, False)
        self.assertEqual((code, (p.root / "main.tex").read_bytes()), (200, b"two\r\n"))
        self.assertEqual(p.rename("main.tex", "paper.tex"), {"path": "paper.tex"})
        self.assertEqual(len(changed), 2)

    def test_nothing_changes_during_an_agent_turn_and_the_turn_is_told_otherwise(self):
        from unittest import mock
        import project
        turn = mock.Mock(**{"editing.return_value": True, "busy.return_value": True})
        p = make({"main.tex": "one\n"})
        p.turn = turn
        self.assertEqual(p.save("main.tex", "two\n", None, False)[1], 503)
        with self.assertRaises(project.FilesBusy):
            p.new("", "new.tex", "file")
        turn.editing.return_value = turn.busy.return_value = False
        p.new("", "new.tex", "file")
        turn.server_wrote.assert_called_with(["new.tex"])


class Building(unittest.TestCase):
    def test_one_build_at_a_time_and_busy_while_it_runs(self):
        import threading
        from unittest import mock
        started, release, stopped = threading.Event(), threading.Event(), []

        class Fake:
            def __init__(self, root, main, outdir, mode, **kw):
                self.root, self.main, self.outdir = root, main, outdir

            def run(self):
                started.set()
                release.wait(10)
                (self.root / self.outdir).mkdir(exist_ok=True)
                (self.root / self.outdir / "main.pdf").write_bytes(b"%PDF")
                return {"exit": 0, "diagnostics": [], "output": ""}

            def stop(self):
                stopped.append(1)
                release.set()

        p = make({"main.tex": DOC})
        p.builder, p.turn = Fake, mock.Mock(**{"busy.return_value": False})
        self.assertFalse(p.busy())
        result = {}
        t = threading.Thread(target=lambda: result.update(p.build("draft")))
        t.start()
        started.wait(5)
        self.assertTrue(p.busy())
        self.assertEqual(p.build("draft"), {"busy": True})
        self.assertTrue(p.stop_build())
        t.join(5)
        self.assertEqual((result["main"], stopped, p.busy()), ("main.tex", [1], False))
        p.turn.server_wrote.assert_called_with(["build"])    # the output is no turn's change


if __name__ == "__main__":
    unittest.main()
