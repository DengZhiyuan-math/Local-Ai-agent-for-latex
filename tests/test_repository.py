"""A git repository as prism-local works on it (repository.py): read freely, changed by one
process at a time, with another program's git waited for."""
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import repository  # noqa: E402
from repository import Repository, RepoBusy  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@unittest.skipUnless(shutil.which("git"), "needs git")
class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir()
        git(self.root, "init", "-q", "-b", "main")
        for k, v in (("user.email", "t@example.com"), ("user.name", "T"), ("commit.gpgsign", "false")):
            git(self.root, "config", k, v)
        (self.root / "a.tex").write_text("a\n", encoding="utf-8")
        self.repo = Repository.find(self.root)
        self.lock = self.repo.git_dir / "index.lock"

    def test_found_from_any_folder_inside(self):
        (self.root / "sub").mkdir()
        repo = Repository.find(self.root / "sub")
        self.assertEqual((repo.top, repo.git_dir), (self.root.resolve(), self.repo.git_dir))
        self.assertIsNone(Repository.find(Path(os.path.abspath(os.sep))))

    def test_changes_only_inside_changing(self):
        """A command that changes the index outside changing() is a bug: it raises at once,
        instead of racing a save or another process's commit now and then."""
        with self.assertRaises(RuntimeError):
            self.repo.git("add", "-A")
        with self.repo.changing(files=False), self.assertRaises(RuntimeError):
            self.repo.git("add", "-A")              # a fetch's hold: no files
        self.assertEqual(self.repo.git("status", "--porcelain").returncode, 0, "a read: any time")
        with self.repo.changing():
            self.repo.ok("add", "-A")
            self.repo.ok("commit", "-q", "-m", "first")
        self.assertEqual(git(self.root, "log", "--format=%s"), "first")

    def test_another_programs_git_is_waited_for(self):
        """An IDE's git status holds index.lock for a moment: a commit waits for it, rather
        than failing; one still there after LOCK_WAIT is reported, and nothing is done."""
        self.lock.write_bytes(b"")
        threading.Timer(0.5, self.lock.unlink).start()
        with self.repo.changing():
            self.repo.ok("add", "-A")
        self.lock.write_bytes(b"")
        try:
            with mock.patch.object(repository, "LOCK_WAIT", 0.3), self.repo.changing(), \
                    self.assertRaises(RepoBusy):
                self.repo.git("commit", "-q", "-m", "x")
        finally:
            self.lock.unlink()

    def test_an_index_lock_left_by_a_crash_goes(self):
        self.lock.write_bytes(b"")
        old = time.time() - repository.STALE_LOCK - 5
        os.utime(self.lock, (old, old))
        with self.repo.changing():
            self.repo.ok("add", "-A")
        self.assertFalse(self.lock.exists())

    def test_one_thread_at_a_time_and_again_in_the_same(self):
        got = []
        with self.repo.changing():
            with self.repo.changing(wait=0):                # the same thread: again
                pass
            t = threading.Thread(target=lambda: got.append(self._try()))
            t.start()
            t.join()
        self.assertEqual(got, [False], "another thread waits")
        self.assertTrue(self._try())

    def _try(self):
        try:
            with self.repo.changing(wait=0.2):
                return True
        except RepoBusy:
            return False


if __name__ == "__main__":
    unittest.main()
