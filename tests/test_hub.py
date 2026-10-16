"""Tests for the Home page server (prism_local/hub.py) and the shared project list."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/: tmpdirs, test_lifecycle
from tmpdirs import tmpdir  # noqa: E402
from test_lifecycle import NO_WINDOW, beat, bye, request, stop, wait_file  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
HUB = REPO / "prism_local" / "hub.py"
PROJECT = REPO / "examples" / "minimal"
sys.path.insert(0, str(REPO / "prism_local"))
import hub  # noqa: E402
import publish  # noqa: E402
import registry  # noqa: E402
import instances  # noqa: E402
import settings  # noqa: E402

HEADERS = {"Content-Type": "application/json", "X-Prism-Local": "1"}


class TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.old = os.environ.get("PRISM_STATE_DIR")
        os.environ["PRISM_STATE_DIR"] = str(self.tmp / "state")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("PRISM_STATE_DIR", None)
        else:
            os.environ["PRISM_STATE_DIR"] = self.old


class Title(unittest.TestCase):
    def title(self, tex):
        f = tmpdir() / "main.tex"
        f.write_text(tex, encoding="utf-8")
        return hub.tex_title(f)

    def test_plain(self):
        self.assertEqual(self.title("\\title{On convexity}"), "On convexity")

    def test_short_title_thanks_and_macros(self):
        tex = "\\title[Short]{On \\emph{convex} sets\\thanks{Funded by {X}.} \\\\ and cones}"
        self.assertEqual(self.title(tex), "On convex sets and cones")

    def test_commented_out_and_missing(self):
        self.assertIsNone(self.title("% \\title{Old}\n\\begin{document}"))

    def test_example_project(self):
        self.assertEqual(hub.tex_title(PROJECT / "main.tex"), "A minimal prism-local example")


class Registry(TempState):
    def test_touch_adds_once_and_records_opening(self):
        registry.touch(PROJECT)
        first = registry.load_projects()["projects"][0]["opened"]
        time.sleep(0.01)
        registry.touch(PROJECT)
        projects = registry.load_projects()["projects"]
        self.assertEqual(len(projects), 1)
        self.assertGreater(projects[0]["opened"], first)

    def test_a_damaged_project_list_is_never_written_back_empty(self):
        """projects.json damaged (a hand edit) or held by another program: opening a project
        must not take it for empty and write back a list with that one project."""
        state = Path(os.environ["PRISM_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        damaged = b'{"projects": [{"path": "C:/papers/a"}, '
        (state / "projects.json").write_bytes(damaged)
        with self.assertRaises(registry.StateUnreadable):
            registry.touch(PROJECT)
        registry.safe_touch(PROJECT)                     # what an editor's start does
        self.assertEqual((state / "projects.json").read_bytes(), damaged, "left as it was")

    def test_damaged_settings_are_kept_and_the_claude_account_is_not_lifted(self):
        """settings.json damaged: saving a setting does not replace it with the defaults,
        and a turn does not run as if no Claude account were set."""
        import backend_claude
        state = Path(os.environ["PRISM_STATE_DIR"])
        state.mkdir(parents=True, exist_ok=True)
        damaged = b'{"claude_account": "me@example.com", '
        (state / "settings.json").write_bytes(damaged)
        with self.assertRaises(registry.StateUnreadable):
            settings.save({"git_init": True})
        self.assertEqual((state / "settings.json").read_bytes(), damaged, "left as it was")
        problem = backend_claude.ClaudeCode("claude").preflight(PROJECT)
        self.assertIn("Nothing was sent", problem or "")

    def test_a_state_file_keeps_its_previous_version_and_a_damaged_one_names_it(self):
        """After a power cut the disk may hold an empty projects.json or settings.json
        (written but not yet on the disk): the version before stays as .bak."""
        state = Path(os.environ["PRISM_STATE_DIR"])
        settings.save({"git_init": False})
        settings.save({"github_repo": True})
        bak = state / "settings.json.bak"
        self.assertFalse(json.loads(bak.read_text(encoding="utf-8"))["github_repo"], "the one before")
        (state / "settings.json").write_bytes(b"")              # what a power cut can leave
        with self.assertRaises(registry.StateUnreadable) as e:
            settings.load()
        self.assertIn(str(bak), str(e.exception))
        self.assertEqual((state / "settings.json").read_bytes(), b"", "left as it was")

    def test_project_key_is_stable(self):
        self.assertEqual(registry.project_key(PROJECT), registry.project_key(Path(str(PROJECT))))

    def test_folder_move_validates_the_list_before_moving_files(self):
        root, new = self.tmp / "paper", self.tmp / "renamed"
        root.mkdir()
        file = registry._registry_path()
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b'{"projects": [')
        with self.assertRaises(registry.StateUnreadable):
            registry.move_folder(root, new)
        self.assertTrue(root.exists())
        self.assertFalse(new.exists())

    def test_the_list_can_change_while_a_folder_moves_and_the_change_is_kept(self):
        """The move runs without the list's lock: another window pinning or opening a
        project meanwhile neither waits for it nor is written over."""
        root, new, other = self.tmp / "paper", self.tmp / "renamed", self.tmp / "other"
        root.mkdir()
        other.mkdir()
        registry.touch(root)
        registry.touch(other)
        rename, during = os.rename, []

        def slow_rename(source, target):
            t = time.monotonic()
            registry.update_projects(lambda d: d["projects"][-1].update(pinned=True))   # another window
            during.append(time.monotonic() - t)
            return rename(source, target)

        with mock.patch.object(registry.os, "rename", side_effect=slow_rename):
            registry.move_folder(root, new)
        self.assertLess(during[0], 2, "the change waited for the move")
        paths = {e["path"]: e for e in registry.load_projects()["projects"]}
        self.assertEqual(set(paths), {str(new), str(other)})
        self.assertTrue(paths[str(other)].get("pinned"), "the other window's change was written over")
        self.assertEqual(list((registry.state_dir() / "moves").glob("*.json")), [])

    def test_metadata_write_failure_rolls_the_folder_back(self):
        root, new = self.tmp / "paper", self.tmp / "renamed"
        root.mkdir()
        (root / "main.tex").write_text("paper")
        registry.touch(root)
        write = registry.write_state
        def fail_list(file, data):
            if file == registry._registry_path():
                raise PermissionError("fixture: metadata is locked")
            return write(file, data)
        with mock.patch.object(registry, "write_state", side_effect=fail_list):
            with self.assertRaisesRegex(OSError, "restored"):
                registry.move_folder(root, new)
        self.assertEqual((root / "main.tex").read_text(), "paper")
        self.assertFalse(new.exists())
        self.assertEqual(registry.load_projects()["projects"][0]["path"], str(root))
        self.assertEqual(list((registry.state_dir() / "moves").glob("*.json")), [])

    def test_failed_rollback_reports_the_actual_path_and_keeps_recovery_details(self):
        root, new = self.tmp / "paper", self.tmp / "renamed"
        root.mkdir()
        registry.touch(root)
        write, rename = registry.write_state, os.rename
        def fail_list(file, data):
            if file == registry._registry_path():
                raise PermissionError("fixture: metadata is locked")
            return write(file, data)
        def fail_rollback(source, target):
            if source == new:
                raise PermissionError("fixture: rollback is locked")
            return rename(source, target)
        with mock.patch.object(registry, "write_state", side_effect=fail_list), \
                mock.patch.object(registry.os, "rename", side_effect=fail_rollback):
            with self.assertRaises(registry.MoveIncomplete) as raised:
                registry.move_folder(root, new)
        self.assertEqual(raised.exception.actual, new)
        self.assertTrue(new.exists())
        recovery = registry.read_json(raised.exception.journal)
        self.assertEqual((recovery["from"], recovery["to"]), (str(root), str(new)))
        self.assertEqual(recovery["projects"]["projects"][0]["path"], str(new))

    def test_partial_cross_device_move_is_not_retried_into_its_own_target(self):
        root, new = self.tmp / "paper", self.tmp / "renamed"
        root.mkdir()
        registry.touch(root)
        def partial_move(source, target):
            target.mkdir()
            (target / "main.tex").write_text("copied")
            raise PermissionError("fixture: source removal blocked after copying")
        with mock.patch.object(registry.shutil, "move", side_effect=partial_move) as move:
            with self.assertRaisesRegex(OSError, "Recovery details"):
                registry.move_folder(root, new, wait=0.3, across=True)
        self.assertEqual(move.call_count, 1)
        self.assertTrue(root.exists())
        self.assertEqual((new / "main.tex").read_text(), "copied")
        self.assertFalse((new / root.name).exists(), "no nested second copy")
        self.assertEqual(registry.load_projects()["projects"][0]["path"], str(root))
        self.assertTrue((registry.state_dir() / "moves" / (registry.project_key(root)+".json")).exists())


@unittest.skipIf(os.name == "nt", "desktop dialogs: Linux")
class FolderDialog(unittest.TestCase):
    def test_zenity_or_kdialog_before_tkinter(self):
        from unittest import mock
        with mock.patch.object(hub.sys, "platform", "linux"), \
                mock.patch.object(hub.shutil, "which", side_effect=lambda c: c in ("zenity", "kdialog") and c):
            cmds = hub.native_folder_dialogs("/home/me/papers", "Location")
        self.assertEqual([c[0] for c in cmds], ["zenity", "kdialog"])
        self.assertIn("--filename=/home/me/papers/", cmds[0])


class Instances(TempState):
    """Read-only instance inspection and process identity checks."""

    def silent(self) -> str:
        """The address of a server that takes connections but does not answer (busy)."""
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)
        self.addCleanup(s.close)
        return f"http://127.0.0.1:{s.getsockname()[1]}/"

    def closed(self) -> str:
        """An address nothing listens at."""
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return f"http://127.0.0.1:{port}/"

    def test_a_live_server_that_does_not_answer_keeps_its_file(self):
        inst = instances.path(self.tmp / "slow")
        registry.write_json(inst, {"pid": os.getpid(), "url": self.silent()})
        self.assertEqual(instances.inspect(self.tmp / "slow", timeout=0.3).state, "unresponsive")
        self.assertTrue(inst.exists())

    def test_nothing_listening_at_its_address_is_a_server_gone(self):
        """Where the system cannot say when a process started (macOS), a new process given
        an ended server's pid passes the process check; nothing listening at its address
        makes the instance absent. Only a later ensure may replace the stale record."""
        inst = instances.path(self.tmp / "gone")
        registry.write_json(inst, {"pid": os.getpid(), "url": self.closed()})      # a live pid
        with mock.patch.object(instances, "process_started", return_value=None):
            self.assertEqual(instances.inspect(self.tmp / "gone", timeout=5).state, "absent")
        self.assertTrue(inst.exists(), "inspection does not delete records")

    def test_inspecting_a_server_that_ended_keeps_its_record(self):
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        inst = instances.path(self.tmp / "gone")
        registry.write_json(inst, {"pid": ended.pid, "url": "http://127.0.0.1:9/"})
        self.assertEqual(instances.inspect(self.tmp / "gone", timeout=0.3).state, "absent")
        self.assertTrue(inst.exists(), "inspection does not delete records")

    def test_server_alive(self):
        self.assertTrue(instances.server_alive(os.getpid()))
        self.assertFalse(instances.server_alive(None))
        self.assertFalse(instances.server_alive(-5))

    def test_a_pid_given_to_a_new_process_is_not_the_server(self):
        # After a crash the system may give the server's pid to a new process, which
        # started later than the instance file says: the file is stale.
        started = instances.process_started(os.getpid())
        if started is None:
            self.skipTest("this system does not say when a process started")
        self.assertEqual(instances.process_started(os.getpid()), started)
        self.assertIsNone(instances.process_started(None))
        self.assertTrue(instances.server_alive(os.getpid(), started))
        self.assertFalse(instances.server_alive(os.getpid(), started + 1))
        inst = instances.path(self.tmp / "slow")
        registry.write_json(inst, {"pid": os.getpid(), "started": started, "url": self.silent()})
        self.assertEqual(instances.inspect(self.tmp / "slow", timeout=0.3).state, "unresponsive")
        self.assertTrue(inst.exists(), "the server itself, only slow")
        registry.write_json(inst, {"pid": os.getpid(), "started": started + 1, "url": self.silent()})
        self.assertEqual(instances.inspect(self.tmp / "slow", timeout=0.3).state, "absent")
        self.assertTrue(inst.exists(), "inspection is read-only, even for a reused pid")


class Create(TempState):
    def test_creates_template_and_lists_it(self):
        r = hub.create_project({"name": "paper", "parent": str(self.tmp), "template": "amsart",
                                "title": "On things", "author": "A. N. Author"})
        root = self.tmp / "paper"
        self.assertEqual(Path(r["path"]), root.resolve())
        main = (root / "main.tex").read_text(encoding="utf-8")
        self.assertIn("\\title{On things}", main)
        self.assertIn("\\author{A. N. Author}", main)
        self.assertTrue((root / "sections" / "intro.tex").is_file())
        self.assertEqual(json.loads((root / "prism.json").read_text())["main"], "main.tex")
        listed = hub.list_projects()
        self.assertEqual([p["name"] for p in listed["projects"]], ["paper"])
        self.assertEqual(listed["projects"][0]["title"], "On things")
        self.assertEqual(Path(listed["default_parent"]), self.tmp.resolve())

    def test_rejects_bad_names_and_existing_folders(self):
        for name in ("", "a/b", "a:b", "..", "x."):
            with self.assertRaises(ValueError, msg=name):
                hub.create_project({"name": name, "parent": str(self.tmp)})
        (self.tmp / "taken").mkdir()
        (self.tmp / "taken" / "file.txt").write_text("x")
        with self.assertRaises(ValueError):
            hub.create_project({"name": "taken", "parent": str(self.tmp)})
        with self.assertRaises(ValueError):
            hub.create_project({"name": "ok", "parent": "relative/path"})

    def test_new_repository_is_ready_for_co_authors(self):
        hub.create_project({"name": "co", "parent": str(self.tmp), "template": "empty", "git": True})
        root = self.tmp / "co"
        if not (root / ".git").is_dir():
            self.skipTest("git not available")
        self.assertIn("* text=auto", (root / ".gitattributes").read_text(encoding="utf-8"))
        hook = root / ".git" / "hooks" / "pre-push"
        self.assertIn(hub.gitsync.GUARD_MARK, hook.read_text(encoding="utf-8"))

    def test_git_info_of_new_repository(self):
        hub.create_project({"name": "g", "parent": str(self.tmp), "template": "empty", "git": True})
        g = hub.git_info(self.tmp / "g")
        if g is None:
            self.skipTest("git not available")
        self.assertIn(g["branch"], ("main", "master"))
        self.assertGreater(g["changes"], 0)

    def test_a_new_repository_is_announced_to_the_editor_unless_it_is_this_process(self):
        # The editor's own publish runs here holding its sync lock: asking it would wait for that.
        for pid, told in ((os.getpid(), False), (os.getpid() + 1, True)):
            with mock.patch.object(instances, "inspect", return_value=instances.State("ready",
                    {"url": "http://127.0.0.1:9/", "pid": pid})), \
                    mock.patch.object(instances.HTTP, "open") as asked:
                hub.tell_editor(self.tmp)
            self.assertEqual(asked.called, told, pid)

    def test_git_info_of_some_or_all_projects(self):
        hub.create_project({"name": "g", "parent": str(self.tmp), "template": "empty", "git": True})
        hub.create_project({"name": "plain", "parent": str(self.tmp), "template": "empty", "git": False})
        g, plain = (hub.registry.project_key((self.tmp / n).resolve()) for n in ("g", "plain"))
        if any((p / ".git").exists() for p in self.tmp.resolve().parents) or not (self.tmp / "g" / ".git").exists():
            self.skipTest("git not available, or the temp folder is inside a repository")
        with mock.patch.object(hub.gitenv.proc, "run", wraps=hub.gitenv.proc.run) as run:
            self.assertIsNone(hub.git_info(self.tmp / "plain"))
            run.assert_not_called()                 # no .git anywhere: no git is run
        self.assertEqual(set(hub.all_git_info()), {g, plain})
        self.assertEqual(list(hub.all_git_info({g})), [g])


class ProjectFolders(TempState):
    """Home's menu: copy a project's folder, paste folders as projects, delete to the Trash."""

    def project(self, name):
        p = self.tmp / name
        p.mkdir(parents=True)
        (p / "main.tex").write_text("x", encoding="utf-8")
        return hub.add_project(str(p))["id"], p.resolve()

    def ids(self):
        return [registry.project_key(Path(e["path"])) for e in registry.load_projects()["projects"]]

    def test_delete_moves_the_folder_to_the_trash_and_forgets_it(self):
        pid, root = self.project("paper")
        with mock.patch.object(hub.desktop, "trash") as trash:
            self.assertEqual(hub.trash_project(pid), {"ok": True})
        trash.assert_called_once_with([root])
        self.assertNotIn(pid, self.ids())

    def test_never_while_its_editor_runs_nor_a_folder_holding_more(self):
        pid, _ = self.project("paper")
        outer, _ = self.project("topic")
        self.project("topic/inner")
        home = hub.add_project(str(Path.home()))["id"]
        busy = mock.Mock(occupied=True)
        with mock.patch.object(hub.desktop, "trash") as trash:
            with mock.patch.object(instances, "inspect", return_value=busy), \
                    self.assertRaisesRegex(instances.Busy, "editor is open"):
                hub.trash_project(pid)
            for bad in (outer, home):
                with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "holds more"):
                    hub.trash_project(bad)
        trash.assert_not_called()
        self.assertTrue({pid, outer, home} <= set(self.ids()))

    def test_paste_adds_copied_folders_as_projects_in_place(self):
        folder = hub.create_folder({"name": "Topic"})
        a, b, f = self.tmp / "a", self.tmp / "b", self.tmp / "note.txt"
        a.mkdir(), b.mkdir(), f.write_text("x")
        before = sorted(p for p in self.tmp.rglob("*") if "state" not in p.parts)
        with mock.patch.object(hub.desktop, "clipboard_files", return_value=[a, b, f]):
            r = hub.paste_projects(folder["id"])
        # Listed where they are: nothing on disk is copied.
        self.assertEqual(sorted(p for p in self.tmp.rglob("*") if "state" not in p.parts), before)
        self.assertEqual([x["path"] for x in r["added"]], [str(a.resolve()), str(b.resolve())])
        self.assertEqual(r["skipped"], ["note.txt: not a folder"])
        self.assertEqual({e.get("folder_id") for e in registry.load_projects()["projects"]}, {folder["id"]})
        with mock.patch.object(hub.desktop, "clipboard_files", return_value=[]), \
                self.assertRaisesRegex(ValueError, "no folders"):
            hub.paste_projects()

    def test_copy_puts_the_folder_on_the_clipboard(self):
        pid, root = self.project("paper")
        with mock.patch.object(hub.desktop, "copy_files") as copy:
            hub.copy_project(pid)
        copy.assert_called_once_with([root])


class FoldersAndTags(TempState):
    def setUp(self):
        super().setUp()
        self.pid = hub.add_project(str(PROJECT))["id"]

    def entry(self):
        return registry.load_projects()["projects"][0]

    def test_topic_with_subprojects(self):
        topic = hub.create_folder({"name": " Dynamics ", "note": "the grant"})
        sub = hub.create_folder({"name": "Paper 1", "parent": topic["id"]})
        self.assertEqual((topic["name"], sub["parent"]), ("Dynamics", topic["id"]))
        hub.change_project(self.pid, {"folder_id": sub["id"]})
        listed = hub.list_projects()
        self.assertEqual(listed["projects"][0]["folder_id"], sub["id"])
        self.assertEqual({f["id"] for f in listed["folders"]}, {topic["id"], sub["id"]})
        with self.assertRaises(ValueError):            # no cycles
            hub.change_folder({"id": topic["id"], "parent": sub["id"]})
        with self.assertRaises(ValueError):
            hub.change_project(self.pid, {"folder_id": "nope"})
        with self.assertRaises(ValueError):
            hub.create_folder({"name": "  "})
        # Deleting a folder moves its projects and subfolders up, never deletes them.
        hub.remove_folder(sub["id"])
        self.assertEqual(self.entry()["folder_id"], topic["id"])
        inner = hub.create_folder({"name": "Inner", "parent": topic["id"]})
        hub.remove_folder(topic["id"])
        self.assertNotIn("folder_id", self.entry())
        self.assertIsNone(registry.load_projects()["folders"][0]["parent"])
        self.assertEqual(registry.load_projects()["folders"][0]["id"], inner["id"])

    def test_add_and_create_into_a_folder(self):
        f = hub.create_folder({"name": "Topic"})
        hub.add_project(str(PROJECT), f["id"])
        self.assertEqual(self.entry()["folder_id"], f["id"])
        r = hub.create_project({"name": "p2", "parent": str(self.tmp), "template": "empty",
                                "folder_id": f["id"]})
        e = registry.find(registry.load_projects(), Path(r["path"]))
        self.assertEqual(e["folder_id"], f["id"])

    def test_tags(self):
        hub.change_project(self.pid, {"tags": [" draft ", "Draft", "with  Anna", ""]})
        self.assertEqual(self.entry()["tags"], ["draft", "with Anna"])
        hub.change_tag({"name": "draft", "color": "#2e8540"})
        with self.assertRaises(ValueError):
            hub.change_tag({"name": "draft", "color": "red;"})
        hub.change_tag({"name": "draft", "new_name": "with Anna"})      # merges
        self.assertEqual(self.entry()["tags"], ["with Anna"])
        self.assertEqual(registry.load_projects()["tags"], {"with Anna": {"color": "#2e8540"}})
        hub.remove_tag("with Anna")
        self.assertNotIn("tags", self.entry())
        self.assertEqual(hub.list_projects()["tags"], {})


@unittest.skipUnless(hub.shutil.which("git"), "needs git")
class FolderSync(TempState):
    """A folder of projects: one shared repository, or one each, and back."""

    def setUp(self):
        super().setUp()
        ident = {"GIT_AUTHOR_NAME": "T", "GIT_COMMITTER_NAME": "T",
                 "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_EMAIL": "t@example.com"}
        for k, v in ident.items():
            self.addCleanup(lambda k=k, old=os.environ.get(k): os.environ.pop(k, None) if old is None
                            else os.environ.__setitem__(k, old))
            os.environ[k] = v
        self.projects = self.tmp / "projects"
        for n in ("ex 1", "ex 1 sol", "exam"):
            (self.projects / n / "build").mkdir(parents=True)
            (self.projects / n / "main.tex").write_text(f"{n}\n", encoding="utf-8")
            (self.projects / n / "build" / "main.pdf").write_bytes(b"%PDF")
        exam = self.projects / "exam"
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "exam: first"]):
            subprocess.run(["git", *args], cwd=exam, check=True, capture_output=True)
        self.ids = {n: hub.add_project(str(self.projects / n))["id"] for n in ("ex 1", "ex 1 sol", "exam")}
        self.top = hub.create_folder({"name": "Course"})
        self.ex = hub.create_folder({"name": "练习", "parent": self.top["id"]})
        self.sol = hub.create_folder({"name": "Answers: all", "parent": self.top["id"]})
        hub.change_project(self.ids["ex 1"], {"folder_id": self.ex["id"]})
        hub.change_project(self.ids["exam"], {"folder_id": self.ex["id"]})
        hub.change_project(self.ids["ex 1 sol"], {"folder_id": self.sol["id"]})

    def git(self, cwd, *args):
        return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=cwd, capture_output=True,
                              text=True, encoding="utf-8").stdout

    def test_shared_then_separate(self):
        plan = hub.sync_plan(self.top["id"], "shared")
        repo = Path(plan["repo"])
        self.assertEqual(repo, (self.projects / "Course").resolve())
        self.assertEqual({r["name"]: r["action"] for r in plan["projects"]},
                         {"ex 1": "moves", "exam": "moves", "ex 1 sol": "moves"})
        r = hub.apply_sync({"id": self.top["id"], "mode": "shared"})
        self.assertTrue(r["ok"], r["report"])
        # Subfolders become directories (a name made safe for disk), build output stays out,
        # and exam's own history comes along.
        files =self.git(repo, "ls-files").splitlines()
        self.assertEqual(sorted(files), sorted([".gitattributes", ".gitignore", "prism-repo.json", "Answers- all/ex 1 sol/main.tex",
                                                "练习/ex 1/main.tex", "练习/exam/main.tex"]))
        self.assertIn("exam: first", self.git(repo, "log", "--format=%s"))
        self.assertTrue((repo / "练习" / "exam" / hub.SPLIT_BACKUP).is_dir())
        listed = {p["name"]: p for p in hub.list_projects()["projects"]}
        self.assertEqual(Path(listed["ex 1"]["path"]), repo / "练习" / "ex 1")
        self.assertEqual(listed["ex 1"]["tags"], [])
        self.assertEqual(hub.git_info(repo / "练习" / "ex 1")["shared"], "Course")
        self.assertEqual(hub.sync_plan(self.ex["id"])["inherited"]["id"], self.top["id"])
        # A new project in the folder belongs to the shared repository.
        new = hub.create_project({"name": "ex 2", "parent": str(repo / "练习"), "template": "empty",
                                  "folder_id": self.ex["id"], "git": True})
        self.assertEqual(hub.repo_kind(Path(new["path"]))[0], "shared")

        r = hub.apply_sync({"id": self.top["id"], "mode": "separate"})
        self.assertTrue(r["ok"], r["report"])
        for p in hub.list_projects()["projects"]:
            root = Path(p["path"])
            self.assertEqual(hub.repo_kind(root)[0], "own", p["name"])
            self.assertNotIn(".git-prism", self.git(root, "status", "--porcelain"))
        self.assertIn("in one repository", self.git(repo / "练习" / "ex 1", "log", "--format=%s"))
        self.assertTrue((repo / hub.SHARED_BACKUP).is_dir(), "the shared .git is set aside, not deleted")
        self.assertFalse((repo / "prism-repo.json").exists())

    def test_rename_project_and_folder(self):
        old = self.projects / "ex 1"
        r = hub.rename_project(self.ids["ex 1"], "Exercise: one", folder=True)
        new = self.projects / "Exercise- one"
        self.assertEqual((Path(r["path"]), r["moved"]), (new, True))
        self.assertFalse(old.exists())
        p = {x["name"]: x for x in hub.list_projects()["projects"]}["Exercise: one"]
        self.assertEqual((Path(p["path"]), p["folder_id"]), (new, self.ex["id"]), "folder in the list kept")
        with self.assertRaises(FileExistsError):
            hub.rename_project(r["id"], "exam", folder=True)
        r2 = hub.rename_project(r["id"], "Just a label", folder=False)
        self.assertFalse(r2["moved"])
        self.assertTrue(new.is_dir())

    def test_rename_inside_a_shared_repository_is_committed(self):
        self.assertTrue(hub.apply_sync({"id": self.top["id"], "mode": "shared"})["ok"])
        repo = self.projects / "Course"
        pid = {p["name"]: p["id"] for p in hub.list_projects()["projects"]}["ex 1"]
        hub.rename_project(pid, "ex one", folder=True)
        self.assertEqual(self.git(repo, "log", "-1", "--format=%s").strip(), "Rename ex 1 to ex one")
        self.assertEqual(self.git(repo, "status", "--porcelain"), "", "nothing left over")
        self.assertIn("练习/ex one/main.tex", self.git(repo, "ls-files"))

    def test_a_rename_waits_for_git_and_says_when_it_could_not_commit(self):
        """Another editor of the shared repository is committing (git's index.lock, for a
        moment): the rename waits its turn and is committed. When it cannot be, the answer
        says so: no other editor would commit it (each commits its own project only)."""
        import threading
        import repository
        self.assertTrue(hub.apply_sync({"id": self.top["id"], "mode": "shared"})["ok"])
        repo = self.projects / "Course"
        pid = {p["name"]: p["id"] for p in hub.list_projects()["projects"]}["ex 1"]
        lock = repo / ".git" / "index.lock"
        lock.write_bytes(b"")
        threading.Timer(1.0, lock.unlink).start()
        r = hub.rename_project(pid, "ex one", folder=True)
        self.assertIsNone(r["warning"])
        self.assertEqual(self.git(repo, "log", "-1", "--format=%s").strip(), "Rename ex 1 to ex one")
        lock.write_bytes(b"")                  # and this time it stays
        try:
            with mock.patch.object(repository, "LOCK_WAIT", 0.3):
                r = hub.rename_project(r["id"], "ex uno", folder=True)
        finally:
            lock.unlink()
        self.assertTrue(r["moved"])
        self.assertIn("could not be committed", r["warning"] or "")

    def github(self):
        """GitHub as a stand-in; real git pushes into local bare repositories."""
        from unittest import mock
        import github
        import gitenv
        from fake_github import FakeGitHub
        gh = FakeGitHub(remotes=self.tmp / "github")
        for p in (mock.patch.object(github, "GITHUB", gh),
                  mock.patch.object(gitenv, "git", gh.git)):
            p.start()
            self.addCleanup(p.stop)
        return gh

    def test_publish_one_project(self):
        root = self.projects / "ex 1"
        (root / "conversations").mkdir()
        (root / "conversations" / "chat.json").write_text("{}", encoding="utf-8")
        info = publish.publish_info(root)
        self.assertEqual((info["kind"], info["name"], info["github"]), ("none", "ex-1", None))
        self.assertEqual([d["dir"] for d in info["private_dirs"]], ["conversations"])
        gh = self.github()
        r = publish.publish_project(root, "my-ex", leave_out=["conversations"])
        self.assertEqual((r["url"], r["created"]), ("https://github.com/me/my-ex", True), r)
        self.assertEqual((gh.created, gh.protected), (["me/my-ex"], [("me/my-ex", "main")]))
        self.assertEqual(self.git(root, "log", "--format=%s").strip(), "Initial commit")
        self.assertNotIn("conversations/chat.json", self.git(root, "ls-files"))
        bound = hub.project_sync(root).settings.read()["sync_target"]
        self.assertEqual(bound["repository_identity"], "github.com/me/my-ex")
        self.assertEqual(hub.repo_kind(root)[0], "own")
        self.assertEqual(self.git(gh.repositories["me/my-ex"], "rev-parse", "main"),
                         self.git(root, "rev-parse", "HEAD"))
        self.assertEqual(self.git(gh.repositories["me/my-ex"], "show", "main:main.tex"), "ex 1\n")
        ignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("conversations/", ignore)
        self.assertIn("build/", ignore)
        self.assertTrue(publish.publish_info(root)["private_dirs"][0]["ignored"])

    def test_publish_in_a_shared_repository_publishes_that_repository(self):
        self.assertTrue(hub.apply_sync({"id": self.top["id"], "mode": "shared"})["ok"])
        repo = (self.projects / "Course").resolve()
        root = repo / "练习" / "ex 1"
        self.assertEqual(publish.publish_info(root)["kind"], "shared")
        gh = self.github()
        r = publish.publish_project(root)
        self.assertEqual((r["url"], gh.created), ("https://github.com/me/Course", ["me/Course"]), r)
        self.assertIn("github.com/me/Course", self.git(repo, "remote", "get-url", "origin"))
        self.assertEqual(self.git(gh.repositories["me/Course"], "rev-parse", "main"),
                         self.git(repo, "rev-parse", "HEAD"))

    def test_big_files_stay_out_of_the_first_commit(self):
        from unittest import mock
        root = self.projects / "ex 1"
        hub.git_init(root)
        (root / "huge.bin").write_bytes(b"x" * 4096)
        repo = hub.Repository.find(root)
        with mock.patch.object(publish.sources, "MAX_FILE", 1024), repo.changing():
            repo.ok("add", "-A")
            self.assertEqual(publish.unstage_big(repo), ["huge.bin"])
        self.assertNotIn("huge.bin", self.git(root, "diff", "--cached", "--name-only"))

    def test_separate_creates_missing_repositories(self):
        r = hub.apply_sync({"id": self.top["id"], "mode": "separate"})
        self.assertTrue(r["ok"], r["report"])
        kinds = {p["name"]: hub.repo_kind(Path(p["path"]))[0] for p in hub.list_projects()["projects"]}
        self.assertEqual(kinds, {"ex 1": "own", "ex 1 sol": "own", "exam": "own"})
        self.assertIn("exam: first", self.git(self.projects / "exam", "log", "--format=%s"))

    def test_refuses_a_repository_inside_a_project(self):
        with self.assertRaises(ValueError):
            hub.apply_sync({"id": self.top["id"], "mode": "shared", "repo": str(self.projects / "exam" / "all")})
        self.assertTrue((self.projects / "exam" / "main.tex").exists())

    def test_slow_editor_blocks_both_home_rename_and_repository_restructure(self):
        root = self.projects / "ex 1"
        registry.write_json(instances.path(root), {"pid": os.getpid(), "url": "http://127.0.0.1:9/"})
        with mock.patch.object(instances, "_probe", return_value=(None, False)):
            with self.assertRaises(instances.Busy):
                hub.rename_project(self.ids["ex 1"], "new name", folder=True)
            with self.assertRaises(ValueError):
                hub.apply_sync({"id": self.top["id"], "mode": "shared"})
        self.assertTrue(root.exists())
        self.assertFalse((self.projects / "Course").exists())

    def test_restructure_reserves_old_and_new_paths_through_metadata_update(self):
        real_move = registry.move_folder
        def checked(old, new, **kw):
            with mock.patch.object(instances, "_spawn") as spawn:
                for root in (old, new):
                    self.assertEqual(instances.inspect(root).state, "busy")
                    self.assertIn("error", instances.ensure(root, timeout=0.2))
                spawn.assert_not_called()
            return real_move(old, new, **kw)
        with mock.patch.object(registry, "move_folder", side_effect=checked):
            result = hub.apply_sync({"id": self.top["id"], "mode": "shared"})
        self.assertTrue(result["ok"], result)

    def test_two_home_mutations_cannot_use_the_same_preview(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        entered, release = threading.Event(), threading.Event()
        real_apply = hub._apply_shared
        def paused(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return real_apply(*args)
        body = {"id": self.top["id"], "mode": "shared"}
        with mock.patch.object(hub, "_apply_shared", side_effect=paused), ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(hub.apply_sync, body)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ValueError):
                    hub.apply_sync(body)
            finally:
                release.set()
            self.assertTrue(first.result()["ok"])


@unittest.skipUnless(hub.shutil.which("git"), "needs git")
class CheckGitHub(TempState):
    """When the Home page starts, every repository is fetched: new commits show on the cards."""

    def run_git(self, cwd, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                       check=True, capture_output=True)

    def test_new_commits_elsewhere_are_found(self):
        from unittest import mock
        remote, mine, other = self.tmp / "remote.git", self.tmp / "mine", self.tmp / "other"
        self.run_git(self.tmp, "init", "-q", "--bare", "-b", "main", str(remote))
        self.run_git(self.tmp, "clone", "-q", str(remote), str(mine))
        (mine / "main.tex").write_text("a\n", encoding="utf-8")
        self.run_git(mine, "add", "-A")
        self.run_git(mine, "commit", "-qm", "first")
        self.run_git(mine, "push", "-q", "-u", "origin", "main")
        self.run_git(self.tmp, "clone", "-q", str(remote), str(other))
        (other / "main.tex").write_text("b\n", encoding="utf-8")
        self.run_git(other, "commit", "-qam", "elsewhere")
        self.run_git(other, "push", "-q")
        hub.project_sync(mine).bind_target()
        hub.add_project(str(mine))
        # github_of: a local remote counts as the project's remote here.
        with mock.patch.object(hub, "github_of", return_value="https://github.com/me/mine"):
            with mock.patch.object(hub, "is_running", return_value=True):
                self.assertEqual(hub.fetch_targets(), [], "an open editor fetches by itself")
            hub.fetch_all()
        self.assertEqual((hub.FETCH["total"], hub.FETCH["done"], hub.FETCH["errors"]), (1, 1, {}))
        g = hub.git_info(mine)
        self.assertEqual((g["behind"], g["fetch_error"]), (1, None))
        self.assertEqual((mine / "main.tex").read_text(encoding="utf-8"), "a\n", "a fetch changes no file")

    def test_the_fetch_is_running_while_its_targets_are_worked_out(self):
        """A page that asks then must keep watching, or it never shows what was found."""
        from unittest import mock
        seen = []
        with mock.patch.object(hub, "fetch_targets", side_effect=lambda: seen.append(hub.FETCH["running"]) or []):
            hub.fetch_all()
        self.assertEqual(seen, [True])
        self.assertFalse(hub.FETCH["running"])

    def test_errors_read_while_fetching_never_change_under_the_reader(self):
        """The cards read the fetch errors (git_info, from other threads) while the fetches
        run: a dict a reader is going through must not change size under it."""
        from unittest import mock
        readers = []

        def fetch_one(top):
            readers.append(iter(hub.FETCH["errors"].items()))   # a card's request, meanwhile
            return "could not reach GitHub"

        with mock.patch.object(hub, "fetch_targets", return_value=[self.tmp / "a", self.tmp / "b"]), \
                mock.patch.object(hub, "fetch_one", fetch_one):
            hub.fetch_all()
        for it in readers:
            list(it)                       # RuntimeError: dictionary changed size during iteration
        self.assertEqual(len(hub.FETCH["errors"]), 2)


class SettingsAndGitHub(TempState):
    def test_concurrent_settings_updates_keep_both_changes(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        start = threading.Barrier(2)

        def save(changes):
            start.wait(5)
            return settings.save(changes)

        with ThreadPoolExecutor(max_workers=2) as pool:
            saved = [pool.submit(save, changes) for changes in (
                {"github_owner": "my-org"}, {"claude_account": "me@uni.example"})]
            for future in saved:
                future.result(timeout=15)
        current = settings.load()
        self.assertEqual(current["github_owner"], "my-org")
        self.assertEqual(current["claude_account"], "me@uni.example")

    def test_settings_round_trip_and_checks(self):
        self.assertEqual(settings.load(), settings.DEFAULTS)
        st = settings.save({"default_parent": str(self.tmp), "github_repo": True,
                                "github_owner": "my-org"})
        self.assertEqual((st["github_repo"], st["github_owner"]), (True, "my-org"))
        self.assertEqual(settings.load(), st)
        self.assertEqual(Path(hub.default_parent({"projects": []})), self.tmp.resolve())
        with self.assertRaises(ValueError):
            settings.save({"github_owner": "not an owner!"})
        with self.assertRaises(ValueError):
            settings.save({"default_parent": str(self.tmp / "missing")})
        self.assertEqual(settings.save({"claude_account": " me@uni.example "})["claude_account"],
                         "me@uni.example")
        with self.assertRaises(ValueError):
            settings.save({"claude_account": "not an email"})

    def test_claude_profile_folder(self):
        import backend_claude
        prof = self.tmp / "profile"
        prof.mkdir()
        with self.assertRaises(ValueError):
            settings.save({"claude_config_dir": str(self.tmp / "missing")})
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        self.addCleanup(lambda: os.environ.pop("CLAUDE_CONFIG_DIR", None) if old is None
                        else os.environ.__setitem__("CLAUDE_CONFIG_DIR", old))
        self.assertEqual(settings.save({"claude_config_dir": f' "{prof}" '})["claude_config_dir"],
                         str(prof.resolve()))
        backend_claude.use_config_dir()
        self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], str(prof.resolve()))
        settings.save({"claude_config_dir": ""})          # back to the environment as started
        backend_claude.use_config_dir()
        self.assertEqual(os.environ.get("CLAUDE_CONFIG_DIR", ""), backend_claude._STARTED_CONFIG_DIR)

    def test_repo_names(self):
        self.assertEqual(publish.repo_name("dyn num paper"), "dyn-num-paper")
        self.assertEqual(publish.repo_name("我的论文"), "latex-project")
        self.assertEqual(publish.repo_name("我的论文 v2"), "v2")

    def test_project_inside_another_repository_has_none_of_its_own(self):
        g = hub.git_info(PROJECT)                  # examples/minimal, inside this repository
        if g is None:
            self.skipTest("not a git checkout")
        self.assertTrue(g["nested"])

    def test_github_refuses_bad_names_before_calling_github(self):
        import github
        from fake_github import FakeGitHub
        gh = FakeGitHub()
        with mock.patch.object(github, "GITHUB", gh):
            self.assertIn("error", publish.create_github_repo(self.tmp, name="bad name!"))
            self.assertIn("error", publish.create_github_repo(self.tmp, owner="bad owner!"))
        self.assertEqual(gh.created, [])
        with mock.patch.object(github, "GITHUB", FakeGitHub(logged_in=False)):
            self.assertIn("not logged in", publish.create_github_repo(self.tmp)["error"])


class HubServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.ready = self.tmp / "ready.json"
        self.proc = subprocess.Popen(
            [sys.executable, str(HUB), "--port", "0", "--no-browser", "--exit-when-idle",
             "--idle-timings", "30,1,30", "--ready-file", str(self.ready)],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
            env={**os.environ, "PRISM_STATE_DIR": str(self.tmp / "state")})
        self.addCleanup(stop, self.proc)
        self.url = wait_file(self.ready)["url"]

    def post(self, path, body):
        return request(self.url, path, json.dumps(body).encode(), HEADERS)

    def test_manage_projects(self):
        self.assertEqual(request(self.url, "/api/ping")[1]["app"], "prism-home")
        status, r = self.post("/api/projects/add", {"path": str(PROJECT)})
        self.assertEqual(status, 200)
        pid = r["id"]
        self.post("/api/projects/add", {"path": str(PROJECT)})           # no duplicate
        _, listed = request(self.url, "/api/projects")
        self.assertEqual(len(listed["projects"]), 1)
        p = listed["projects"][0]
        self.assertEqual((p["main"], p["exists"], p["running"]), ("main.tex", True, None))

        self.assertEqual(self.post("/api/projects/update",
                                   {"id": pid, "pinned": True, "name": "Example"})[0], 200)
        p = request(self.url, "/api/projects")[1]["projects"][0]
        self.assertEqual((p["name"], p["pinned"]), ("Example", True))

        status, folder = self.post("/api/folders/create", {"name": "Topic"})
        self.assertEqual(status, 200)
        self.post("/api/projects/update", {"id": pid, "folder_id": folder["id"], "tags": ["draft"]})
        self.assertEqual(self.post("/api/tags/update", {"name": "draft", "color": "#c0392b"})[0], 200)
        listed = request(self.url, "/api/projects")[1]
        self.assertEqual((listed["projects"][0]["folder_id"], listed["projects"][0]["tags"]),
                         (folder["id"], ["draft"]))
        self.assertEqual(listed["tags"], {"draft": {"color": "#c0392b"}})
        self.assertEqual(self.post("/api/folders/update", {"id": "nope", "name": "x"})[0], 400)
        self.assertEqual(self.post("/api/folders/remove", {"id": folder["id"]})[0], 200)
        self.assertIsNone(request(self.url, "/api/projects")[1]["projects"][0]["folder_id"])

        self.assertEqual(self.post("/api/projects/add", {"path": str(self.tmp / "nope")})[0], 400)
        self.assertEqual(self.post("/api/projects/remove", {"id": "unknown"})[0], 404)
        self.assertEqual(self.post("/api/projects/remove", {"id": pid})[0], 200)
        self.assertEqual(request(self.url, "/api/projects")[1]["projects"], [])
        self.assertTrue(PROJECT.is_dir(), "removing from the list must not touch the folder")

    def test_requires_header_and_exits_after_last_page(self):
        status, _ = request(self.url, "/api/projects/add", json.dumps({"path": str(PROJECT)}).encode(),
                            {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        beat(self.url, "home-page-0001")
        self.assertEqual(bye(self.url, "home-page-0001"), (200, {"ok": True}))
        self.assertEqual(self.proc.wait(10), 0)
        self.assertFalse(self.ready.exists())


if __name__ == "__main__":
    unittest.main()
