"""Tests for the Home page server (prism_local/hub.py) and the shared project list."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/: tmpdirs, test_lifecycle
from tmpdirs import tmpdir  # noqa: E402
from test_lifecycle import NO_WINDOW, beat, bye, request, stop, wait_file  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
HUB = REPO / "prism_local" / "hub.py"
PROJECT = REPO / "examples" / "minimal"
sys.path.insert(0, str(REPO / "prism_local"))
import hub  # noqa: E402
import registry  # noqa: E402

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

    def test_project_key_is_stable(self):
        self.assertEqual(registry.project_key(PROJECT), registry.project_key(Path(str(PROJECT))))


class Instances(TempState):
    """An instance file goes only when its server has ended, not when it is slow."""

    def test_a_live_server_that_does_not_answer_keeps_its_file(self):
        inst = registry.instance_file("slow")
        registry.write_json(inst, {"pid": os.getpid(), "url": "http://127.0.0.1:9/"})
        self.assertIsNone(registry.running_instance(inst, timeout=0.3))
        self.assertTrue(inst.exists())

    def test_the_file_of_a_server_that_ended_is_removed(self):
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        inst = registry.instance_file("gone")
        registry.write_json(inst, {"pid": ended.pid, "url": "http://127.0.0.1:9/"})
        self.assertIsNone(registry.running_instance(inst, timeout=0.3))
        self.assertFalse(inst.exists())

    def test_server_alive(self):
        self.assertTrue(registry.server_alive(os.getpid()))
        self.assertFalse(registry.server_alive(None))
        self.assertFalse(registry.server_alive(-5))


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

    def test_git_info_of_new_repository(self):
        hub.create_project({"name": "g", "parent": str(self.tmp), "template": "empty", "git": True})
        g = hub.git_info(self.tmp / "g")
        if g is None:
            self.skipTest("git not available")
        self.assertIn(g["branch"], ("main", "master"))
        self.assertGreater(g["changes"], 0)


class SettingsAndGitHub(TempState):
    def test_settings_round_trip_and_checks(self):
        self.assertEqual(hub.load_settings(), hub.SETTINGS_DEFAULTS)
        st = hub.save_settings({"default_parent": str(self.tmp), "github_repo": True,
                                "github_owner": "my-org"})
        self.assertEqual((st["github_repo"], st["github_owner"]), (True, "my-org"))
        self.assertEqual(hub.load_settings(), st)
        self.assertEqual(Path(hub.default_parent({"projects": []})), self.tmp.resolve())
        with self.assertRaises(ValueError):
            hub.save_settings({"github_owner": "not an owner!"})
        with self.assertRaises(ValueError):
            hub.save_settings({"default_parent": str(self.tmp / "missing")})
        self.assertEqual(hub.save_settings({"claude_account": " me@uni.example "})["claude_account"],
                         "me@uni.example")
        with self.assertRaises(ValueError):
            hub.save_settings({"claude_account": "not an email"})

    def test_repo_names(self):
        self.assertEqual(hub.repo_name("dyn num paper"), "dyn-num-paper")
        self.assertEqual(hub.repo_name("我的论文"), "latex-project")
        self.assertEqual(hub.repo_name("我的论文 v2"), "v2")

    def test_project_inside_another_repository_has_none_of_its_own(self):
        g = hub.git_info(PROJECT)                  # examples/minimal, inside this repository
        if g is None:
            self.skipTest("not a git checkout")
        self.assertTrue(g["nested"])

    def test_github_refuses_bad_names_before_calling_github(self):
        hub._gh_cache.update(at=time.time(), status={"gh": "gh", "logged_in": True, "account": "me"})
        self.addCleanup(hub._gh_cache.clear)
        self.assertIn("error", hub.create_github_repo(self.tmp, name="bad name!"))
        self.assertIn("error", hub.create_github_repo(self.tmp, owner="bad owner!"))


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
