"""prism-local updates its own checkout before a server starts (selfupdate.py)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import selfupdate  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class SelfUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        origin = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        self.dev, self.app = self.tmp / "dev", self.tmp / "app"
        git(self.tmp, "clone", "-q", str(origin), str(self.dev))
        self.commit(self.dev, "a.py", "one\n")
        git(self.dev, "push", "-q", "-u", "origin", "main")
        git(self.tmp, "clone", "-q", str(origin), str(self.app))
        for repo in (self.dev, self.app):
            git(repo, "config", "user.email", "t@example.org")
            git(repo, "config", "user.name", "T")
        env = {"PRISM_STATE_DIR": str(self.tmp / "state"), "PRISM_AUTO_UPDATE": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def commit(self, repo: Path, name: str, text: str) -> None:
        (repo / name).write_text(text, encoding="utf-8")
        git(repo, "add", name)
        git(repo, "-c", "user.email=t@example.org", "-c", "user.name=T", "commit", "-q", "-m", f"edit {name}")

    def publish(self) -> str:
        self.commit(self.dev, "a.py", "two\n")
        git(self.dev, "push", "-q")
        return git(self.dev, "rev-parse", "HEAD").strip()

    def head(self) -> str:
        return git(self.app, "rev-parse", "HEAD").strip()

    def test_fast_forwards_to_github(self):
        new = self.publish()
        msg = selfupdate.update(self.app)
        self.assertTrue(msg.startswith("updated: 1 commit from origin/main"), msg)
        self.assertEqual(self.head(), new)
        self.assertEqual((self.app / "a.py").read_text(encoding="utf-8"), "two\n")

    def test_up_to_date_says_nothing(self):
        self.assertIsNone(selfupdate.update(self.app))

    def test_checks_at_most_every_few_minutes(self):
        selfupdate.update(self.app)
        old = self.head()
        self.publish()
        self.assertIsNone(selfupdate.update(self.app))
        self.assertEqual(self.head(), old)

    def test_uncommitted_changes_are_left_alone(self):
        old = self.head()
        self.publish()
        (self.app / "a.py").write_text("mine\n", encoding="utf-8")
        self.assertIn("uncommitted changes", selfupdate.update(self.app))
        self.assertEqual(self.head(), old)
        self.assertEqual((self.app / "a.py").read_text(encoding="utf-8"), "mine\n")

    def test_local_commits_are_never_merged(self):
        self.commit(self.app, "b.py", "local\n")
        mine = self.head()
        self.publish()
        self.assertIn("pull by hand", selfupdate.update(self.app))
        self.assertEqual(self.head(), mine)

    def test_other_branch_without_upstream_is_left_alone(self):
        git(self.app, "switch", "-q", "-c", "work")
        self.publish()
        self.assertIn("tracks no branch", selfupdate.update(self.app))

    def test_switched_off(self):
        old = self.head()
        self.publish()
        with mock.patch.dict(os.environ, {"PRISM_AUTO_UPDATE": "0"}):
            self.assertIsNone(selfupdate.update(self.app))
        self.assertEqual(self.head(), old)


if __name__ == "__main__":
    unittest.main()
