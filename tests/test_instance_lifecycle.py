"""Real Home/editor processes across restart, custom readiness files and folder moves."""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from test_lifecycle import NO_WINDOW, PROJECT, REPO, request, stop, wait_file
from tmpdirs import tmpdir
import instances
import registry

HEADERS = {"Content-Type": "application/json", "X-Prism-Local": "1"}


class Handover(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir()
        self.root = self.base / "paper"
        shutil.copytree(PROJECT, self.root)
        self.state = self.base / "state"
        self.known = []
        patch = mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.stop_servers)

    def stop_servers(self):
        infos = self.known + [registry.read_json(file) or {} for file in (self.state / "instances").glob("*.json")]
        for info in infos:
            pid = info.get("pid")
            if pid != os.getpid() and instances.server_alive(pid, info.get("started")):
                if os.name == "nt":
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                                   timeout=8, creationflags=NO_WINDOW)
                else:
                    os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 3
                while instances.server_alive(pid, info.get("started")) and time.monotonic() < deadline:
                    time.sleep(0.05)

    def start_direct(self, alias, cwd=None, python=None):
        proc = subprocess.Popen([python or sys.executable, str(REPO / "prism_local/server.py"), str(self.root),
                                 "--port", "0", "--no-browser", "--exit-when-idle",
                                 "--idle-timings", "60,1,60", "--ready-file", str(alias)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, creationflags=NO_WINDOW, cwd=cwd)
        self.addCleanup(stop, proc)
        info = wait_file(alias)
        self.known.append(info)
        return info

    def wait_replacement(self, project, previous):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = instances.inspect(project, timeout=0.3)
            if state.state == "ready" and state.info["token"] != previous["token"]:
                self.known.append(state.info)
                self.assertNotEqual(state.info["pid"], previous["pid"])
                self.assertEqual(state.info["url"], previous["url"], "browser origin must survive restart")
                return state.info
            time.sleep(0.1)
        self.fail(f"no successor for {previous}")

    def test_home_restarts_on_the_bound_port_and_concurrent_open_reuses_it(self):
        result = instances.ensure(None, port=0, extra=["--idle-timings", "60,1,60"])
        self.assertNotIn("error", result, result)
        info = instances.inspect(None).info
        for _ in range(3):
            self.assertEqual(request(info["url"], "/api/restart", b"{}", HEADERS)[0], 200)
            with ThreadPoolExecutor(max_workers=3) as pool:
                opened = list(pool.map(lambda _: instances.ensure(None, timeout=15), range(3)))
            self.assertEqual(opened, [{"url": info["url"], "started": False}] * 3)
            info = self.wait_replacement(None, info)

    def test_custom_alias_survives_rename_then_another_restart(self):
        alias = self.base / "custom-ready.json"
        info = self.start_direct(alias)
        self.assertEqual(registry.read_json(instances.path(self.root)), info)
        self.assertEqual(request(info["url"], "/api/restart", b"{}", HEADERS)[0], 200)
        info = self.wait_replacement(self.root, info)
        self.assertEqual(registry.read_json(alias), registry.read_json(instances.path(self.root)))
        new = self.base / "Renamed paper"
        self.assertEqual(request(info["url"], "/api/project/rename", b'{"name":"Renamed paper"}', HEADERS)[0], 200)
        with ThreadPoolExecutor(max_workers=2) as pool:
            old_open = pool.submit(instances.ensure, self.root, timeout=15)
            new_open = pool.submit(instances.ensure, new, timeout=15)
            self.assertIn("error", old_open.result())
            self.assertEqual(new_open.result(), {"url": info["url"], "started": False})
        info = self.wait_replacement(new, info)
        self.assertEqual(Path(registry.read_json(alias)["root"]), new)
        self.assertFalse(self.root.exists())
        self.assertEqual(request(info["url"], "/api/restart", b"{}", HEADERS)[0], 200)
        self.wait_replacement(new, info)
        self.assertTrue((new / "main.tex").exists())
        self.assertFalse(self.root.exists(), "the old --moved-from option must not be replayed")

    def test_alias_inside_a_moved_project_follows_the_folder(self):
        alias = self.root / "ready.json"
        info = self.start_direct(alias)
        new = self.base / "New paper"
        self.assertEqual(request(info["url"], "/api/project/rename", b'{"name":"New paper"}', HEADERS)[0], 200)
        self.wait_replacement(new, info)
        self.assertTrue((new / "ready.json").is_file())
        self.assertFalse(self.root.exists(), "publishing an old alias must not recreate the old folder")

    def test_direct_cli_registers_the_editor_and_refuses_a_duplicate(self):
        info = self.start_direct(self.base / "direct.json")
        result = instances.ensure(self.root)
        self.assertEqual(result, {"url": info["url"], "started": False})
        duplicate = subprocess.run([sys.executable, str(REPO / "prism_local/server.py"), str(self.root),
                                    "--port", "0", "--no-browser"], capture_output=True, timeout=8)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn(b"already open", duplicate.stderr)
        self.assertEqual(registry.read_json(instances.path(self.root)), info)

    def test_editor_started_in_its_project_folder_releases_cwd_before_renaming(self):
        info = self.start_direct(self.base / "cwd-ready.json", cwd=self.root)
        new = self.base / "New paper"
        self.assertEqual(request(info["url"], "/api/project/rename", b'{"name":"New paper"}', HEADERS)[0], 200)
        self.wait_replacement(new, info)
        self.assertTrue((new / "main.tex").exists())
        self.assertFalse(self.root.exists())

    @unittest.skipUnless(os.name == "nt", "Windows venv redirector holds the working directory")
    def test_venv_editor_releases_its_redirectors_cwd_before_renaming(self):
        import venv
        environment = self.base / "venv"
        venv.EnvBuilder(with_pip=False).create(environment)
        info = self.start_direct(self.base / "venv-ready.json", cwd=self.root,
                                 python=str(environment / "Scripts/python.exe"))
        new = self.base / "New paper"
        self.assertEqual(request(info["url"], "/api/project/rename", b'{"name":"New paper"}', HEADERS)[0], 200)
        self.wait_replacement(new, info)
        self.assertTrue((new / "main.tex").exists())
        self.assertFalse(self.root.exists())
