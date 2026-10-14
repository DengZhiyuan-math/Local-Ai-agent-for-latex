"""Running git the one way prism-local does (gitenv.py)."""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitenv  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Git(unittest.TestCase):
    def setUp(self):
        self.repo = tmpdir() / "练习 1"
        self.repo.mkdir()
        gitenv.git(self.repo, "init", "-q", check=True)

    def test_output_is_utf8_whatever_the_code_page(self):
        top = gitenv.git(self.repo, "rev-parse", "--show-toplevel").stdout.strip()
        self.assertEqual(Path(top).resolve(), self.repo.resolve())

    def test_the_repository_is_found_from_cwd_only(self):
        other = tmpdir()
        gitenv.git(other, "init", "-q", check=True)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
            top = gitenv.git(self.repo, "rev-parse", "--show-toplevel").stdout.strip()
        self.assertEqual(Path(top).resolve(), self.repo.resolve())

    def test_bytes_failures_and_check(self):
        (self.repo / "a.bin").write_bytes(b"\x00\xff")
        gitenv.git(self.repo, "add", "a.bin", check=True)
        blob = gitenv.git(self.repo, "show", ":a.bin", binary=True).stdout
        self.assertEqual(blob, b"\x00\xff")
        self.assertNotEqual(gitenv.git(self.repo, "show", "nope").returncode, 0)
        with self.assertRaises(subprocess.CalledProcessError):
            gitenv.git(self.repo, "show", "nope", check=True)

    def test_git_never_waits_for_input(self):
        # stdin is closed: a git that reads it (hash-object --stdin) gets EOF at once.
        self.assertEqual(gitenv.git(self.repo, "hash-object", "--stdin", timeout=10).returncode, 0)


if __name__ == "__main__":
    unittest.main()
