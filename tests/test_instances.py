"""Server identity and lifecycle at the instances module's interface."""
import json
import io
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import instances
import registry
from tmpdirs import tmpdir


class InstanceState(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir()
        self.root = self.base / "paper"
        self.root.mkdir()
        env = mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.base / "state")})
        env.start()
        self.addCleanup(env.stop)

    def record(self, **fields):
        info = {"pid": os.getpid(), "started": instances.process_started(os.getpid()),
                "url": "http://127.0.0.1:9/", **fields}
        registry.write_json(instances.path(self.root), info)
        return info

    def test_inspection_of_an_absent_instance_creates_no_state_files(self):
        self.assertEqual(instances.inspect(self.root).state, "absent")
        self.assertFalse((self.base / "state").exists())

    def test_slow_live_instance_is_occupied_and_its_record_is_unchanged(self):
        info = self.record()
        with mock.patch.object(instances, "_probe", return_value=(None, False)):
            state = instances.inspect(self.root)
        self.assertEqual(state.state, "unresponsive")
        self.assertTrue(state.occupied)
        self.assertEqual(registry.read_json(instances.path(self.root)), info)

    def test_dead_instance_is_absent_but_inspection_does_not_delete_its_record(self):
        info = self.record()
        with mock.patch.object(instances, "server_alive", return_value=False):
            self.assertFalse(instances.inspect(self.root).occupied)
        self.assertEqual(registry.read_json(instances.path(self.root)), info)

    def test_wrong_generation_is_not_reused(self):
        self.record(token="current")
        with mock.patch.object(instances, "_probe", return_value=({"pid": os.getpid(), "token": "old"}, False)):
            self.assertEqual(instances.inspect(self.root).state, "unresponsive")

    def test_wrong_application_root_or_pid_is_not_reused(self):
        self.record()
        live = {"app": "prism-local", "root": str(self.root), "pid": os.getpid()}
        for wrong in ({"app": "other"}, {"root": str(self.base)}, {"pid": os.getpid()+1}):
            with self.subTest(wrong=wrong), mock.patch.object(instances.HTTP, "open",
                    return_value=io.BytesIO(json.dumps(live | wrong).encode())):
                self.assertEqual(instances.inspect(self.root).state, "unresponsive")

    def test_unresponsive_process_is_neither_replaced_nor_killed(self):
        info = self.record()
        with mock.patch.object(instances, "_probe", return_value=(None, False)), \
                mock.patch.object(instances, "_spawn") as spawn, \
                mock.patch.object(instances, "kill_tree") as kill:
            self.assertIn("error", instances.ensure(self.root, timeout=0.5))
        spawn.assert_not_called()
        kill.assert_not_called()
        self.assertEqual(registry.read_json(instances.path(self.root)), info)

    def test_waiting_for_the_os_lock_obeys_the_startup_time_budget(self):
        begin = time.monotonic()
        with registry.FileLock(instances.path(self.root).with_suffix(".lock")), \
                mock.patch.object(instances, "_spawn") as spawn:
            self.assertIn("error", instances.ensure(self.root, timeout=0.3))
        self.assertLess(time.monotonic()-begin, 1.5)
        spawn.assert_not_called()

    def test_successor_claim_blocks_takeover_even_between_the_two_os_lock_owners(self):
        registry.write_json(instances._op_path(self.root), {"pid": os.getpid(),
                            "started": instances.process_started(os.getpid()),
                            "token": "successor", "operation": "restarting"})
        with mock.patch.object(instances, "_spawn") as spawn:
            self.assertIn("error", instances.ensure(self.root, timeout=0.2))
            with self.assertRaises(instances.Busy):
                with instances.exclusive([self.root]):
                    self.fail("a claimed project was moved")
            with self.assertRaises(instances.Busy):
                with instances.serving(self.root):
                    self.fail("a claimed project was started twice")
        spawn.assert_not_called()

    def test_slow_live_instance_cannot_be_moved(self):
        self.record()
        with mock.patch.object(instances, "_probe", return_value=(None, False)):
            with self.assertRaises(instances.Busy):
                with instances.exclusive([self.root]):
                    self.fail("a slow editor was considered safe to move")

    def test_move_reserves_source_and_destination_and_open_does_not_spawn(self):
        target = self.base / "renamed"
        with instances.exclusive([self.root], targets=[target]), \
                mock.patch.object(instances, "_spawn") as spawn:
            for project in (self.root, target):
                self.assertEqual(instances.inspect(project).state, "busy")
                self.assertIn("moved", instances.ensure(project, timeout=0.2)["error"])
            spawn.assert_not_called()
        self.assertEqual(instances.inspect(self.root).state, "absent")

    def test_failed_exclusive_operation_releases_every_lock(self):
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with instances.exclusive([self.root]):
                raise RuntimeError("fixture")
        with instances.exclusive([self.root]):
            pass

    def test_invalid_record_is_occupied_and_never_overwritten_by_open(self):
        file = instances.path(self.root)
        registry.write_json(file, [])
        with mock.patch.object(instances, "_spawn") as spawn:
            result = instances.ensure(self.root, timeout=0.5)
        self.assertIn("error", result)
        self.assertEqual(registry.read_json(file), [])
        spawn.assert_not_called()

    def test_open_does_not_recreate_a_moved_or_missing_project(self):
        target = self.base / "gone"
        self.assertIn("folder not found", instances.ensure(target, timeout=0.5)["error"])
        self.assertFalse(target.exists())


