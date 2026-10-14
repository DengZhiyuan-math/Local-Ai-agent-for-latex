"""Recording changes in the project's repository and syncing with its remote (gitsync.py)."""
import shutil
import subprocess
import sys
import threading
import time
from unittest import mock
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitsync  # noqa: E402
import repository  # noqa: E402
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
        self.g.bind_target()

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

    def test_files_lock_is_held_across_processes(self):
        """Another editor of the repository (another process) writing the files, or its git
        committing or merging: this one waits, and gets the lock once it is done."""
        lock = repository.FilesLock(lambda: self.root / ".git")
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import repository; "
                "from pathlib import Path; l = repository.FilesLock(lambda: Path(sys.argv[2])); "
                "l.acquire(); print('held', flush=True); time.sleep(1.5)")
        p = subprocess.Popen([sys.executable, "-c", code, str(Path(gitsync.__file__).parent), str(self.root / ".git")],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait)
        self.assertEqual(p.stdout.readline().strip(), "held")
        self.assertFalse(lock.acquire(timeout=0.3), "the other process holds it")
        p.wait()
        p.stdout.close()
        self.assertTrue(lock.acquire(timeout=5), "free once it is done")
        with lock:                            # re-entrant
            pass
        lock.release()
        self.assertTrue(lock.acquire(timeout=0), "and let go")
        lock.release()

    def test_files_lock_let_go_when_unlocking_the_file_fails(self):
        """Unlocking the lock file can fail (the folder on a network drive gone): the lock is
        let go anyway, or every later save and sync would wait on it for good."""
        lock = repository.FilesLock(lambda: self.root / ".git")
        self.assertTrue(lock.acquire(timeout=5))
        with mock.patch.object(repository.registry.FileLock, "_lock", side_effect=OSError("gone")):
            with self.assertRaises(OSError):
                lock.release()
        got = []

        def other_thread():
            got.append(lock.acquire(timeout=2))
            if got[-1]:
                lock.release()

        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
        self.assertEqual(got, [True], "another thread gets it")

    def test_a_fetch_cut_off_by_its_timeout_leaves_others_index_lock(self):
        """A fetch or a read takes no index.lock: when one is cut off by its timeout, an
        index.lock there belongs to someone else's commit (a terminal, another editor)."""
        lock = self.root / ".git" / "index.lock"

        def slow(cwd, *args, **kw):
            lock.write_bytes(b"")              # someone else's commit, meanwhile
            raise subprocess.TimeoutExpired(["git", *args], kw.get("timeout", 60))

        self.g.check_repo()
        try:
            for args in (("fetch", "-q", "origin"), ("status", "--porcelain"), ("log", "-1")):
                with mock.patch.object(gitsync.gitenv, "git", slow):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        self.g.git(*args)
                self.assertTrue(lock.exists(), f"git {args[0]} left it alone")
        finally:
            lock.unlink()

    def test_a_failing_start_does_not_end_autosave(self):
        """The editor's start calls off a merge left half-way; that git cut off by its timeout,
        or any bug at the start, must not end the worker (no autosave for the session)."""
        ticks = []

        def tick():
            ticks.append(1)
            self.g.stop.set()

        with mock.patch.object(gitsync, "TICK", 0.01), \
                mock.patch.object(self.g, "_finish_crashed_merge",
                                  side_effect=subprocess.TimeoutExpired(["git", "merge"], 60)), \
                mock.patch.object(self.g, "_remote_state", side_effect=KeyError("a bug")), \
                mock.patch.object(self.g, "tick", tick):
            t = threading.Thread(target=self.g.run)
            t.start()
            t.join(20)
        self.assertEqual(ticks, [1], "the worker went on")

    def test_nothing_to_commit_does_not_wait_for_the_index(self):
        """Another program's git (an IDE's) holds index.lock for a moment: with nothing to
        commit, there is nothing to wait for, and a pull goes on."""
        (self.root / ".git" / "index.lock").write_bytes(b"")
        try:
            self.assertFalse(self.g.commit_all("Autosave before pulling"))
        finally:
            (self.root / ".git" / "index.lock").unlink()

    def test_a_save_between_the_check_and_the_all_recorded_keeps_its_autosave(self):
        """commit_all decides "everything saved is recorded" while a save may call touched():
        the two do not interleave, so that save's autosave stays due."""
        g = self.g
        other = []

        class Comparing(int):              # touched() from another thread, mid-comparison
            def __eq__(self, n):
                t = threading.Thread(target=g.touched)
                t.start()
                t.join(0.3)
                other.append(t)
                return int(self) == n

            __hash__ = int.__hash__

        g.touched()
        g.saves = Comparing(g.saves)
        g.commit_all("Autosave")
        for t in other:
            t.join()
        self.assertTrue(other)
        self.assertIsNotNone(g.dirty_since, "the save's autosave is still due")

    def test_a_save_during_a_commit_keeps_its_autosave_due(self):
        """git may look at a file just before a save writes it: a save that came in while a
        commit ran keeps the next autosave due, or its edit would wait for another one."""
        (self.root / "main.tex").write_text("two\n", encoding="utf-8")
        self.g.touched()
        real = self.g._record

        def record(pick, message):
            ok = real(pick, message)
            self.g.touched()                # a save, its bytes not seen by git
            return ok

        self.g._record = record
        self.g.commit_all("Autosave")
        self.assertIsNotNone(self.g.dirty_since, "still due")
        self.g._record = real
        self.g.commit_all("Autosave")
        self.assertIsNone(self.g.dirty_since)

    def test_an_upload_is_told_what_its_own_push_did(self):
        """The worker may push the upload's commit first, and `error` may still say what
        failed earlier: the upload is told what happened to it, not that."""
        (self.root / "notes.pdf").write_bytes(b"%PDF")
        real = self.g.commit

        def commit_then_the_worker_pushes(paths, message):
            ok = real(paths, message)
            self.g.push()
            self.g.error = "Could not reach GitHub: an hour ago"
            return ok

        self.g.commit = commit_then_the_worker_pushes
        o = self.g.record_file("notes.pdf", "Add notes.pdf")
        self.assertEqual(o.state, "synced", o)
        self.assertEqual(git(self.remote, "rev-parse", "main"), git(self.root, "rev-parse", "HEAD"))

    def test_every_page_gets_every_notice(self):
        """Several ask for the status (the tree's poll, a button's answer, another tab): the
        first to ask must not take a notice away from the others."""
        self.g.notice = "Pulled 1 commit from GitHub"
        a, b = self.g.status(), self.g.status()
        self.assertEqual((a["notice"], a["notice_id"]), (b["notice"], b["notice_id"]))
        self.assertLess(a["notice_age"], 60)
        self.g.notice = "Combined your changes with 1 commit from GitHub"
        self.assertNotEqual(self.g.status()["notice_id"], a["notice_id"], "a new one")

    def test_both_versions_kept_by_a_merge_meanwhile_stay_listed(self):
        """The page asks where both versions are still kept while a merge adds new places:
        those must not be dropped by the look that did not see them."""
        import keepboth
        (self.root / "main.tex").write_text("% [prism-local] mine\n", encoding="utf-8")
        old = {"path": "main.tex", "line": 1, "who": "A", "mtime": 0}
        new = {"path": "sec.tex", "line": 1, "who": "B", "mtime": 0}
        self.g.kept = [old]
        real = keepboth.marked_lines

        def merge_meanwhile(text):
            if new not in self.g.kept:
                self.g.kept = self.g.kept + [new]      # the worker's merge, at this moment
            return real(text)

        with mock.patch.object(keepboth, "marked_lines", merge_meanwhile):
            self.g.kept_both()
        self.assertIn(new, self.g.kept)

    def test_a_save_half_way_is_not_a_change(self):
        """The temporary file a save writes before it replaces the file (fsutil.replace_file)
        is gone a moment later: committing it would fail, or record it."""
        (self.root / "main.tex").write_text("two\n", encoding="utf-8")
        (self.root / "main.tex.4242.17.prism-tmp").write_text("two\n", encoding="utf-8")
        self.assertEqual(self.g.changes(), ["main.tex"])

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

    def test_agent_turn_skips_ignored_build_output(self):
        (self.root / ".gitignore").write_text("build/\n", encoding="utf-8")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-qm", "Ignore build output")
        (self.root / "main.tex").write_text("agent\n", encoding="utf-8")
        (self.root / "sec.tex").write_text("your separate edit\n", encoding="utf-8")
        (self.root / "build").mkdir()
        (self.root / "build/main.pdf").write_bytes(b"%PDF")
        self.g.after_turn(["build/main.pdf", "main.tex"], "Improve the proof")
        self.assertIsNone(self.g.error)
        self.assertEqual(self.last(), ("Agent: Improve the proof", ["main.tex"]))
        self.assertIn("sec.tex", git(self.root, "status", "--porcelain"))
        self.assertEqual((self.root / "build/main.pdf").read_bytes(), b"%PDF")
        self.g.push()
        self.assertEqual(git(self.remote, "rev-parse", "main"), git(self.root, "rev-parse", "HEAD"))

    def test_agent_turn_keeps_tracked_ignored_changes_and_deletions(self):
        (self.root / ".gitignore").write_text("main.tex\nprivate/\n", encoding="utf-8")
        git(self.root, "add", ".gitignore")
        git(self.root, "commit", "-qm", "Ignore local-only files")
        (self.root / "main.tex").write_text("agent\n", encoding="utf-8")
        (self.root / "sec.tex").unlink()
        (self.root / "private").mkdir()
        (self.root / "private/note.txt").write_text("keep local\n", encoding="utf-8")
        (self.root / "main.aux").write_text("build output\n", encoding="utf-8")
        self.g.after_turn(["main.tex", "sec.tex", "private/note.txt", "main.aux"], "Update sources")
        self.assertIsNone(self.g.error)
        self.assertEqual(self.last(), ("Agent: Update sources", ["main.tex", "sec.tex"]))
        self.assertFalse((self.root / "sec.tex").exists())
        self.assertEqual((self.root / "private/note.txt").read_text(), "keep local\n")
        head = git(self.root, "rev-parse", "HEAD")
        self.g.after_turn(["private/note.txt", "main.aux"], "Only local output")
        self.assertIsNone(self.g.error)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)

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

    def test_only_a_button_may_ask_for_a_login(self):
        seen, real = [], gitsync.gitenv.git

        def spy(cwd, *args, interactive=False, **kw):
            if args[0] in ("fetch", "push"):
                seen.append((args[0], interactive))
            return real(cwd, *args, interactive=interactive, **kw)

        with mock.patch.object(gitsync.gitenv, "git", spy):
            (self.root / "main.tex").write_text("two\n", encoding="utf-8")
            self.g.commit_all("Autosave")
            self.g.push()                           # what the worker does
            self.g.pull()
            background, seen[:] = list(seen), []
            (self.root / "main.tex").write_text("three\n", encoding="utf-8")
            self.g.now("push")                      # the buttons
            self.g.now("pull")
        self.assertEqual(background, [("push", False), ("fetch", False)])
        self.assertEqual(seen, [("push", True), ("fetch", True)])
        self.assertEqual(git(self.remote, "rev-parse", "main"), git(self.root, "rev-parse", "HEAD"))

    def test_opening_the_project_pulls_at_once(self):
        other = tmpdir()
        git(other, "clone", "-q", str(self.remote), ".")
        identity(other)
        (other / "sec.tex").write_text("written elsewhere\n", encoding="utf-8")
        git(other, "commit", "-q", "-am", "elsewhere")
        git(other, "push", "-q")
        g = gitsync.GitSync(lambda: self.root, lambda: "build")
        g.stop.set()                      # run() does its start-up work, then no ticks
        g.run()
        self.assertEqual((self.root / "sec.tex").read_text(encoding="utf-8"), "written elsewhere\n")
        self.assertIn("Pulled 1 commit", g.notice)

    def test_diverged_histories_are_merged_not_rewritten(self):
        """Both sides moved on (a co-author pushed while you wrote): a merge keeps both; no
        commit is rewritten (tests/test_collab.py has the clashes)."""
        other = tmpdir()
        git(other, "clone", "-q", str(self.remote), ".")
        identity(other)
        (other / "sec.tex").write_text("theirs\n", encoding="utf-8")
        git(other, "commit", "-q", "-am", "elsewhere")
        git(other, "push", "-q")
        theirs = git(other, "rev-parse", "HEAD")
        (self.root / "main.tex").write_text("ours\n", encoding="utf-8")
        git(self.root, "commit", "-q", "-am", "here")
        ours = git(self.root, "rev-parse", "HEAD")
        self.g.pull()
        self.assertIsNone(self.g.error)
        self.assertIn("Combined", self.g.notice)
        self.assertEqual((self.root / "sec.tex").read_text(encoding="utf-8"), "theirs\n")
        self.assertEqual((self.root / "main.tex").read_text(encoding="utf-8"), "ours\n")
        parents = git(self.root, "log", "-1", "--format=%P").split()
        self.assertEqual(sorted(parents), sorted([ours, theirs]), "a merge of the two, nothing rewritten")

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
        self.g.bind_target()

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
        (self.top / ".gitignore").write_text("**/build/\n", encoding="utf-8")
        git(self.top, "add", ".gitignore")
        git(self.top, "commit", "-qm", "Ignore build output")
        (self.a / "main.tex").write_text("two\n", encoding="utf-8")
        (self.a / "build").mkdir()
        (self.a / "build/main.pdf").write_bytes(b"%PDF")
        self.g.after_turn(["main.tex", "build/main.pdf"], "Fix the proof")
        self.assertIsNone(self.g.error)
        self.assertEqual(git(self.top, "log", "-1", "--format=%s"), "ex 1: Agent: Fix the proof")
        self.assertEqual(git(self.top, "show", "--name-only", "--format=", "HEAD"), "sheets/ex 1/main.tex")
        log = self.g.log("main.tex")
        self.assertEqual(len(log), 2)
        old = self.g.show(log[1]["hash"], "main.tex")
        self.assertEqual((old["content"], old["path"]), ("one\n", "main.tex"))

    def test_no_marker_no_sync(self):
        (self.top / gitsync.SHARED_MARKER).unlink()
        self.assertFalse(gitsync.GitSync(lambda: self.a, lambda: "build").check_repo())


