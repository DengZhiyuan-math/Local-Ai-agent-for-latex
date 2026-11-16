"""The project's versions (versions.py): history, pins, restoring; the agent's tools for them
(mcp_compile.py) and the editor's routes (server.py)."""
import json
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitsync  # noqa: E402
import mcp_compile  # noqa: E402
import versions  # noqa: E402
from agent import AgentManager  # noqa: E402
from backends import Job  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def identity(cwd):
    git(cwd, "config", "user.email", "t@example.com")
    git(cwd, "config", "user.name", "T")
    git(cwd, "config", "commit.gpgsign", "false")
    git(cwd, "config", "tag.gpgsign", "false")
    git(cwd, "config", "core.autocrlf", "false")


@unittest.skipUnless(shutil.which("git"), "needs git")
class VersionsTest(unittest.TestCase):
    """Three files, then merged into one: the case an agent could not get back from."""

    def setUp(self):
        self.root = tmpdir()
        git(self.root, "init", "-q", "-b", "main")
        identity(self.root)
        for name in ("intro.tex", "method.tex", "results.tex"):
            (self.root / name).write_bytes((f"{name} text\n").encode())
        (self.root / "main.tex").write_bytes(("\\input{intro}\\input{method}\\input{results}\n").encode())
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "three files")
        self.three = git(self.root, "rev-parse", "HEAD")
        for name in ("intro.tex", "method.tex", "results.tex"):
            (self.root / name).unlink()
        (self.root / "main.tex").write_bytes(("all in one\n").encode())
        (self.root / "finite-volume.tex").write_bytes(("merged\n").encode())
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "merged into one")
        self.g = gitsync.GitSync(lambda: self.root, lambda: "build")
        self.v = versions.Versions(self.g)

    def test_log_and_show_name_the_files_of_an_old_version(self):
        log = self.v.log()
        self.assertEqual([c["message"] for c in log], ["merged into one", "three files"])
        shown = self.v.show(self.three[:8])
        self.assertEqual(sorted(shown["files"]), ["intro.tex", "main.tex", "method.tex", "results.tex"])
        self.assertEqual(self.v.show("HEAD~1", "method.tex")["content"], "method.tex text\n")
        changed = {c["path"]: c["status"] for c in self.v.show("HEAD")["changed"]}
        self.assertEqual(changed["intro.tex"], "D")
        self.assertEqual(changed["finite-volume.tex"], "A")

    def test_diff_to_now(self):
        d = self.v.diff("HEAD~1")
        self.assertIn("-intro.tex text", d["diff"])
        self.assertIn("+merged", d["diff"])

    def test_relative_restore_keeps_the_target_before_saving_edits(self):
        (self.root / "main.tex").write_bytes(b"unsaved draft\n")
        r = self.g.restore_version("HEAD~1")
        self.assertEqual(r["hash"], self.three)
        self.assertEqual((self.root / "intro.tex").read_bytes(), b"intro.tex text\n")
        self.assertEqual(git(self.root, "show", "HEAD~1:main.tex"), "unsaved draft")

    def test_restore_refuses_an_ignored_draft_before_writing_anything(self):
        (self.root / ".gitignore").write_bytes(b"intro.tex\n")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-qm", "ignore a local draft")
        (self.root / "intro.tex").write_bytes(b"private draft\n")
        before = (self.root / "main.tex").read_bytes()
        with self.assertRaisesRegex(versions.VersionError, "untracked|ignored"):
            self.g.restore_version(self.three)
        self.assertEqual((self.root / "intro.tex").read_bytes(), b"private draft\n")
        self.assertEqual((self.root / "main.tex").read_bytes(), before)
        self.assertEqual((self.root / ".gitignore").read_bytes(), b"intro.tex\n")

    def test_restore_handles_file_and_directory_transitions(self):
        chapter = self.root / "chapter"
        chapter.write_bytes(b"flat chapter\n")
        git(self.root, "add", "chapter")
        git(self.root, "commit", "-qm", "flat chapter")
        flat = git(self.root, "rev-parse", "HEAD")
        chapter.unlink()
        chapter.mkdir()
        (chapter / "intro.tex").write_bytes(b"nested chapter\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "split chapter")
        nested = git(self.root, "rev-parse", "HEAD")
        self.g.restore_version(flat)
        self.assertEqual(chapter.read_bytes(), b"flat chapter\n")
        self.g.restore_version(nested)
        self.assertEqual((chapter / "intro.tex").read_bytes(), b"nested chapter\n")

    def test_directory_replacement_preserves_untracked_children(self):
        chapter = self.root / "chapter"
        chapter.write_bytes(b"flat\n")
        git(self.root, "add", "chapter")
        git(self.root, "commit", "-qm", "flat")
        flat = git(self.root, "rev-parse", "HEAD")
        chapter.unlink()
        chapter.mkdir()
        (chapter / "intro.tex").write_bytes(b"tracked\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "nested")
        (chapter / "draft.tex").write_bytes(b"untracked\n")
        with self.assertRaisesRegex(versions.VersionError, "untracked|ignored"):
            self.v.restore(flat)
        self.assertEqual((chapter / "intro.tex").read_bytes(), b"tracked\n")
        self.assertEqual((chapter / "draft.tex").read_bytes(), b"untracked\n")

    def test_layout_commit_keeps_unrelated_staged_edits_out(self):
        chapter = self.root / "chapter"
        chapter.write_bytes(b"old\n")
        git(self.root, "add", "chapter")
        git(self.root, "commit", "-qm", "flat")
        chapter.unlink()
        chapter.mkdir()
        (chapter / "intro.tex").write_bytes(b"new\n")
        (self.root / "main.tex").write_bytes(b"unrelated staged edit\n")
        git(self.root, "add", "main.tex")
        self.assertTrue(self.g.commit(["chapter", "chapter/intro.tex"], "split chapter"))
        self.assertEqual(git(self.root, "show", "HEAD:main.tex"), "all in one")
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only"), "main.tex")

    def test_pin_sync_retries_after_a_restart_even_without_new_commits(self):
        remote = tmpdir()
        git(remote, "init", "-q", "--bare", "-b", "main")
        git(self.root, "remote", "add", "origin", str(remote))
        git(self.root, "push", "-qu", "origin", "main")
        self.g.set_enabled(False)
        self.g.pin_version("offline")
        self.g.set_enabled(True)
        restarted = gitsync.GitSync(lambda: self.root, lambda: "build")
        real_git = restarted.git
        def offline(*args, **kw):
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 1, "", "offline")
            return real_git(*args, **kw)
        with mock.patch.object(restarted, "git", side_effect=offline):
            self.assertIsNotNone(restarted.push().error)
        self.assertIsNone(restarted.push().error)
        self.assertIsNone(restarted.error)
        self.assertEqual(versions.Versions(restarted).pending(), [])
        self.assertEqual(git(remote, "rev-parse", "offline^{commit}"),
                         git(self.root, "rev-parse", "offline^{commit}"))

    def test_fetch_imports_another_computers_pins(self):
        remote = tmpdir()
        git(remote, "init", "-q", "--bare", "-b", "main")
        git(self.root, "remote", "add", "origin", str(remote))
        git(self.root, "push", "-qu", "origin", "main")
        other = tmpdir()
        git(other, "clone", "-q", "--no-tags", str(remote), ".")
        git(self.root, "tag", "-a", "-m", "from another computer", "elsewhere")
        git(self.root, "push", "-q", "origin", "refs/tags/elsewhere")
        sync = gitsync.GitSync(lambda: other, lambda: "build")
        self.assertEqual(sync.fetch().returncode, 0)
        self.assertEqual([p["name"] for p in versions.Versions(sync).pins()], ["elsewhere"])
        sync.unpin_version("elsewhere")
        sync.fetch()
        self.assertEqual(versions.Versions(sync).pins(), [])
        self.assertTrue(git(remote, "rev-parse", "refs/tags/elsewhere"))

    def test_conflicting_pin_never_overwrites_either_computer(self):
        remote = tmpdir()
        git(remote, "init", "-q", "--bare", "-b", "main")
        git(self.root, "remote", "add", "origin", str(remote))
        git(self.root, "push", "-qu", "origin", "main")
        git(self.root, "tag", "-a", "-m", "remote version", "clash", self.three)
        git(self.root, "push", "-q", "origin", "refs/tags/clash")
        remote_tag = git(remote, "rev-parse", "refs/tags/clash")
        git(self.root, "tag", "-d", "clash")
        self.v.pin("clash", "local version")
        local_tag = git(self.root, "rev-parse", "refs/tags/clash")
        with self.assertRaisesRegex(versions.VersionError, "differs"):
            self.g.fetch()
        self.assertIsNotNone(self.g.push().error)
        self.assertEqual(git(remote, "rev-parse", "refs/tags/clash"), remote_tag)
        self.assertEqual(git(self.root, "rev-parse", "refs/tags/clash"), local_tag)

    def test_pin_resolve_and_refuse_a_taken_name(self):
        r = self.g.pin_version("v1", "the merged version")
        self.assertEqual(r["hash"], git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(self.v.resolve("v1"), r["hash"])
        self.assertEqual(self.v.pins()[0]["note"], "the merged version")
        self.assertEqual(self.v.log()[0]["pins"], ["v1"])
        with self.assertRaises(versions.VersionError):
            self.g.pin_version("v1")
        self.g.unpin_version("v1")
        self.assertEqual(self.v.pins(), [])

    def test_history_folds_by_pinned_version(self):
        def commit(msg):
            (self.root / "main.tex").write_bytes(msg.encode() + b"\n")
            git(self.root, "commit", "-q", "-am", msg)
        self.g.pin_version("v1")                 # "three files", "merged" -> v1
        commit("c3")
        git(self.root, "checkout", "-q", "-b", "side")
        (self.root / "side.tex").write_bytes(b"side\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "on a branch")
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--no-edit", "side")
        self.g.pin_version("v2")                 # c3, the branch, the merge -> v2
        commit("c5")                             # not pinned yet
        r = self.v.grouped()
        names = {g["hash"]: g["names"][0] for g in r["groups"]}
        self.assertEqual([g["names"] for g in r["groups"]], [["v2"], ["v1"]])
        part = {c["message"]: names.get(c["version"]) for c in r["commits"]}
        self.assertEqual(part["c5"], None)
        self.assertEqual((part["c3"], part["on a branch"]), ("v2", "v2"))
        self.assertEqual((part["merged into one"], part["three files"]), ("v1", "v1"))
        self.assertEqual([g["count"] for g in r["groups"]], [2, 2])    # the merge changed no file: git leaves it out

    def test_pin_commits_what_is_not_committed_yet(self):
        (self.root / "main.tex").write_bytes(("edited, not committed\n").encode())
        r = self.g.pin_version("v2")
        self.assertTrue(r["committed"])
        self.assertEqual(self.v.show("v2", "main.tex")["content"], "edited, not committed\n")

    def test_no_option_or_range_reaches_git(self):
        for bad in ("--output=x", "HEAD..main", "main@{1}", "v1;rm", "", "../x"):
            with self.assertRaises(versions.VersionError):
                self.v.resolve(bad)
        for bad in ("-f", "a..b", "x.lock", "has space", ""):
            with self.assertRaises(versions.VersionError):
                versions.check_name(bad)

    def test_restore_from_the_editor_commits_before_and_after(self):
        self.g.pin_version("merged")
        (self.root / "main.tex").write_bytes(("unsaved idea\n").encode())
        r = self.g.restore_version(self.three[:10])
        self.assertTrue(r["saved_before"] and r["committed"])
        self.assertEqual(sorted(p.name for p in self.root.glob("*.tex")),
                         ["intro.tex", "main.tex", "method.tex", "results.tex"])
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        msgs = [c["message"] for c in self.v.log()]
        self.assertTrue(msgs[0].startswith("Restored"), msgs)
        self.assertTrue(msgs[1].startswith("Before restoring"), msgs)
        # and back again: nothing was lost
        self.g.restore_version("merged")
        self.assertTrue((self.root / "finite-volume.tex").exists())
        self.assertEqual(self.v.show("HEAD~2", "main.tex")["content"], "unsaved idea\n")

    def test_restore_some_paths_and_respect_writable(self):
        r = self.v.restore("HEAD~1", ["method.tex"])
        self.assertEqual(r["written"], ["method.tex"])
        self.assertTrue((self.root / "finite-volume.tex").exists())
        r = self.v.restore("HEAD~1", writable=lambda rel: rel != "intro.tex")
        self.assertIn("intro.tex", [s["path"] for s in r["skipped"]])
        self.assertFalse((self.root / "intro.tex").exists())
        self.assertFalse((self.root / "finite-volume.tex").exists())
        with self.assertRaises(versions.VersionError):
            self.v.restore("HEAD~1", ["nope.tex"])

    def test_shared_repository_pins_live_under_the_project(self):
        top = tmpdir()
        git(top, "init", "-q", "-b", "main")
        identity(top)
        (top / gitsync.SHARED_MARKER).write_bytes(b"{\"shared\": true}")
        for proj in ("a", "b"):
            (top / proj).mkdir()
            (top / proj / "main.tex").write_bytes((proj + "\n").encode())
        git(top, "add", "-A")
        git(top, "commit", "-q", "-m", "two projects")
        ga = gitsync.GitSync(lambda: top / "a", lambda: "build")
        gb = gitsync.GitSync(lambda: top / "b", lambda: "build")
        if not (ga.check_repo() and ga.prefix == "a/"):
            self.skipTest("shared repository not recognised here")
        ga.pin_version("v1")
        gb.pin_version("v1")
        self.assertEqual(sorted(git(top, "tag").split()), ["a/v1", "b/v1"])
        self.assertEqual([p["name"] for p in versions.Versions(ga).pins()], ["v1"])
        self.assertEqual(versions.Versions(ga).show("v1")["files"], ["main.tex"])

    def test_shared_project_names_are_encoded_only_in_tag_paths(self):
        top, remote = tmpdir(), tmpdir()
        git(top, "init", "-q", "-b", "main")
        git(remote, "init", "-q", "--bare", "-b", "main")
        identity(top)
        (top / gitsync.SHARED_MARKER).write_text('{"shared": true}')
        paper = top / "答案" / "ex 1.sol~draft"
        paper.mkdir(parents=True)
        (paper / "main.tex").write_bytes(b"paper\n")
        git(top, "add", "-A")
        git(top, "commit", "-qm", "shared paper")
        git(top, "remote", "add", "origin", str(remote))
        git(top, "push", "-qu", "origin", "main")
        sync = gitsync.GitSync(lambda: paper, lambda: "build")
        sync.set_enabled(False)
        sync.pin_version("v1")
        sync.set_enabled(True)
        self.assertIsNone(sync.push().error)
        self.assertEqual(sync.fetch().returncode, 0)
        view = versions.Versions(sync)
        self.assertIn("%20", view.tag_prefix)
        self.assertIn("%7E", view.tag_prefix)
        self.assertEqual(view.show("v1")["files"], ["main.tex"])
        self.assertEqual(git(remote, "rev-parse", view._ref("v1") + "^{commit}"),
                         git(top, "rev-parse", "HEAD"))


@unittest.skipUnless(shutil.which("git"), "needs git")
class ServerRoutes(unittest.TestCase):
    """The editor's routes: the page's restore commits; the agent's is its turn's change."""

    def setUp(self):
        import server
        self.server = server
        self.root = tmpdir()
        git(self.root, "init", "-q", "-b", "main")
        identity(self.root)
        (self.root / "a.tex").write_bytes(b"old\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "old")
        (self.root / "a.tex").write_bytes(b"new\n")
        git(self.root, "commit", "-q", "-am", "new")
        server.set_root(self.root)
        server.GITSYNC.recheck()

    def history(self, path, body):
        return self.server.Handler._history(None, path, body)

    def test_the_agent_restores_only_in_an_edit_turn_and_pins_at_its_end(self):
        s = self.server
        with self.assertRaises(versions.Refused):
            self.history("/api/history/restore", {"rev": "HEAD~1", "by": "agent"})
        job = Job(1)
        job.mode, job.scope = "edit", None
        with mock.patch.object(s.AGENT, "editing", return_value=True), \
                mock.patch.object(s.AGENT, "active", job):
            r = self.history("/api/history/restore", {"rev": "HEAD~1", "by": "agent"})
            self.assertEqual(r["written"], ["a.tex"])
            self.assertEqual(self.history("/api/history/pin", {"name": "v1", "by": "agent"}), {"queued": "v1"})
        # Written, not committed: the turn's own commit records it.
        self.assertEqual((self.root / "a.tex").read_bytes(), b"old\n")
        self.assertEqual(git(self.root, "log", "-1", "--format=%s"), "new")
        self.assertEqual(git(self.root, "tag"), "")
        pinned = s.AGENT._pin_after(job, "ok")["pinned"]
        self.assertEqual([p["name"] for p in pinned], ["v1"])
        self.assertEqual(git(self.root, "show", "v1:a.tex"), "old")

    def test_the_page_waits_for_a_turn(self):
        with mock.patch.object(self.server.AGENT, "busy", return_value=True):
            with self.assertRaises(versions.Refused):
                self.history("/api/history/restore", {"rev": "HEAD~1"})
            with self.assertRaises(versions.Refused):
                self.history("/api/history/pin", {"name": "v1"})


class AgentPins(unittest.TestCase):
    def test_a_pin_waits_for_the_end_of_an_edit_turn(self):
        m = AgentManager(lambda: Path("."), lambda: [], backends=({}, "x", None))
        self.assertIn("error", m.queue_pin("v1", ""))         # no turn
        job = Job(1)
        job.mode = "edit"
        m.active = job
        m.on_pin = lambda name, note: {"name": name, "hash": "a" * 40, "upload": "ok"}
        self.assertEqual(m.queue_pin("v1", "n"), {"queued": "v1"})
        self.assertIn("error", m.queue_pin("v1", "n"))
        self.assertEqual(m._pin_after(job, "ok")["pinned"][0]["name"], "v1")
        self.assertEqual(m._pin_after(job, "stopped")["pinned"], [])


class Tools(unittest.TestCase):
    def test_ask_turns_only_look(self):
        self.assertEqual(mcp_compile.names_for("ask"), ["versions", "version_show", "version_diff"])
        self.assertEqual(set(mcp_compile.names_for("edit")) - set(mcp_compile.names_for("ask")),
                         {"compile", "version_restore", "version_pin"})
        refused = mcp_compile.answer({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "version_restore", "arguments": {"rev": "v1"}}},
                                     "u", "ask")
        self.assertIn("error", refused)

    def test_versions_tool_text(self):
        answer = {"pins": [{"name": "v1", "hash": "a" * 40, "at": 0, "note": "submitted"}],
                  "commits": [{"hash": "b" * 40, "at": int(time.time()), "author": "T",
                               "message": "merged", "pins": ["v1"]}], "sync": True}
        with mock.patch("mcp_compile.request", return_value=answer):
            text, failed = mcp_compile.call("u", "versions", {})
        self.assertFalse(failed)
        self.assertIn("- v1: aaaaaaaaaa", text)
        self.assertIn("merged [pinned: v1]", text)
        with mock.patch("mcp_compile.request", side_effect=ValueError("No version 'v9'")):
            text, failed = mcp_compile.call("u", "version_show", {"rev": "v9"})
        self.assertTrue(failed)
        self.assertIn("v9", text)


if __name__ == "__main__":
    unittest.main()