class Registration(unittest.TestCase):
    def setUp(self):
        InstanceState.setUp(self)

    def test_custom_alias_and_canonical_record_are_both_published_and_removed(self):
        alias = self.base / "custom.json"
        with instances.serving(self.root, alias) as life:
            life.publish("http://127.0.0.1:9/", 9)
            info = registry.read_json(alias)
            self.assertEqual(registry.read_json(instances.path(self.root)), info)
            self.assertEqual(info["started"], instances.process_started(os.getpid()))
        self.assertFalse(alias.exists())
        self.assertFalse(instances.path(self.root).exists())

    def test_old_cleanup_cannot_remove_a_successors_registration(self):
        with instances.serving(self.root) as life:
            life.publish("first", 9)
            newer = {**registry.read_json(instances.path(self.root)), "token": "successor"}
            registry.write_json(instances.path(self.root), newer)
            life.unpublish()
            self.assertEqual(registry.read_json(instances.path(self.root)), newer)

    def test_locked_ready_record_does_not_prevent_shutdown_and_releasing_locks(self):
        with instances.serving(self.root) as life:
            life.publish("old", 9)
            transition = life.transition()
            with mock.patch.object(instances, "_discard", side_effect=PermissionError("fixture reader")):
                life.unpublish()
            transition.close()
        self.assertFalse(instances._op_path(self.root).exists())

    def test_shutdown_cannot_accept_another_transition_after_handover_is_released(self):
        with instances.serving(self.root) as life:
            life.publish("old", 9)
            transition = life.transition()
            life.unpublish()
            transition.close()
            with self.assertRaises(instances.Busy):
                life.transition()

    def test_cancelling_a_rename_before_shutdown_allows_retry(self):
        with instances.serving(self.root) as life:
            life.publish("old", 9)
            transition = life.transition(self.base / "new")
            transition.close()
            life.transition().close()

    def test_ready_record_waits_for_a_reader_on_windows(self):
        alias = self.base / "ready.json"
        with instances.serving(self.root, alias) as life:
            life.publish("first", 9)
            reader = open(alias, "rb")
            timer = threading.Timer(0.3, reader.close)
            timer.start()
            try:
                life.publish("second", 10)
            finally:
                reader.close()
                timer.join()
            self.assertEqual(registry.read_json(alias)["url"], "second")
            self.assertEqual(list(alias.parent.glob("ready.json.*")), [])

    def test_failed_handover_reports_failure_and_releases_both_paths(self):
        target = self.base / "renamed"
        with instances.serving(self.root) as life:
            life.publish("old", 9)
            transition = life.transition(target)
            life.unpublish()
            with mock.patch.object(instances, "_spawn", side_effect=OSError("fixture failure")):
                result = transition.handover(["unused"], self.base)
            self.assertIn("fixture failure", result["error"])
            transition.close()
        with instances.exclusive([self.root], targets=[target]):
            pass


