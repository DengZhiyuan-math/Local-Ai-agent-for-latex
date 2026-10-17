"""Sync target and environment regressions. Only local temporary remotes are used."""
import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends
import gitsync
import synctarget
import backend_deepcode
import backend_openai
from gitenv import git_env
from tmpdirs import tmpdir


def git(root, *args):
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return r.stdout.strip()


class Targets(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir()
        self.env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.base / "empty"),
                                              "GIT_CONFIG_NOSYSTEM": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = self.base / "paper"
        self.root.mkdir()
        self.remote = self.base / "paper.git"
        self.tool = self.base / "tool.git"
        for p in (self.remote, self.tool):
            git(self.base, "init", "-q", "--bare", "-b", "main", str(p))
        git(self.root, "init", "-q", "-b", "main")
        for k, v in (("user.name", "Fixture"), ("user.email", "test@example.invalid"),
                     ("commit.gpgsign", "false")):
            git(self.root, "config", k, v)
        (self.root / "main.tex").write_text("base\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        git(self.root, "remote", "add", "origin", str(self.remote))
        self.g = gitsync.GitSync(lambda: self.root, lambda: "build")

    def test_the_target_and_its_binding_without_a_sync_running(self):
        """What publishing and the Home page need: no editor's sync worker, only the
        repository's configuration and .git/prism-local.json (synctarget)."""
        from repository import Repository
        repo = Repository.find(self.root)
        settings = synctarget.RepoSettings(repo)
        target, blocked = settings.validate(fresh=True)
        self.assertIsNone(blocked)
        self.assertEqual((target["remote"], target["branch_ref"]), ("origin", "refs/heads/main"))
        self.assertEqual(settings.read()["sync_target"]["branch_ref"], "refs/heads/main", "bound at once")
        git(self.root, "remote", "set-url", "origin", str(self.tool))       # another repository
        target, blocked = settings.validate(fresh=True)
        self.assertIn("confirm the new target", blocked)
        self.assertEqual(settings.read()["sync_target"]["repository_identity"],
                         synctarget.repository_identity(str(self.remote), self.root), "kept until confirmed")
        settings.bind(target)
        self.assertEqual(settings.validate(fresh=True)[1], None)
        git(self.root, "checkout", "-q", "--detach")
        with self.assertRaisesRegex(ValueError, "Detached HEAD"):
            synctarget.resolve(repo, fresh=True)

    def test_unbound_project_binds_itself_and_pushes(self):
        self.g.push()
        self.assertIsNone(self.g.status()["blocked_reason"])
        self.assertEqual(self.g.settings.read()["sync_target"]["branch_ref"], "refs/heads/main")
        self.assertEqual(git(self.remote, "rev-parse", "main"), git(self.root, "rev-parse", "HEAD"))

    def test_status_binds_nothing_the_worker_does(self):
        """The page asks status() every two seconds: it shows the target an unbound project
        would bind, and writes nothing; the worker's first step binds it."""
        settings = self.root / ".git" / "prism-local.json"
        st = self.g.status()
        self.assertIsNone(st["blocked_reason"])
        self.assertEqual(st["target"]["branch_ref"], "refs/heads/main")
        self.assertIsNone(st["bound_target"])
        self.assertFalse(settings.exists(), "a look writes nothing")
        self.g._start()                                 # what the worker does first
        self.assertEqual(self.g.status()["bound_target"]["branch_ref"], "refs/heads/main")

    def test_status_runs_no_git_for_the_target_until_the_config_changes(self):
        self.g.bind_target()
        self.g.status()
        with mock.patch.object(gitsync.gitenv, "git", wraps=gitsync.gitenv.git) as run:
            for _ in range(3):
                self.assertIsNone(self.g.status()["blocked_reason"])
            run.assert_not_called()
            git(self.root, "remote", "set-url", "origin", str(self.tool))     # another repository
            st = self.g.status()
        self.assertIn("Sync target changed", st["blocked_reason"])
        self.assertTrue(run.called, "a changed config is looked at again at once")

    def test_working_the_target_out_takes_few_git_commands(self):
        """Renewed every CACHE_TTL seconds while the editor is idle: it took 12 commands,
        each reading the configuration again; one listing of it serves them now."""
        from repository import Repository
        repo = Repository(self.root.resolve(), (self.root / ".git").resolve())
        with mock.patch.object(gitsync.gitenv, "git", wraps=gitsync.gitenv.git) as run:
            synctarget._resolve(repo)
        self.assertLessEqual(run.call_count, 5, [c.args[1:] for c in run.call_args_list])

    def test_push_checks_the_target_afresh(self):
        self.g.bind_target()
        self.g.status()
        (self.root / "main.tex").write_text("mine\n")
        self.g.commit(["main.tex"], "edit")
        # A change the cache does not see (a system-wide config, say): the push still does.
        with mock.patch.object(synctarget, "config_key", return_value=("same",)):
            synctarget._CACHE.clear()
            self.g.status()
            git(self.root, "remote", "set-url", "origin", str(self.tool))
            self.assertIsNone(self.g.status()["blocked_reason"], "status may use what it knew")
            self.g.push()
        self.assertIn("Sync target changed", self.g.blocked_reason)
        self.assertNotEqual(git(self.root, "rev-parse", "HEAD"),
                            subprocess.run(["git", "rev-parse", "main"], cwd=self.tool,
                                           capture_output=True, text=True).stdout.strip())

    def test_status_does_not_wait_for_a_sync_in_progress(self):
        self.g.bind_target()
        self.g.status()
        held = __import__("threading").Event()
        release = __import__("threading").Event()

        def hold():
            with self.g.lock:
                held.set()
                release.wait(10)
        t = __import__("threading").Thread(target=hold)
        t.start()
        held.wait(5)
        try:
            start = __import__("time").monotonic()
            st = self.g.status()
            self.assertLess(__import__("time").monotonic() - start, 1.0)
            self.assertEqual(st["target"]["branch_ref"], "refs/heads/main", "what is known")
        finally:
            release.set()
            t.join()

    def test_same_repository_reached_another_way_rebinds_quietly(self):
        self.g.bind_target()
        git(self.root, "config", "branch.main.remote", "origin")
        git(self.root, "config", "branch.main.merge", "refs/heads/main")
        self.assertTrue(self.g.validate_target(), self.g.blocked_reason)
        self.assertEqual(self.g.settings.read()["sync_target"]["configuration_fingerprint"],
                         self.g.target["configuration_fingerprint"])

    def test_create_rejects_target_changed_before_binding(self):
        import github
        import publish
        from fake_github import FakeGitHub
        git(self.root, "remote", "remove", "origin")
        real_bind = self.g.bind_target

        def changed_before_binding(expected=None):
            git(self.root, "remote", "set-url", "origin", "https://github.com/owner/tool.git")
            real_bind(expected)

        gh = FakeGitHub(account="owner")
        with mock.patch.object(github, "GITHUB", gh), \
                mock.patch.object(publish, "project_sync", return_value=self.g), \
                mock.patch.object(self.g, "bind_target", side_effect=changed_before_binding), \
                mock.patch.object(self.g, "push") as push:
            result = publish.create_github_repo(self.root, "owner", "paper")
        self.assertIn("changed since confirmation", result.get("error", ""))
        self.assertEqual(result["url"], "https://github.com/owner/paper")
        self.assertEqual(gh.created, ["owner/paper"])
        push.assert_not_called()
        self.assertEqual(gh.protected, [], "no protection asked for")
        # Bound to the repository created: the redirected one is never bound by a look
        # (the Home page, the editor's sync), which pauses until it points there again.
        self.assertEqual(self.g.settings.read()["sync_target"]["repository_identity"], "github.com/owner/paper")
        self.assertFalse(self.g.validate_target(fresh=True))
        self.assertEqual(self.g.settings.read()["sync_target"]["repository_identity"], "github.com/owner/paper")
        git(self.root, "remote", "set-url", "origin", "https://github.com/owner/paper.git")
        self.assertTrue(self.g.validate_target(fresh=True), "pointed back: it goes on by itself")
        self.assertEqual((self.root / "main.tex").read_text(), "base\n")

    def test_publishing_commits_holding_the_files(self):
        """Publishing commits everything (git add -A): not while a save replaces a file (on
        Windows git cannot open it then), so it holds the editor's save lock it is given."""
        import gitenv
        import github
        import publish
        import threading
        from fake_github import FakeGitHub
        git(self.root, "remote", "remove", "origin")
        (self.root / "new.tex").write_text("new\n")
        held, lock = [], threading.RLock()
        real_git = gitenv.git

        def run_git(cwd, *args, **kw):
            if args and args[0] in ("add", "commit"):
                held.append(lock._is_owned())
            return real_git(cwd, *args, **kw)

        with mock.patch.object(github, "GITHUB", FakeGitHub(account="owner", fail="no network")), \
                mock.patch.object(gitenv, "git", run_git):
            r = publish.create_github_repo(self.root, "owner", "paper", files_lock=lock)
        self.assertIn("no network", r.get("error", ""))
        self.assertTrue(held)
        self.assertEqual(set(held), {True}, "git add and commit ran holding the files")

    def test_redirects_block_and_keep_local_edits(self):
        self.g.bind_target()
        git(self.root, "remote", "add", "tool", str(self.tool))
        for key, value in (("branch.main.pushRemote", "tool"),
                           ("remote.pushDefault", "tool"),
                           ("remote.origin.pushurl", str(self.tool)),
                           (f"url.{self.tool}.pushInsteadOf", str(self.remote)),
                           ("remote.origin.mirror", "true")):
            with self.subTest(key=key):
                git(self.root, "config", key, value)
                (self.root / "main.tex").write_text(key)
                self.g.now("commit")
                self.assertTrue(self.g.status()["blocked_reason"])
                self.assertEqual(git(self.tool, "for-each-ref"), "")
                self.assertEqual(git(self.root, "show", "HEAD:main.tex"), key)
                git(self.root, "config", "--unset-all", key)

    def test_a_target_changed_while_waiting_for_the_network_is_not_pushed_to(self):
        """A push checks its target, then waits for the network lock (another window's
        transfer: minutes on a slow network); git reads the configuration only when it
        starts. A change in between is seen, not pushed to."""
        import threading
        import repository
        self.g.bind_target()
        (self.root / "main.tex").write_text("mine\n")
        self.g.commit_all("Autosave")
        with repository.network_lock(self.root / ".git"):         # another window's transfer
            pushed = []
            t = threading.Thread(target=lambda: pushed.append(self.g.push()))
            t.start()
            for _ in range(200):                                   # it has checked, and waits
                if self.g.state == "pushing":
                    break
                threading.Event().wait(0.02)
            git(self.root, "config", "remote.origin.pushurl", str(self.tool))
        t.join(30)
        self.assertEqual(pushed[0].state, "local")
        self.assertIn("Sync paused", pushed[0].message)
        self.assertEqual(git(self.tool, "for-each-ref"), "", "nothing reached the other repository")
        self.assertEqual(git(self.remote, "for-each-ref"), "")
        git(self.root, "config", "--unset", "remote.origin.pushurl")
        self.assertEqual(self.g.push().state, "synced", "set back: it goes on")
        self.assertEqual(git(self.remote, "rev-parse", "main"), git(self.root, "rev-parse", "HEAD"))

    def test_a_repository_prism_set_up_is_put_back_and_syncs(self):
        """What test_redirects_block_and_keep_local_edits pauses for, in a repository you
        set up, Prism puts back in one it set up, and syncs: nobody has to know git. A
        look (status(), every two seconds) changes nothing; nor does another repository
        named as its address, which still waits."""
        self.g.bind_target()
        self.g.settings.update(managed=True)
        git(self.root, "remote", "add", "tool", str(self.tool))
        for n, (key, value) in enumerate((("branch.main.pushRemote", "tool"),
                                          ("remote.pushDefault", "tool"),
                                          ("remote.origin.mirror", "true"),
                                          ("branch.main.merge", "refs/heads/elsewhere"))):
            with self.subTest(key=key):
                git(self.root, "config", key, value)
                self.assertTrue(self.g.status()["blocked_reason"], "a look changes nothing")
                self.assertEqual(git(self.root, "config", "--get", key), value)
                (self.root / "main.tex").write_text(f"edit {n}\n")
                self.g.now("commit")
                self.assertIsNone(self.g.status()["blocked_reason"])
                self.assertEqual(git(self.remote, "rev-parse", "main"), git(self.root, "rev-parse", "HEAD"))
                self.assertIn("put back", self.g.notice)
        self.assertEqual(git(self.tool, "for-each-ref"), "", "nothing went elsewhere")
        git(self.root, "checkout", "-q", "--detach")                 # left at its branch's commit
        (self.root / "main.tex").write_text("detached\n")
        self.g.now("commit")
        self.assertEqual(git(self.root, "symbolic-ref", "HEAD"), "refs/heads/main")
        self.assertEqual(git(self.remote, "show", "main:main.tex"), "detached")
        git(self.root, "config", "remote.origin.pushurl", str(self.tool))  # another repository: waits
        (self.root / "main.tex").write_text("waits\n")
        self.g.now("commit")
        self.assertTrue(self.g.status()["blocked_reason"])
        self.assertEqual(git(self.tool, "for-each-ref"), "")
        self.assertEqual(git(self.root, "config", "remote.origin.pushurl"), str(self.tool), "its address is left")

    def test_a_github_remote_gone_is_added_back_by_its_address(self):
        from repository import Repository
        repo = Repository.find(self.root)
        git(self.root, "remote", "remove", "origin")
        done = synctarget.repair(repo, {"remote": "origin", "repository_identity": "github.com/me/paper",
                                        "branch_ref": "refs/heads/main", "local_branch": "refs/heads/main"})
        self.assertIn("its GitHub address github.com/me/paper", done)
        self.assertEqual(git(self.root, "remote", "get-url", "origin"), "https://github.com/me/paper.git")
        self.assertEqual(git(self.root, "config", "branch.main.merge"), "refs/heads/main")
        self.assertEqual(synctarget.repair(repo, {"remote": "origin", "repository_identity": "github.com/me/paper",
                                                  "branch_ref": "refs/heads/main",
                                                  "local_branch": "refs/heads/main"}), [], "nothing more to do")

    def test_branch_switch_and_multiple_push_urls(self):
        self.g.bind_target()
        git(self.root, "checkout", "-qb", "other")
        self.g.push()
        self.assertTrue(self.g.status()["blocked_reason"])
        git(self.root, "checkout", "-q", "main")
        git(self.root, "config", "--add", "remote.origin.pushurl", str(self.remote))
        git(self.root, "config", "--add", "remote.origin.pushurl", str(self.tool))
        self.g.push()
        self.assertTrue(self.g.status()["blocked_reason"])
        self.assertEqual(git(self.tool, "for-each-ref"), "")

    def test_binding_preserves_settings_and_upstream_branch(self):
        git(self.root, "config", "branch.main.remote", "origin")
        git(self.root, "config", "branch.main.merge", "refs/heads/paper")
        path = self.root / ".git/prism-local.json"
        path.write_text(json.dumps({"restore_rewritten": False, "sync": False}))
        self.g.bind_target()
        data = json.loads(path.read_text())
        self.assertFalse(data["restore_rewritten"])
        self.assertFalse(data["sync"])
        self.assertEqual(data["sync_target"]["branch_ref"], "refs/heads/paper")

    def test_rename_and_move_keep_bound_remote_and_branch(self):
        self.g.bind_target()
        destinations = [self.base / "renamed", self.base / "papers/moved"]
        for destination in destinations:
            with self.subTest(destination=destination.name):
                destination.parent.mkdir(exist_ok=True)
                self.root.rename(destination)
                self.root = destination
                self.assertTrue(self.g.validate_target(), self.g.blocked_reason)
                reopened = gitsync.GitSync(lambda: self.root, lambda: "build")
                self.assertTrue(reopened.validate_target(), reopened.blocked_reason)
                (self.root / "main.tex").write_text(destination.name)
                reopened.now("commit")
                self.assertEqual(git(self.remote, "show", "main:main.tex"), destination.name)
                git(self.root, "remote", "set-url", "origin", str(self.tool))
                self.assertFalse(reopened.validate_target())
                self.assertEqual(git(self.tool, "for-each-ref"), "")
                git(self.root, "remote", "set-url", "origin", str(self.remote))

    def test_an_agent_in_a_project_inside_another_repository_does_not_find_it(self):
        """A project in some other repository (prism-local's examples): nothing syncs it,
        and the agent's git does not reach that repository's remotes or history either. A
        project with a repository of its own finds that one, as before."""
        outer = self.base / "tool"
        nested = outer / "examples" / "paper"
        nested.mkdir(parents=True)
        (nested / "main.tex").write_text("x\n")
        git(outer, "init", "-q", "-b", "main")
        git(outer, "remote", "add", "origin", str(self.tool))

        def top(project, cwd):
            return subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True,
                                  text=True, env=backends.agent_env(root=project))
        self.assertNotEqual(top(nested, nested).returncode, 0, "the outer repository is not found")
        (nested / "sub").mkdir()
        self.assertNotEqual(top(nested, nested / "sub").returncode, 0, "nor from a folder of the project")
        self.assertEqual(Path(top(self.root, self.root).stdout.strip()).resolve(), self.root.resolve(),
                         "a project's own repository is found")
        git(nested, "init", "-q", "-b", "main")                    # it gets one of its own
        self.assertEqual(Path(top(nested, nested).stdout.strip()).resolve(), nested.resolve())

    def test_inherited_git_selectors_do_not_select_tool(self):
        other = self.base / "other"
        other.mkdir()
        git(other, "init", "-q")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                                          "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.url",
                                          "GIT_CONFIG_VALUE_0": str(self.tool)}):
            self.assertTrue(self.g.check_repo())
            self.assertEqual(self.g.top, self.root)
            env = backends.agent_env()
            self.assertNotIn("GIT_DIR", env)
            self.assertNotIn("GIT_CONFIG_KEY_0", env)

    def test_conflict_merge_ignores_foreign_git_environment(self):
        (self.root / "fig.png").write_bytes(b"\x89PNG\0base")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "figure")
        self.g.bind_target()
        self.g.push()
        coauthor = self.base / "coauthor"
        git(self.base, "clone", "-qb", "main", str(self.remote), str(coauthor))
        git(coauthor, "config", "user.name", "Coauthor")
        git(coauthor, "config", "user.email", "coauthor@example.invalid")
        git(coauthor, "config", "commit.gpgsign", "false")
        (coauthor / "main.tex").write_text("coauthor's text\n")
        (coauthor / "fig.png").write_bytes(b"\x89PNG\0coauthor")
        git(coauthor, "commit", "-qam", "coauthor")
        git(coauthor, "push", "-q", "origin", "main")
        (self.root / "main.tex").write_text("my text\n")
        (self.root / "fig.png").write_bytes(b"\x89PNG\0mine")
        git(self.root, "commit", "-qam", "mine")

        foreign = self.base / "foreign"
        foreign.mkdir()
        git(foreign, "init", "-q", "-b", "main")
        rows = []
        for path, data in (("main.tex", b"FOREIGN\n"), ("fig.png", b"\x89PNG\0FOREIGN")):
            (foreign / path).write_bytes(data)
            oid = git(foreign, "hash-object", "-w", path)
            rows.extend(f"100644 {oid} {stage}\t{path}\n" for stage in (1, 2, 3))
        subprocess.run(["git", "update-index", "--index-info"], cwd=foreign,
                       input="".join(rows), text=True, capture_output=True, check=True)
        with mock.patch.dict(os.environ, {
                "GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                "GIT_INDEX_FILE": str(foreign / ".git/index"),
                "GIT_OBJECT_DIRECTORY": str(foreign / ".git/objects"),
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.url",
                "GIT_CONFIG_VALUE_0": str(self.tool)}):
            self.g.pull()
        self.assertIsNone(self.g.error)
        text = (self.root / "main.tex").read_text()
        self.assertIn("my text", text)
        self.assertIn("coauthor's text", text)
        self.assertNotIn("FOREIGN", text)
        self.assertEqual((self.root / "fig.png").read_bytes(), b"\x89PNG\0mine")
        self.assertEqual((self.root / "fig.from-Coauthor.png").read_bytes(), b"\x89PNG\0coauthor")
        self.assertEqual(git(self.root, "ls-files", "-u"), "")
        self.assertEqual(git(self.tool, "for-each-ref"), "")

    def test_env_keeps_auth_transport_and_platform_variables(self):
        base = {"GIT_SSH_COMMAND": "ssh", "HTTPS_PROXY": "proxy", "PATH": "path",
                "GIT_CONFIG_PARAMETERS": "injection", "git_dir": "bad"}
        self.assertEqual(git_env(base), {"GIT_SSH_COMMAND": "ssh", "HTTPS_PROXY": "proxy",
                                         "PATH": "path", "GIT_TERMINAL_PROMPT": "0"})

    def test_worktree_binding_and_git_hub_target(self):
        import hub
        work = self.base / "worktree"
        git(self.root, "worktree", "add", "-qb", "work", str(work))
        sync = gitsync.GitSync(lambda: work, lambda: "build")
        self.assertTrue(sync.check_repo())
        hook = Path(git(work, "rev-parse", "--git-path", "hooks/pre-push"))
        if not hook.is_absolute():
            hook = work / hook
        self.assertIn(gitsync.GUARD_MARK, hook.read_text())
        sync.bind_target()
        sync.push()
        self.assertEqual(git(self.remote, "rev-parse", "work"), git(work, "rev-parse", "HEAD"))
        git(work, "config", "branch.work.pushRemote", "tool")
        self.assertIsNone(hub.github_of(work))
        self.assertIn("pushRemote", hub.fetch_one(work))

    def test_moved_worktree_keeps_binding(self):
        work = self.base / "worktree"
        git(self.root, "worktree", "add", "-qb", "work", str(work))
        sync = gitsync.GitSync(lambda: work, lambda: "build")
        sync.bind_target()
        moved = self.base / "moved-worktree"
        git(self.root, "worktree", "move", str(work), str(moved))
        work = moved
        self.assertTrue(sync.validate_target(), sync.blocked_reason)
        reopened = gitsync.GitSync(lambda: moved, lambda: "build")
        self.assertTrue(reopened.validate_target(), reopened.blocked_reason)
        (moved / "main.tex").write_text("moved worktree\n")
        reopened.now("commit")
        self.assertEqual(git(self.remote, "show", "work:main.tex"), "moved worktree")
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)"), "refs/heads/work")
        # Moving the owner repository also moves the worktree's actual git_dir.
        owner = self.base / "moved-owner"
        self.root.rename(owner)
        self.root = owner
        git(owner, "worktree", "repair", str(moved))
        self.assertFalse(reopened.validate_target(), "cached repository location still requires reopening")
        repaired = gitsync.GitSync(lambda: moved, lambda: "build")
        self.assertTrue(repaired.validate_target(), repaired.blocked_reason)
        repaired.push()
        self.assertIsNone(repaired.error)

    def test_detached_local_upstream_and_stale_confirmation(self):
        self.g.bind_target()
        expected = self.g.status()["target"]
        git(self.root, "checkout", "-q", "--detach")
        self.assertFalse(self.g.validate_target())
        git(self.root, "checkout", "-q", "main")
        git(self.root, "config", "branch.main.remote", ".")
        self.assertFalse(self.g.validate_target())
        git(self.root, "config", "--unset", "branch.main.remote")
        git(self.root, "checkout", "-qb", "new-branch")
        with self.assertRaisesRegex(ValueError, "changed since confirmation"):
            self.g.bind_target(expected)
        self.assertEqual(self.g.status()["bound_target"]["branch_ref"], "refs/heads/main")

    def test_push_only_bound_branch_even_with_tags_and_custom_refspec(self):
        git(self.root, "config", "remote.origin.push", "refs/heads/main:refs/heads/tool")
        git(self.root, "config", "push.followTags", "true")
        git(self.root, "tag", "-am", "fixture", "tagged")
        self.g.bind_target()
        self.g.push()
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)"), "refs/heads/main")

    def test_github_transport_identity_and_secret_redaction(self):
        expected = "github.com/owner/paper"
        for url in ("git@github.com:Owner/Paper.git", "https://token@github.com/Owner/Paper.git",
                    "ssh://git@github.com:22/Owner/Paper.git"):
            self.assertEqual(gitsync.repository_identity(url, self.root), expected)
        git(self.root, "remote", "set-url", "origin", "https://token@github.com/owner/paper.git")
        git(self.root, "config", "remote.origin.pushurl", "git@github.com:owner/paper.git")
        self.g.bind_target()
        self.assertNotIn("token", json.dumps(self.g.status()))
        self.assertNotIn("token", (self.root / ".git/prism-local.json").read_text())
        self.assertNotIn("token", gitsync.safe_git_message("failed: https://token@github.com/owner/paper.git?secret=yes"))

    def test_fetch_upstream_remote_branch_without_origin_assumption(self):
        git(self.root, "remote", "rename", "origin", "paper")
        git(self.root, "config", "branch.main.remote", "paper")
        git(self.root, "config", "branch.main.merge", "refs/heads/document")
        self.g.bind_target()
        self.g.push()
        clone = self.base / "coauthor"
        git(self.base, "clone", "-qb", "document", str(self.remote), str(clone))
        git(clone, "config", "user.name", "Fixture")
        git(clone, "config", "user.email", "test@example.invalid")
        git(clone, "config", "commit.gpgsign", "false")
        (clone / "main.tex").write_text("from coauthor\n")
        git(clone, "commit", "-qam", "coauthor")
        git(clone, "push", "-q", "origin", "HEAD:refs/heads/document")
        self.g.pull()
        self.assertEqual((self.root / "main.tex").read_text(), "from coauthor\n")