HOLD = """import sys, time
sys.path.insert(0, sys.argv[1])
import repository
from pathlib import Path
with repository.repo_lock(Path(sys.argv[2])):
    print("held", flush=True)
    sys.stdin.readline()
"""


@unittest.skipUnless(shutil.which("git"), "needs git")
class OtherProcesses(unittest.TestCase):
    """Another prism-local process changing the same repository: an editor still pushing as
    it closes while the project opens again, or another project's editor in a shared one."""

    setUp = GitSyncTest.setUp

    def hold(self):
        """Another process holding the repository; returns a function that lets it go."""
        p = subprocess.Popen([sys.executable, "-c", HOLD, str(Path(gitsync.__file__).parent), str(self.g.git_dir)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")

        def release():
            p.stdin.close()
            p.wait(10)
            p.stdout.close()
        self.addCleanup(lambda: p.poll() is None and release())
        return release

    def test_a_merge_another_process_is_making_is_not_called_off(self):
        merge_head = self.g.git_dir / "MERGE_HEAD"
        merge_head.write_text(git(self.root, "rev-parse", "HEAD") + "\n")
        (self.g.git_dir / "MERGE_MSG").write_text("Merge changes from GitHub\n")
        release = self.hold()
        with mock.patch.object(self.g, "git", wraps=self.g.git) as run:
            self.g._finish_crashed_merge()
        self.assertNotIn(("merge", "--abort"), [c.args[:2] for c in run.call_args_list])
        release()
        with mock.patch.object(self.g, "git", wraps=self.g.git) as run:
            self.g._finish_crashed_merge()                # its process is gone: a crash
        self.assertIn(("merge", "--abort"), [c.args[:2] for c in run.call_args_list])

    def test_sync_waits_its_turn_without_an_error(self):
        release = self.hold()
        (self.root / "main.tex").write_text("mine\n", encoding="utf-8")
        with mock.patch.object(repository, "REPO_WAIT", 0.3):
            with self.assertRaises(repository.RepoBusy):
                self.g.commit(["main.tex"], "edit")
            self.g._safe(self.g.push)
        self.assertIsNone(self.g.error, "busy is not an error: the next tick tries again")
        release()
        self.assertTrue(self.g.commit(["main.tex"], "edit"))

    def test_the_lock_is_reentrant_and_shared_by_threads(self):
        with self.g.repo.changing(files=False):
            with self.g.repo.changing(files=False, wait=0):
                pass                                       # the same thread again
            got = []
            t = threading.Thread(target=lambda: got.append(self._try()))
            t.start()
            t.join()
            self.assertEqual(got, [False], "another thread waits")
        self.assertTrue(self._try())

    def _try(self):
        try:
            with self.g.repo.changing(files=False, wait=0.2):
                return True
        except repository.RepoBusy:
            return False

    def test_settings_written_at_once_keep_every_key(self):
        others = [gitsync.GitSync(lambda: self.root, lambda: "build") for _ in range(4)]
        threads = [threading.Thread(target=lambda g=g, i=i: [g._update_settings(**{f"k{i}": n}) for n in range(20)])
                   for i, g in enumerate(others)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = self.g._settings()
        self.assertEqual({k: data.get(k) for k in ("k0", "k1", "k2", "k3")}, {f"k{i}": 19 for i in range(4)})
        self.assertEqual(data["sync_target"]["branch_ref"], "refs/heads/main", "bound earlier, still there")


if __name__ == "__main__":
    unittest.main()
