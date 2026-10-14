"""The project's files around an agent turn (snapshot.py)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import snapshot  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir()
        (self.root / "a.tex").write_bytes(b"one\r\ntwo\r\n")
        (self.root / "fig.png").write_bytes(b"\x89PNG\x00")
        (self.root / ".hidden").write_text("h", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "HEAD").write_text("ref", encoding="utf-8")

    def test_every_file_but_git(self):
        files, issues = snapshot.take(self.root)
        self.assertEqual(sorted(files), [".hidden", "a.tex", "fig.png"])
        self.assertEqual(issues, set())

    def test_what_changed(self):
        before, _ = snapshot.take(self.root)
        (self.root / "a.tex").write_bytes(b"one\r\nTWO\r\n")
        (self.root / "fig.png").unlink()
        (self.root / "new.tex").write_text("new\n", encoding="utf-8")
        after, _ = snapshot.take(self.root)
        got = {c["path"]: c for c in snapshot.changes(before, after)}
        self.assertEqual(sorted(got), ["a.tex", "fig.png", "new.tex"])
        self.assertIn("+TWO", got["a.tex"]["diff"])
        self.assertTrue(got["fig.png"]["deleted"] and got["new.tex"]["created"])
        self.assertIn("Binary", got["fig.png"]["diff"])

    def test_restore_puts_back_the_exact_bytes_but_never_over_a_later_edit(self):
        before, _ = snapshot.take(self.root)
        (self.root / "a.tex").write_bytes(b"agent\n")
        (self.root / "new.tex").write_text("agent's\n", encoding="utf-8")
        after, _ = snapshot.take(self.root)
        (self.root / "new.tex").write_text("then yours\n", encoding="utf-8")
        restored = snapshot.restore(self.root, before, after, snapshot.changed_paths(before, after))
        self.assertEqual(restored, ["a.tex"])
        self.assertEqual((self.root / "a.tex").read_bytes(), b"one\r\ntwo\r\n")
        self.assertEqual((self.root / "new.tex").read_text(encoding="utf-8"), "then yours\n")

    def test_a_file_too_big_to_keep_is_seen_but_not_undone(self):
        with mock.patch.object(snapshot, "MAX_FILE", 4):
            before, _ = snapshot.take(self.root)
            (self.root / "a.tex").write_bytes(b"changed and long\n")
            after, _ = snapshot.take(self.root)
            [change] = snapshot.changes(before, after)
            self.assertFalse(change["undoable"])
            self.assertEqual(snapshot.restore(self.root, before, after, ["a.tex"]), [])

    def test_what_someone_else_wrote_is_no_change(self):
        """rebase: a save, and a build's output folder (also what is gone from it), become
        part of `before`."""
        (self.root / "build").mkdir()
        (self.root / "build" / "old.log").write_text("log", encoding="utf-8")
        before, _ = snapshot.take(self.root)
        (self.root / "a.tex").write_bytes(b"yours\n")
        (self.root / "build" / "old.log").unlink()
        (self.root / "build" / "main.pdf").write_bytes(b"%PDF")
        written = {"a.tex": snapshot.state_of(self.root / "a.tex"),
                   "build/main.pdf": snapshot.state_of(self.root / "build" / "main.pdf")}
        snapshot.rebase(before, written, {"build/"})
        after, _ = snapshot.take(self.root)
        self.assertEqual(snapshot.changes(before, after), [])


if __name__ == "__main__":
    unittest.main()
