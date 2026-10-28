"""What git says of a project's folder, looked up once and again only on a change (repoview.py)."""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import proc  # noqa: E402
import repoview  # noqa: E402
from repository import SHARED_MARKER  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@unittest.skipUnless(shutil.which("git"), "needs git")
class RepoView(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir() / "paper"
        write(self.root / "main.tex", "a\n")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "init")

    def counting(self):
        return mock.patch.object(proc, "run", wraps=proc.run)

    def test_asked_again_only_when_the_files_or_the_repository_changed(self):
        self.assertEqual(repoview.changes(self.root, seen=1).files(), {})
        with self.counting() as run:
            self.assertEqual(repoview.changes(self.root, seen=1).files(), {})
            self.assertEqual(repoview.place(self.root).kind, "own")
        self.assertEqual(run.call_count, 0, "nothing changed: no git")
        write(self.root / "main.tex", "b\n")
        self.assertEqual(repoview.changes(self.root, seen=1).files(), {}, "what the caller saw is the key")
        self.assertEqual(repoview.changes(self.root, seen=2).files(), {"main.tex": "M"}, "an edit is seen")
        git(self.root, "commit", "-qam", "second")
        self.assertEqual(repoview.changes(self.root, seen=2).files(), {}, "a commit is seen")
        with self.counting() as run:
            repoview.changes(self.root)
        self.assertEqual(run.call_count, 1, "without `seen`, git is asked")

    def test_what_autosave_records_and_the_branch(self):
        remote = self.root.with_name("paper.git")
        git(self.root.parent, "init", "-q", "--bare", "-b", "main", str(remote))
        git(self.root, "remote", "add", "origin", str(remote))
        git(self.root, "push", "-q", "-u", "origin", "main")
        write(self.root / "new file.tex", "n\n")             # recorded
        write(self.root / "main.aux", "x")                    # never
        write(self.root / "out" / "main.pdf", "%PDF")         # the output folder: never
        git(self.root, "commit", "-q", "--allow-empty", "-m", "local")
        found = repoview.changes(self.root)
        self.assertEqual(found.recordable("out"), ["new file.tex"])
        self.assertEqual((found.branch, found.ahead, found.behind), ("main", 1, 0))
        self.assertIn("main.aux", found.files(), "the tree marks every change git reports")

    def test_a_project_in_a_shared_repository_sees_its_own_files(self):
        top = tmpdir() / "sheets"
        for name in ("ex 1", "ex 2"):
            write(top / name / "main.tex", "a\n")
        write(top / SHARED_MARKER, json.dumps({"shared": True}))
        git(top, "init", "-q", "-b", "main")
        git(top, "add", "-A")
        git(top, "commit", "-qm", "init")
        write(top / "ex 1" / "main.tex", "b\n")
        write(top / "ex 2" / "main.tex", "b\n")
        found = repoview.changes(top / "ex 1")
        self.assertEqual(found.place.kind, "shared")
        self.assertEqual(found.files(), {"main.tex": "M"})
        self.assertEqual(found.recordable("build"), ["ex 1/main.tex"])
        self.assertEqual(found.branch, "main")

    def test_a_new_repository_is_seen(self):
        plain = tmpdir() / "plain"
        write(plain / "main.tex", "a\n")
        if any((p / ".git").exists() for p in plain.parents):
            self.skipTest("the temp folder is inside a repository")
        with self.counting() as run:
            self.assertIsNone(repoview.changes(plain, seen=1))
        self.assertEqual(run.call_count, 0, "no .git anywhere: no git is run")
        git(plain, "init", "-q", "-b", "main")
        self.assertEqual(repoview.changes(plain, seen=1).files(), {"main.tex": "??"})


if __name__ == "__main__":
    unittest.main()
