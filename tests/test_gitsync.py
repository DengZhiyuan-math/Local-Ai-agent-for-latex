"""Recording changes in the project's repository and syncing with its remote (gitsync.py)."""
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitsync  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def identity(cwd):
    git(cwd, "config", "user.email", "t@example.com")
    git(cwd, "config", "user.name", "T")
    git(cwd, "config", "commit.gpgsign", "false")


@unittest.skipUnless(shutil.which("git"), "needs git")
class GitSyncTest(unittest.TestCase):
    def setUp(self):
        self.remote = tmpdir()
        git(self.remote, "init", "-q", "--bare", "-b", "main")
        self.root = tmpdir()
        git(self.root, "init", "-q", "-b", "main")
        identity(self.root)
        (self.root / "main.tex").write_text("one\n", encoding="utf-8")
        (self.root / "sec.tex").write_text("a\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        git(self.root, "remote", "add", "origin", str(self.remote))
        git(self.root, "push", "-q", "-u", "origin", "main")
        self.g = gitsync.GitSync(lambda: self.root, lambda: "build")

    def last(self):
        return git(self.root, "log", "-1", "--format=%s"), \
            git(self.root, "show", "--name-only", "--format=", "HEAD").splitlines()

    def test_autosave_records_sources_not_build_output(self):
        (self.root / "main.tex").write_text("two\n", encoding="utf-8")
        (self.root / "fig.png").write_bytes(b"\x89PNG")
        (self.root / "main.aux").write_text("aux", encoding="utf-8")
        (self.root / "build").mkdir()
        (self.root / "build" / "main.pdf").write_bytes(b"%PDF")
        self.g.touched()
        self.g.dirty_since = self.g.last_edit = time.time() - gitsync.IDLE - 1
        self.g.tick()
        msg, files = self.last()
        self.assertTrue(msg.startswith("Autosave: "), msg)
        self.assertEqual(sorted(files), ["fig.png", "main.tex"])
        self.assertIsNone(self.g.dirty_since)
        self.assertEqual(git(self.remote, "rev-parse", "main"), git(self.root, "rev-parse", "HEAD"), "pushed")

    def test_no_autosave_while_you_type(self):
        (self.root / "main.tex").write_text("two\n", encoding="utf-8")
        self.g.touched()
        self.g.tick()
        self.assertEqual(self.last()[0], "init")

    def test_agent_turn_commits_apart(self):
        (self.root / "sec.tex").write_text("mine\n", encoding="utf-8")       # yours, before the turn
        self.g.before_turn()
        self.assertEqual(self.last(), ("Your edits before an agent turn: sec.tex", ["sec.tex"]))
        (self.root / "main.tex").write_text("agent\n", encoding="utf-8")
        (self.root / "sec.tex").write_text("mine again\n", encoding="utf-8")  # typed during the turn
        self.g.after_turn(["main.tex"], "[Referenced] x\n\nExpand the remark into a paragraph")
        self.assertEqual(self.last(), ("Agent: Expand the remark into a paragraph", ["main.tex"]))
        self.assertIn("sec.tex", git(self.root, "status", "--porcelain"), "your edit is not in it")

    def test_pull_fast_forwards(self):
        other = tmpdir()
        git(other, "clone", "-q", str(self.remote), ".")
        identity(other)
        (other / "sec.tex").write_text("from the other computer\n", encoding="utf-8")
        git(other, "commit", "-q", "-am", "elsewhere")
        git(other, "push", "-q")
        self.g.pull()
        self.assertEqual((self.root / "sec.tex").read_text(encoding="utf-8"), "from the other computer\n")
        self.assertEqual(self.g.behind, 0)
        self.assertIn("Pulled 1 commit", self.g.notice)

    def test_diverged_is_reported_not_merged(self):
        other = tmpdir()
        git(other, "clone", "-q", str(self.remote), ".")
        identity(other)
        (other / "sec.tex").write_text("theirs\n", encoding="utf-8")
        git(other, "commit", "-q", "-am", "elsewhere")
        git(other, "push", "-q")
        (self.root / "main.tex").write_text("ours\n", encoding="utf-8")
        git(self.root, "commit", "-q", "-am", "here")
        self.g.pull()
        self.assertIn("both changed", self.g.error)
        self.assertEqual((self.root / "sec.tex").read_text(encoding="utf-8"), "a\n")

    def test_switched_off(self):
        self.g.set_enabled(False)
        self.assertFalse(self.g.status()["enabled"])
        (self.root / "main.tex").write_text("two\n", encoding="utf-8")
        self.g.before_turn()
        self.assertEqual(self.last()[0], "init")
        self.g.set_enabled(True)
        self.assertTrue(self.g.status()["enabled"])

    def test_history_and_old_versions(self):
        (self.root / "main.tex").write_text("two\n", encoding="utf-8")
        self.g.commit(["main.tex"], "second")
        log = self.g.log("main.tex")
        self.assertEqual([c["message"] for c in log], ["second", "init"])
        old = self.g.show(log[1]["hash"], "main.tex")
        self.assertEqual(old["content"], "one\n")
        self.assertIn("+two", self.g.show(log[0]["hash"], "main.tex")["diff"])
        with self.assertRaises(ValueError):
            self.g.show("HEAD; rm", "main.tex")

    def test_inside_another_repository(self):
        (self.root / "paper").mkdir()
        g = gitsync.GitSync(lambda: self.root / "paper", lambda: "build")
        self.assertFalse(g.status()["own"])
        self.assertFalse(g.active())


@unittest.skipUnless(shutil.which("git"), "needs git")
class RepositoryCreatedLater(unittest.TestCase):
    def test_an_open_editor_finds_a_new_repository(self):
        root = tmpdir()
        (root / "main.tex").write_text("x\n", encoding="utf-8")
        g = gitsync.GitSync(lambda: root, lambda: "build")
        self.assertFalse(g.status()["own"])
        git(root, "init", "-q", "-b", "main")              # the Home page's "+ GitHub", say
        self.assertFalse(g.check_repo(), "not looked for again by itself: no timer")
        self.assertTrue(g.recheck())                       # the Home page tells it, or the menu opens
        self.assertTrue(g.status()["own"])


@unittest.skipUnless(shutil.which("git"), "needs git")
class SharedRepositoryTest(unittest.TestCase):
    """Several projects in one repository: each editor records its own project only."""

    def setUp(self):
        self.remote = tmpdir()
        git(self.remote, "init", "-q", "--bare", "-b", "main")
        self.top = tmpdir()
        git(self.top, "init", "-q", "-b", "main")
        identity(self.top)
        (self.top / gitsync.SHARED_MARKER).write_text('{"shared": true}', encoding="utf-8")
        for name in ("ex 1", "ex 2"):
            (self.top / "sheets" / name).mkdir(parents=True)
            (self.top / "sheets" / name / "main.tex").write_text("one\n", encoding="utf-8")
        git(self.top, "add", "-A")
        git(self.top, "commit", "-q", "-m", "init")
        git(self.top, "remote", "add", "origin", str(self.remote))
        git(self.top, "push", "-q", "-u", "origin", "main")
        self.a = self.top / "sheets" / "ex 1"
        self.g = gitsync.GitSync(lambda: self.a, lambda: "build")

    def test_commits_only_its_own_project_and_pushes(self):
        st = self.g.status()
        self.assertTrue(st["own"])
        self.assertEqual(st["shared"], self.top.name)
        (self.a / "main.tex").write_text("two\n", encoding="utf-8")
        (self.a / "build").mkdir()
        (self.a / "build" / "main.pdf").write_bytes(b"%PDF")
        (self.top / "sheets" / "ex 2" / "main.tex").write_text("theirs\n", encoding="utf-8")
        self.g.touched()
        self.g.dirty_since = self.g.last_edit = time.time() - gitsync.IDLE - 1
        self.g.tick()
        self.assertEqual(git(self.top, "log", "-1", "--format=%s"), "ex 1: Autosave: main.tex")
        self.assertEqual(git(self.top, "show", "--name-only", "--format=", "HEAD"), "sheets/ex 1/main.tex")
        self.assertIn("sheets/ex 2/main.tex", git(self.top, "status", "--porcelain"), "the other project's edit stays")
        self.assertEqual(git(self.remote, "rev-parse", "main"), git(self.top, "rev-parse", "HEAD"), "pushed")

    def test_agent_turns_and_history_use_project_paths(self):
        (self.a / "main.tex").write_text("two\n", encoding="utf-8")
        self.g.after_turn(["main.tex"], "Fix the proof")
        self.assertEqual(git(self.top, "log", "-1", "--format=%s"), "ex 1: Agent: Fix the proof")
        log = self.g.log("main.tex")
        self.assertEqual(len(log), 2)
        old = self.g.show(log[1]["hash"], "main.tex")
        self.assertEqual((old["content"], old["path"]), ("one\n", "main.tex"))

    def test_no_marker_no_sync(self):
        (self.top / gitsync.SHARED_MARKER).unlink()
        self.assertFalse(gitsync.GitSync(lambda: self.a, lambda: "build").check_repo())


if __name__ == "__main__":
    unittest.main()
