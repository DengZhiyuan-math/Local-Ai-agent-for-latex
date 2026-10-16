"""Where trash() puts files when PRISM_TRASH_DIR names a folder (desktop.py)."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import desktop  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


class TrashFolder(unittest.TestCase):
    def setUp(self):
        self.src, self.bin = tmpdir(), tmpdir() / "trash"
        env = mock.patch.dict(os.environ, {"PRISM_TRASH_DIR": str(self.bin)})
        env.start()
        self.addCleanup(env.stop)

    def test_files_and_folders_go_into_the_folder_not_the_system_trash(self):
        (self.src / "a.tex").write_text("a", encoding="utf-8")
        (self.src / "d").mkdir()
        (self.src / "d/b.tex").write_text("b", encoding="utf-8")
        with mock.patch.object(desktop, "_win_trash") as win, mock.patch.object(desktop, "_jxa") as mac:
            desktop.trash([self.src / "a.tex", self.src / "d"])
        win.assert_not_called()
        mac.assert_not_called()
        self.assertEqual((self.bin / "a.tex").read_text(encoding="utf-8"), "a")
        self.assertEqual((self.bin / "d/b.tex").read_text(encoding="utf-8"), "b")
        self.assertEqual(list(self.src.iterdir()), [])

    def test_a_name_already_there_is_kept_and_the_new_one_numbered(self):
        for text in ("one", "two", "three"):
            (self.src / "a.tex").write_text(text, encoding="utf-8")
            desktop.trash([self.src / "a.tex"])
        self.assertEqual(sorted(p.name for p in self.bin.iterdir()), ["a (2).tex", "a (3).tex", "a.tex"])
        self.assertEqual((self.bin / "a (3).tex").read_text(encoding="utf-8"), "three")

    def test_a_missing_path_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            desktop.trash([self.src / "gone.tex"])


if __name__ == "__main__":
    unittest.main()