class SessionOwnership(unittest.TestCase):
    def test_deepcode_collision_allows_a_b_a_and_rejects_foreign_sessions(self):
        with tempfile.TemporaryDirectory(prefix="dc-", dir="/tmp" if Path("/tmp").is_dir() else None) as tmp:
            base = Path(tmp).resolve()
            a, b = base / "a-b/c", base / "a/b-c"
            a.mkdir(parents=True)
            b.mkdir(parents=True)
            if len(str(b)) > 64:
                self.skipTest("requires a short native project path")
            sid = "session-123"
            backend = backend_deepcode.DeepCode("deepcode")
            with mock.patch.object(Path, "home", return_value=base / "home"):
                directory = backend_deepcode.sessions_dir(a)
                self.assertEqual(directory, backend_deepcode.sessions_dir(b))
                directory.mkdir(parents=True)
                index = directory / "sessions-index.json"
                entries = [{"id": sid, "updateTime": 1000}]
                index.write_text(json.dumps({"originalPath": str(a), "entries": entries}))
                (directory / f"{sid}.jsonl").write_text(json.dumps({
                    "id": "native-message-id", "sessionId": sid, "role": "system", "content":
                    '# Local Workspace Environment\n\n```json\n' +
                    json.dumps({"root path": str(a), "pwd": str(a)}) + '\n```'}))
                self.assertIsNone(backend.check_session(a, sid))
                self.assertEqual(backend_deepcode.latest_session(a, 0), sid)
                self.assertTrue(backend.check_session(b, sid))
                self.assertIsNone(backend_deepcode.latest_session(b, 0))
                # The native CLI rewrites originalPath when a colliding project saves.
                sid_b = "session-b"
                entries.append({"id": sid_b, "updateTime": 2000})
                (directory / f"{sid_b}.jsonl").write_text(json.dumps({
                    "id": "native-message-b", "sessionId": sid_b, "role": "system", "content":
                    '# Local Workspace Environment\n\n```json\n' +
                    json.dumps({"root path": str(b), "pwd": str(b)}) + '\n```'}))
                index.write_text(json.dumps({"originalPath": str(b), "entries": entries}))
                self.assertIsNone(backend.check_session(b, sid_b))
                self.assertTrue(backend.check_session(b, sid))
                self.assertTrue(backend.check_session(a, sid_b))
                self.assertIsNone(backend.check_session(a, sid))
                # Returning to A rewrites the shared index again; B remains resumable.
                entries[0]["updateTime"] = 3000
                index.write_text(json.dumps({"originalPath": str(a), "entries": entries}))
                self.assertIsNone(backend.check_session(a, sid))
                self.assertIsNone(backend.check_session(b, sid_b))
                self.assertTrue(backend.check_session(b, sid))
                self.assertTrue(backend.check_session(a, sid_b))

    def test_native_cli_metadata_rejects_foreign_project(self):
        import backend_codex
        import backend_claude
        root, other, home = tmpdir(), tmpdir(), tmpdir()
        sid = "session-123"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            path = home / "sessions/2026/10/08" / f"rollout-fixture-{sid}.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(root)}}))
            backend = backend_codex.Codex("codex")
            self.assertIsNone(backend.check_session(root, sid))
            self.assertTrue(backend.check_session(other, sid))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), mock.patch.object(backend_claude, "use_config_dir"):
            code = __import__('re').sub(r"[^A-Za-z0-9]", "-", str(root))
            path = home / "projects" / code / f"{sid}.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"sessionId": sid, "cwd": str(other)}))
            backend = backend_claude.ClaudeCode("claude")
            self.assertTrue(backend.check_session(root, sid))
            path.write_text(json.dumps({"sessionId": sid, "cwd": str(root)}))
            self.assertIsNone(backend.check_session(root, sid))
    def test_deepcode_requires_local_index_and_file(self):
        root, other, home = tmpdir(), tmpdir(), tmpdir()
        sid = "session-123"
        backend = backend_deepcode.DeepCode("deepcode")
        with mock.patch.object(Path, "home", return_value=home):
            directory = backend_deepcode.sessions_dir(root)
            directory.mkdir(parents=True)
            (directory / "sessions-index.json").write_text(json.dumps({"entries": [{"id": sid}]}))
            self.assertTrue(backend.check_session(root, sid))
            (directory / f"{sid}.jsonl").write_text('{}\n')
            self.assertTrue(backend.check_session(root, sid))
            (directory / "sessions-index.json").write_text(json.dumps({
                "originalPath": str(root), "entries": [{"id": sid}]}))
            self.assertTrue(backend.check_session(root, sid))
            (directory / f"{sid}.jsonl").write_text(json.dumps({
                "id": "native-message-id", "sessionId": sid, "role": "system", "content":
                '# Local Workspace Environment\n\n```json\n' +
                json.dumps({"root path": str(root), "pwd": str(root)}) + '\n```'}))
            self.assertIsNone(backend.check_session(root, sid))
            self.assertTrue(backend.check_session(other, sid))
            self.assertTrue(backend.check_session(root, "../escape"))

    def test_deepcode_settings_cannot_reinject_git_environment(self):
        root, home = tmpdir(), tmpdir()
        backend = backend_deepcode.DeepCode("deepcode")
        with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture"}):
            for source in (home / ".deepcode/settings.json", root / ".deepcode/settings.json"):
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(json.dumps({"env": {"GIT_DIR": "secret-path"}}))
                reason = backend.preflight(root)
                self.assertIn("GIT_DIR", reason)
                self.assertIn(str(source), reason)
                self.assertNotIn("secret-path", reason)
                source.unlink()

    def test_api_session_is_bound_to_project(self):
        backend = backend_openai.OpenAICompat("fixture", {"base_url": "http://localhost:9", "model": "test", "api_key_env": ""})
        root, other = tmpdir(), tmpdir()
        backend.sessions["sid"] = []
        backend.session_projects["sid"] = __import__('registry').project_key(root)
        self.assertIsNone(backend.check_session(root, "sid"))
        self.assertTrue(backend.check_session(other, "sid"))
