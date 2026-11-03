"""Explorer operations through Home's actual project-list refresh path."""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import tmpdir
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import diskprojects
import diskwatch
import hub
import registry
from test_lifecycle import NO_WINDOW, request, stop, wait_file


class FakeChanges:
    """Unit tests deliver notifications explicitly; the HTTP test uses the OS watcher."""
    def __init__(self, *args, **kwargs):
        self.generation = 0

    def stamp(self):
        return self.generation

    def changed(self):
        self.generation += 1

    def configure(self, roots):
        pass

    def close(self):
        pass


class ExplorerChanges(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.env = mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.tmp / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        diskprojects.reset()
        self.notifications = mock.patch.object(diskprojects.diskwatch, "Changes", FakeChanges)
        self.notifications.start()
        self.addCleanup(self.notifications.stop)
        self.addCleanup(diskprojects.reset)
        self.root = self.tmp / "papers"
        self.root.mkdir()
        self.folder = hub.create_folder({"name": "papers"})["id"]
        self.paper = self.make(self.root / "paper")
        self.pid = hub.add_project(str(self.paper), self.folder)["id"]

    def make(self, path):
        path.mkdir(parents=True)
        (path / "main.tex").write_text(r"\documentclass{article}\title{Example}", encoding="utf-8")
        return path

    def listed(self, changed=True):
        state = diskprojects._states.get(str(registry.state_dir()))
        if changed and state:
            state["events"].changed()
        return hub.list_projects()["projects"]

    def test_move_and_rename_preserves_metadata_and_follows_history_group(self):
        hub.change_project(self.pid, {"tags": ["keep"], "pinned": True, "name": "My paper"})
        self.listed()
        new = self.root / "history backup" / "renamed"
        new.parent.mkdir()
        self.paper.rename(new)
        projects = self.listed()
        self.assertEqual(len(projects), 1)
        project = projects[0]
        self.assertEqual(Path(project["path"]), new)
        self.assertTrue(project["exists"])
        self.assertEqual(project["tags"], ["keep"])
        self.assertTrue(project["pinned"])
        self.assertEqual(project["name"], "My paper")
        folder = next(f for f in registry.load_projects()["folders"] if f["id"] == project["folder_id"])
        self.assertEqual(folder["name"], "history backup")
        self.assertEqual(folder["parent"], self.folder)

    def test_new_projects_are_discovered_but_sources_and_outputs_are_not(self):
        self.listed()
        new = self.make(self.root / "new" / "second")
        self.make(self.root / "build" / "output")
        (self.paper / "sections").mkdir()
        (self.paper / "sections" / "intro.tex").write_text("section")
        paths = {Path(p["path"]) for p in self.listed()}
        self.assertEqual(paths, {self.paper, new})

    def test_delete_and_restore_keeps_the_record(self):
        self.listed()
        content = (self.paper / "main.tex").read_bytes()
        shutil.rmtree(self.paper)
        project = self.listed()[0]
        self.assertFalse(project["exists"])
        self.paper.mkdir()
        (self.paper / "main.tex").write_bytes(content)
        self.assertTrue(self.listed()[0]["exists"])
        renamed = self.root / "restored and renamed"
        self.paper.rename(renamed)
        self.assertEqual(Path(self.listed()[0]["path"]), renamed)

    def test_copy_is_distinct_and_does_not_steal_a_missing_identity(self):
        self.listed()
        copy = self.root / "copy"
        shutil.copytree(self.paper, copy)
        shutil.rmtree(self.paper)
        projects = self.listed()
        old = next(p for p in projects if Path(p["path"]) == self.paper)
        self.assertFalse(old["exists"])
        self.assertTrue(next(p for p in projects if Path(p["path"]) == copy)["exists"])

    def test_manual_remove_is_not_rediscovered(self):
        self.listed()
        hub.remove_project(self.pid)
        self.assertEqual(self.listed(), [])
        hub.add_project(str(self.paper), self.folder)
        self.assertEqual(len(self.listed()), 1)

    def test_container_rename_and_restart_follow_persisted_identity(self):
        registry.update_projects(lambda d: next(f for f in d['folders'] if f['id'] == self.folder)
                                 .update(sync={"mode": "shared", "repo": str(self.root)}))
        self.listed()
        new = self.root.with_name("renamed papers")
        self.root.rename(new)
        diskprojects.reset()
        project = self.listed()[0]
        self.assertEqual(Path(project["path"]), new / "paper")
        folder = next(f for f in registry.load_projects()["folders"] if f["id"] == self.folder)
        self.assertEqual(Path(folder["disk_path"]), new)
        self.assertEqual(folder["name"], new.name)
        self.assertEqual(folder["sync"]["repo"], str(new))

    def test_unchanged_poll_does_not_rewrite_registry(self):
        self.listed()
        before = registry._registry_path().read_bytes()
        with mock.patch.object(registry, "write_state", wraps=registry.write_state) as write, \
                mock.patch.object(diskprojects, "_walk", wraps=diskprojects._walk) as walk:
            for _ in range(10):
                self.listed(changed=False)
            write.assert_not_called()
            walk.assert_not_called()
        self.assertEqual(registry._registry_path().read_bytes(), before)

    def test_missing_path_recovers_without_notification_and_does_not_repeat(self):
        self.listed()
        renamed = self.root / "renamed"
        self.paper.rename(renamed)
        self.assertEqual(Path(self.listed(changed=False)[0]["path"]), renamed)
        shutil.rmtree(renamed)
        self.assertFalse(self.listed(changed=False)[0]["exists"])
        with mock.patch.object(diskprojects, "_walk", wraps=diskprojects._walk) as walk:
            for _ in range(5):
                self.assertFalse(self.listed(changed=False)[0]["exists"])
            walk.assert_not_called()

    def test_manual_grouping_survives_polling(self):
        self.listed()
        manual = hub.create_folder({"name": "Selected papers"})["id"]
        hub.change_project(self.pid, {"folder_id": manual})
        self.assertEqual(self.listed()[0]["folder_id"], manual)

    def test_replacement_at_old_name_does_not_hide_a_move(self):
        self.listed()
        new = self.root / "renamed"
        self.paper.rename(new)
        self.make(self.paper)
        projects = self.listed()
        self.assertEqual({Path(p["path"]) for p in projects}, {self.paper, new})
        moved = next(p for p in projects if Path(p["path"]) == new)
        self.assertTrue(moved["exists"])

    def test_disk_bound_folder_moves_under_new_container(self):
        child = self.root / "chapter"
        paper = self.make(child / "other")
        fid = hub.create_folder({"name": "chapter", "parent": self.folder})["id"]
        hub.add_project(str(paper), fid)
        self.listed()
        archive = self.root / "archive"
        archive.mkdir()
        child.rename(archive / "chapter")
        self.listed()
        folders = registry.load_projects()["folders"]
        child_group = next(f for f in folders if f["id"] == fid)
        archive_group = next(f for f in folders if f["name"] == "archive")
        self.assertEqual(child_group["parent"], archive_group["id"])

    def test_directory_symlinks_are_not_discovered(self):
        outside = self.make(self.tmp / "outside")
        try:
            (self.root / "link").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks unavailable")
        self.assertEqual(len(self.listed()), 1)

    def test_container_without_direct_projects_discovers_new_subtree(self):
        child = self.root / "chapter"
        self.paper.rename(child)
        # A leaf group contains the existing paper; the outer group has no projects.
        project = self.make(child / "nested")
        registry.update_projects(lambda d: d["projects"].clear())
        fid = hub.create_folder({"name": "chapter", "parent": self.folder})["id"]
        hub.add_project(str(project), fid)
        self.listed()
        new = self.make(self.root / "new chapter" / "new project")
        self.assertIn(new, {Path(p["path"]) for p in self.listed()})

    @unittest.skipUnless(os.name == "nt" or sys.platform == "darwin",
                         "Real directory notifications require Windows or macOS.")
    def test_http_poll_follows_explorer_move_and_discovers_new_project(self):
        ready = self.tmp / "ready.json"
        server = subprocess.Popen(
            [sys.executable, str(Path(hub.__file__)), "--no-browser", "--port", "0",
             "--ready-file", str(ready)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.addCleanup(stop, server)
        url = wait_file(ready)["url"]
        self.assertEqual(len(request(url, "/api/projects")[1]["projects"]), 1)
        added = self.make(self.root / "new project")
        import time
        deadline = time.monotonic() + 5
        while True:
            response = request(url, "/api/projects")[1]
            if len(response["projects"]) == 2 or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        self.assertEqual(len(response["projects"]), 2, "new-only change needs a real OS notification")
        new = self.root / "history backup" / "renamed"
        new.parent.mkdir()
        # Both endpoints stay in this test's resolved temporary directory.
        self.assertTrue(new.is_relative_to(self.tmp))
        self.assertTrue(self.paper.is_relative_to(self.tmp))
        self.paper.rename(new)
        deadline = time.monotonic() + 10
        while True:
            code, response = request(url, "/api/projects")
            paths = {Path(p["path"]) for p in response["projects"]}
            if paths == {new, added} or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        self.assertEqual(code, 200)
        self.assertEqual(paths, {new, added})
        self.assertTrue(all(p["exists"] for p in response["projects"]))


@unittest.skipUnless(os.name == "nt" or sys.platform == "darwin", "Windows/macOS native notifications")
class NativeNotifications(unittest.TestCase):
    def test_waits_for_changes_filters_build_output_and_closes(self):
        import threading
        root = tmpdir()
        (root / "build").mkdir()
        event = threading.Event()
        factory = diskwatch.DirectoryWatch if os.name == "nt" else diskwatch.MacDirectoryWatch
        watch = factory(root, event.set)
        self.addCleanup(watch.close)
        self.assertFalse(event.wait(0.1), "idle watcher must not synthesize polling events")
        (root / "build" / "output.tex").write_text("build output")
        self.assertFalse(event.wait(0.1))
        (root / "main.tex").write_text("source")
        self.assertTrue(event.wait(2))
        watch.close()
        self.assertFalse(watch.is_alive())

    @unittest.skipUnless(os.name == 'nt', 'Windows I/O belongs to its issuing thread')
    def test_watcher_survives_the_creating_request_thread(self):
        import threading
        root, changed, watches = tmpdir(), threading.Event(), []
        creator = threading.Thread(target=lambda: watches.append(diskwatch.DirectoryWatch(root, changed.set)))
        creator.start()
        creator.join(timeout=3)
        self.assertFalse(creator.is_alive())
        self.assertEqual(len(watches), 1)
        self.addCleanup(watches[0].close)
        self.assertFalse(changed.wait(.1), 'creator exit must not cancel the notification request')
        (root / 'main.tex').write_text('source')
        self.assertTrue(changed.wait(2))


class MacEventContract(unittest.TestCase):
    def test_file_events_filter_outputs_and_overflow_forces_refresh(self):
        import ctypes as c
        from macdiskwatch import Watch
        watch = Watch.__new__(Watch)
        watch.root = Path('/papers')
        watch.excluded = ()
        watch.accept = diskwatch.relevant
        watch.changed = mock.Mock()
        names = (c.c_char_p * 1)(b'/papers/build/output.tex')
        flags = (c.c_uint32 * 1)(0x1000)
        watch._events(None, None, 1, c.cast(names, c.c_void_p), flags, None)
        watch.changed.assert_not_called()
        names[0] = b'/papers/new/main.tex'
        watch._events(None, None, 1, c.cast(names, c.c_void_p), flags, None)
        watch.changed.assert_called_once()
        watch.changed.reset_mock()
        flags[0] = 2
        names[0] = b'/papers/build/output.tex'
        watch._events(None, None, 1, c.cast(names, c.c_void_p), flags, None)
        watch.changed.assert_called_once()

    def test_darwin_selects_fsevents_adapter(self):
        changes = diskwatch.Changes()
        fake = mock.Mock()
        fake.is_alive.return_value = True
        root = Path('/papers')
        with mock.patch.object(diskwatch.os, 'name', 'posix'), \
                mock.patch.object(diskwatch.sys, 'platform', 'darwin'), \
                mock.patch.object(diskwatch, 'MacDirectoryWatch', return_value=fake) as factory:
            changes.configure([root])
            factory.assert_called_once()
        changes.close()
        fake.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
