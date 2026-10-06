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
        self.assertEqual(sorted(files), sorted([".gitignore", "prism-repo.json", "Answers- all/ex 1 sol/main.tex",
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

    def test_publish_one_project(self):
        from unittest import mock
        root = self.projects / "ex 1"
        (root / "conversations").mkdir()
        (root / "conversations" / "chat.json").write_text("{}", encoding="utf-8")
        info = hub.publish_info(root)
        self.assertEqual((info["kind"], info["name"], info["github"]), ("none", "ex-1", None))
        self.assertEqual([d["dir"] for d in info["private_dirs"]], ["conversations"])
        calls = []
        with mock.patch.object(hub, "create_github_repo",
                               side_effect=lambda r, o, n: calls.append((r, o, n)) or {"url": "https://github.com/me/" + n}):
            r = hub.publish_project(root, "my-ex", leave_out=["conversations"])
        self.assertEqual(r, {"url": "https://github.com/me/my-ex", "created": True})
        self.assertEqual(calls, [(root, "", "my-ex")])
        self.assertEqual(hub.repo_kind(root)[0], "own")
        ignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("conversations/", ignore)
        self.assertIn("build/", ignore)
        self.assertTrue(hub.publish_info(root)["private_dirs"][0]["ignored"])

    def test_publish_in_a_shared_repository_publishes_that_repository(self):
        from unittest import mock
        self.assertTrue(hub.apply_sync({"id": self.top["id"], "mode": "shared"})["ok"])
        repo = (self.projects / "Course").resolve()
        root = repo / "练习" / "ex 1"
        self.assertEqual(hub.publish_info(root)["kind"], "shared")
        with mock.patch.object(hub, "create_github_repo",
                               side_effect=lambda r, o, n: {"url": f"https://github.com/me/{n}", "seen": str(r)}):
            r = hub.publish_project(root)
        self.assertEqual((r["seen"], r["url"]), (str(repo), "https://github.com/me/Course"))

    def test_big_files_stay_out_of_the_first_commit(self):
        from unittest import mock
        root = self.projects / "ex 1"
        hub.git_init(root)
        (root / "huge.bin").write_bytes(b"x" * 64)
        with mock.patch.object(hub.gitsync, "MAX_FILE", 32):
            hub.run_git(root, "add", "-A")
            self.assertEqual(hub._unstage_big(root), ["huge.bin"])
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

    def test_claude_profile_folder(self):
        import backend_claude
        prof = self.tmp / "profile"
        prof.mkdir()
        with self.assertRaises(ValueError):
            hub.save_settings({"claude_config_dir": str(self.tmp / "missing")})
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        self.addCleanup(lambda: os.environ.pop("CLAUDE_CONFIG_DIR", None) if old is None
                        else os.environ.__setitem__("CLAUDE_CONFIG_DIR", old))
        self.assertEqual(hub.save_settings({"claude_config_dir": f' "{prof}" '})["claude_config_dir"],
                         str(prof.resolve()))
        backend_claude.use_config_dir()
        self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], str(prof.resolve()))
        hub.save_settings({"claude_config_dir": ""})          # back to the environment as started
        backend_claude.use_config_dir()
        self.assertEqual(os.environ.get("CLAUDE_CONFIG_DIR", ""), backend_claude._STARTED_CONFIG_DIR)

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