class Startup(unittest.TestCase):
    def setUp(self):
        InstanceState.setUp(self)
        package = self.base / "stub"
        package.mkdir()
        source = Path(__file__).with_name("fake_instance.py")
        for name in ("server.py", "hub.py"):
            shutil.copyfile(source, package / name)
        for patch in (mock.patch.object(instances, "PKG", package),
                      mock.patch.dict(os.environ, {"PYTHONPATH": str(source.parents[1] / "prism_local")})):
            patch.start()
            self.addCleanup(patch.stop)
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for info in self.children:
            try:
                request = urllib.request.Request(info["url"] + "stop", data=b"{}")
                instances.HTTP.open(request, timeout=2).close()
            except OSError:
                pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and instances.server_alive(info["pid"], info.get("started")):
                time.sleep(0.05)
            if instances.server_alive(info["pid"], info.get("started")):
                os.kill(info["pid"], signal.SIGTERM)

    def start(self, project=..., extra=()):
        project = self.root if project is ... else project
        result = instances.ensure(project, port=0, extra=extra, timeout=8)
        self.assertNotIn("error", result, result)
        state = instances.inspect(project)
        self.assertEqual(state.state, "ready")
        self.children.append(state.info)
        return result, state.info

    def test_ensure_starts_and_reuses_without_an_unowned_popen(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            result, info = self.start()
            self.assertTrue(result["started"])
            again = instances.ensure(self.root)
            self.assertEqual(again, {"url": info["url"], "started": False})
        self.assertEqual([w for w in caught if w.category is ResourceWarning], [])

    def test_two_concurrent_open_requests_start_one_server(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: instances.ensure(self.root, port=0, timeout=8), range(2)))
        self.assertTrue(all("url" in result for result in results), results)
        self.children.append(instances.inspect(self.root).info)
        self.assertEqual(len({r["url"] for r in results}), 1)
        self.assertEqual(sum(r["started"] for r in results), 1)

    def test_redirector_pid_can_differ_from_the_ready_server_pid(self):
        result, info = self.start(extra=["--fixture-redirect"])
        self.assertTrue(result["started"])
        self.assertTrue(instances.server_alive(info["pid"], info.get("started")))

    def test_failed_start_reports_its_log_and_releases_the_reservation(self):
        result = instances.ensure(self.root, port=0, extra=["--fixture-exit"], timeout=2)
        self.assertIn("error", result)
        self.assertTrue(Path(result["logfile"]).exists())
        self.assertEqual(instances.inspect(self.root).state, "absent")
        self.start()

    def test_home_uses_the_same_startup_protocol(self):
        result, info = self.start(None)
        self.assertEqual(info["app"], "prism-home")
        self.assertEqual(instances.ensure(None)["url"], result["url"])

    def test_healthy_legacy_record_without_a_token_is_reused(self):
        _, info = self.start()
        info.pop("token")
        registry.write_json(instances.path(self.root), info)
        self.assertFalse(instances.ensure(self.root)["started"])

    def test_startup_timeout_kills_and_reaps_the_child_then_allows_retry(self):
        begin = time.monotonic()
        result = instances.ensure(self.root, port=0, extra=["--fixture-hang"], timeout=1)
        self.assertIn("error", result)
        info = json.loads(Path(result["logfile"]).read_text(encoding="utf-8").strip())
        self.assertFalse(instances.server_alive(info["hung_pid"], info["started"]))
        self.assertLess(time.monotonic() - begin, 8)
        self.start()

    def test_separate_launcher_processes_share_one_startup_lock(self):
        script = ("import instances,json,sys; from pathlib import Path; "
                  "instances.PKG=Path(sys.argv[1]); "
                  "print(json.dumps(instances.ensure(Path(sys.argv[2]),port=0,timeout=8)))")
        children = []
        try:
            for _ in range(3):
                children.append(subprocess.Popen([sys.executable, "-c", script, str(instances.PKG), str(self.root)],
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            results = []
            for child in children:
                output, error = child.communicate(timeout=12)
                self.assertEqual(child.returncode, 0, error)
                results.append(json.loads(output))
            self.children.append(instances.inspect(self.root).info)
            self.assertTrue(all("url" in result for result in results), results)
            self.assertEqual(len({r["url"] for r in results}), 1)
            self.assertEqual(sum(r["started"] for r in results), 1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    @unittest.skipUnless(os.name == "nt", "Windows venv redirector")
    def test_real_windows_venv_startup_uses_the_servers_identity(self):
        import venv
        environment = self.base / "venv"
        venv.EnvBuilder(with_pip=False).create(environment)
        with mock.patch.object(instances, "_python", return_value=str(environment / "Scripts/python.exe")):
            self.start()


if __name__ == "__main__":
    unittest.main()
