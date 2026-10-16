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


class TurnCost(unittest.TestCase):
    """A turn's two snapshots on a project with big data: read once, the sources kept."""

    def project(self):
        import os
        root = tmpdir()
        (root / "data").mkdir()
        (root / "data" / "big.csv").write_bytes(b"x" * 300_000)          # walked before sections/
        (root / "sections").mkdir()
        (root / "sections" / "a.tex").write_text("one\n", encoding="utf-8")
        for p in (root / "data" / "big.csv", root / "sections" / "a.tex"):
            os.utime(p, ns=(10**18, 10**18))                             # written long ago
        return root

    def test_the_budget_for_undo_goes_to_the_sources_first(self):
        root = self.project()
        with mock.patch.object(snapshot, "MAX_BYTES", 200_000):
            snap, _ = snapshot.take(root)
        self.assertIsNotNone(snap["sections/a.tex"].data, "a 4-byte .tex could not be undone")
        self.assertIsNone(snap["data/big.csv"].data, "only its digest")

    def test_what_did_not_change_is_not_read_again(self):
        import os
        root = self.project()
        before, _ = snapshot.take(root)
        (root / "sections" / "a.tex").write_bytes(b"two\n")                   # the turn's edit, now
        reads = []
        real = Path.read_bytes
        with mock.patch.object(Path, "read_bytes", lambda p: reads.append(p.name) or real(p)):
            after, _ = snapshot.take(root, since=before)
        self.assertEqual(reads, ["a.tex"], "the unchanged big file was read again")
        self.assertEqual([c["path"] for c in snapshot.changes(before, after)], ["sections/a.tex"])
        # Changed in place with its size and mtime kept (as within a clock step): a file that
        # recent is read again however its stat looks.
        st = (root / "sections" / "a.tex").stat()
        (root / "sections" / "a.tex").write_bytes(b"TWO\n")
        os.utime(root / "sections" / "a.tex", ns=(st.st_atime_ns, st.st_mtime_ns))
        again, _ = snapshot.take(root, since=after)
        self.assertEqual(again["sections/a.tex"].data, b"TWO\n")

    def test_a_big_text_file_is_described_not_diffed(self):
        a = snapshot.FileState(b"line\n" * 30000, 0o644, digest="a")
        b = snapshot.FileState(b"line\n" * 30001, 0o644, digest="b")
        out = snapshot.diff("data.bib", a, b)
        self.assertIn("Large file changed", out)
        self.assertLess(len(out), 300)


if __name__ == "__main__":
    unittest.main()
