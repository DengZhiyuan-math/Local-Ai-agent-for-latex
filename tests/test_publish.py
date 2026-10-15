"""Publishing through its own module, using real git and offline GitHub transport."""
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import github
import gitenv
import gitsync
import publish
import settings
from fake_github import FakeGitHub
from repository import RepoBusy
from tmpdirs import tmpdir


@unittest.skipUnless(shutil.which("git"), "needs git")
class Publish(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir()
        self.root = self.base / "paper"
        self.root.mkdir()
        (self.root / "main.tex").write_text("original\n", encoding="utf-8")
        env = mock.patch.dict(os.environ, {
            "PRISM_STATE_DIR": str(self.base / "state"),
            "GIT_AUTHOR_NAME": "Fixture", "GIT_COMMITTER_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_EMAIL": "test@example.invalid"})
        env.start()
        self.addCleanup(env.stop)
        self.gh = FakeGitHub(remotes=self.base / "github")
        for patch in (mock.patch.object(github, "GITHUB", self.gh),
                      mock.patch.object(gitenv, "git", self.gh.git)):
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, root, *args):
        return gitenv.git(root, *args, check=True).stdout.strip()

    def assert_created_failure(self, result):
        self.assertIn("error", result)
        self.assertEqual(result["url"], "https://github.com/me/paper")
        self.assertTrue(result["created"])
        self.assertEqual(self.gh.created, ["me/paper"])
        self.assertEqual(self.gh.protected, [])
        self.assertEqual((self.root / "main.tex").read_text(), "original\n")
        self.assertEqual(self.git(self.root, "show", "HEAD:main.tex"), "original")
        self.assertEqual(self.git(self.gh.repositories["me/paper"], "for-each-ref"), "")

    def assert_unbound(self):
        path = self.root / ".git/prism-local.json"
        self.assertNotIn("sync_target", json.loads(path.read_text()) if path.exists() else {})

    def test_default_owner_is_used_and_a_second_publish_does_not_create_again(self):
        settings.save({"github_owner": "my-org"})
        result = publish.publish_project(self.root)
        self.assertNotIn("error", result)
        self.assertEqual(result["url"], "https://github.com/my-org/paper")
        remote = self.gh.repositories["my-org/paper"]
        self.assertEqual(self.git(remote, "rev-parse", "main"), self.git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(self.git(remote, "show", "main:main.tex"), "original")
        self.assertEqual(self.gh.protected, [("my-org/paper", "main")])
        self.assertEqual(publish.publish_project(self.root), {"url": result["url"], "created": False})
        self.assertEqual(self.gh.created, ["my-org/paper"])

    def test_first_push_and_protection_use_the_current_branch(self):
        self.git(self.root, "init", "-q", "-b", "draft")
        result = publish.publish_project(self.root)
        self.assertNotIn("error", result)
        self.assertEqual(self.gh.protected, [("me/paper", "draft")])
        remote = self.gh.repositories["me/paper"]
        self.assertEqual(self.git(remote, "for-each-ref", "--format=%(refname)"), "refs/heads/draft")
        self.assertEqual(self.git(remote, "rev-parse", "draft"), self.git(self.root, "rev-parse", "HEAD"))

    def test_creation_failure_keeps_the_local_commit(self):
        self.gh.fail = "fixture offline"
        result = publish.publish_project(self.root)
        self.assertEqual(result, {"error": "fixture offline", "created": False})
        self.assertEqual(self.git(self.root, "show", "HEAD:main.tex"), "original")
        self.assertEqual(self.git(self.root, "remote"), "")
        self.assertEqual(self.gh.protected, [])

    def test_rejected_first_push_keeps_the_commit_and_created_repository_link(self):
        create = self.gh.create

        def reject(root, full):
            error = create(root, full)
            hook = self.gh.repositories["me/paper"] / "hooks/pre-receive"
            hook.write_text("#!/bin/sh\necho 'fixture rejects push' >&2\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)
            return error

        with mock.patch.object(self.gh, "create", reject):
            result = publish.publish_project(self.root)
        self.assert_created_failure(result)

    def test_wrong_created_target_reports_the_link_without_binding_or_pushing(self):
        create = self.gh.create

        def redirected(root, full):
            error = create(root, full)
            self.git(root, "remote", "set-url", "origin", "https://github.com/me/other.git")
            return error

        with mock.patch.object(self.gh, "create", redirected):
            result = publish.publish_project(self.root)
        self.assert_created_failure(result)
        self.assertIn("differs from the effective", result["error"])
        self.assert_unbound()

    def test_wrong_remote_branch_reports_the_link_without_binding_or_pushing(self):
        create = self.gh.create

        def redirected(root, full):
            error = create(root, full)
            self.git(root, "config", "branch.main.remote", "origin")
            self.git(root, "config", "branch.main.merge", "refs/heads/other")
            return error

        with mock.patch.object(self.gh, "create", redirected):
            result = publish.publish_project(self.root)
        self.assert_created_failure(result)
        self.assertIn("differs from the current branch", result["error"])
        self.assert_unbound()

    def test_busy_first_push_reports_the_created_repository(self):
        with mock.patch.object(gitsync.GitSync, "push", side_effect=RepoBusy("fixture git is busy")):
            result = publish.publish_project(self.root)
        self.assert_created_failure(result)
        self.assertIn("git is busy", result["error"])

    def test_large_files_are_left_local_and_build_output_is_ignored(self):
        (self.root / "huge.bin").write_bytes(b"x" * 4096)
        (self.root / "build").mkdir()
        (self.root / "build/main.pdf").write_bytes(b"%PDF")
        with mock.patch.object(publish.sources, "MAX_FILE", 1024):
            result = publish.publish_project(self.root)
        self.assertNotIn("error", result)
        files = self.git(self.gh.repositories["me/paper"], "ls-tree", "-r", "--name-only", "main").splitlines()
        self.assertIn("main.tex", files)
        self.assertNotIn("huge.bin", files)
        self.assertNotIn("build/main.pdf", files)
        self.assertEqual((self.root / "huge.bin").stat().st_size, 4096)

    def test_nested_project_is_rejected_without_creating_its_own_repository(self):
        self.git(self.base, "init", "-q", "-b", "main")
        result = publish.publish_project(self.root)
        self.assertIn("inside another repository", result["error"])
        self.assertFalse((self.root / ".git").exists())
        self.assertEqual(self.gh.created, [])


@unittest.skipUnless(shutil.which("git"), "needs git")
class ProjectSync(unittest.TestCase):
    def test_the_build_output_is_where_prism_json_says(self):
        """Home and publishing sync a project whose editor may not be open: its build
        output, never committed, is in the folder its prism.json names."""
        root = tmpdir()
        (root / "main.tex").write_text("\\documentclass{article}", encoding="utf-8")
        (root / "prism.json").write_text('{"outdir": "out"}', encoding="utf-8")
        (root / "out").mkdir()
        (root / "out" / "main.pdf").write_bytes(b"%PDF")
        gitenv.git(root, "init", "-q", check=True)
        self.assertEqual(publish.project_sync(root).changes(), ["main.tex", "prism.json"])


if __name__ == "__main__":
    unittest.main()
