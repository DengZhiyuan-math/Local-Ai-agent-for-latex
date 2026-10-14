"""The production GitHub adapter, with only the external gh process replaced."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import github


def result(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["gh"], code, stdout, stderr)


class GitHubCLI(unittest.TestCase):
    def setUp(self):
        self.gh = github.GhCli()
        patch = mock.patch.object(github.shutil, "which", return_value="fixture-gh")
        patch.start()
        self.addCleanup(patch.stop)

    def test_login_is_cached_and_can_be_refreshed(self):
        with mock.patch.object(github.subprocess, "run", side_effect=[
                result(stderr="Logged in to github.com account owner (keyring)"),
                result(1, stderr="not logged in")]) as run:
            self.assertEqual(self.gh.status()["account"], "owner")
            self.assertTrue(self.gh.status()["logged_in"])
            self.assertEqual(run.call_count, 1)
            self.assertFalse(self.gh.status(refresh=True)["logged_in"])
            self.assertEqual(run.call_count, 2)

    def test_login_cache_expires(self):
        with mock.patch.object(github.time, "time", return_value=100) as clock, \
                mock.patch.object(github.subprocess, "run", side_effect=[
                    result(stdout="Logged in to github.com as owner"),
                    result(stdout="Logged in to github.com as other")]) as run:
            self.assertEqual(self.gh.status()["account"], "owner")
            clock.return_value += github.STATUS_TTL + 1
            self.assertEqual(self.gh.status()["account"], "other")
            self.assertEqual(run.call_count, 2)

    def test_missing_cli_is_reported_without_starting_a_process(self):
        with mock.patch.object(github.shutil, "which", return_value=None), \
                mock.patch.object(github.subprocess, "run") as run:
            self.assertIn("not found", self.gh.status()["error"])
            self.assertIn("not found", self.gh.create(Path("."), "owner/paper"))
        run.assert_not_called()

    def test_create_uses_the_project_and_sanitizes_inherited_git_selectors(self):
        with mock.patch.object(self.gh, "status", return_value={"gh": "fixture-gh"}), \
                mock.patch.dict(os.environ, {"GIT_DIR": "foreign", "GIT_WORK_TREE": "foreign",
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.url",
                    "GIT_CONFIG_VALUE_0": "foreign"}), \
                mock.patch.object(github.subprocess, "run", return_value=result()) as run:
            root = Path("paper")
            self.assertIsNone(self.gh.create(root, "owner/paper"))
        args, options = run.call_args
        self.assertEqual(args[0], ["fixture-gh", "repo", "create", "owner/paper", "--private",
                                   "--source", ".", "--remote", "origin"])
        self.assertEqual(options["cwd"], root)
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
            self.assertNotIn(name, options["env"])
        self.assertEqual(options["env"]["GH_PROMPT_DISABLED"], "1")

    def test_create_reports_cli_failure_and_timeout(self):
        with mock.patch.object(self.gh, "status", return_value={"gh": "fixture-gh"}), \
                mock.patch.object(github.subprocess, "run", side_effect=[
                    result(1, stderr="no network"), subprocess.TimeoutExpired("gh", 180)]):
            self.assertEqual(self.gh.create(Path("."), "owner/paper"), "no network")
            self.assertIn("timed out", self.gh.create(Path("."), "owner/paper"))

    def test_branch_protection_and_unavailable_plan(self):
        with mock.patch.object(self.gh, "status", return_value={"gh": "fixture-gh"}), \
                mock.patch.object(github.subprocess, "run", side_effect=[
                    result(), result(1, stderr="Upgrade to GitHub Pro")]) as run:
            self.assertTrue(self.gh.protect("owner/paper", "main").startswith("on:"))
            args, options = run.call_args
            self.assertIn("repos/owner/paper/branches/main/protection", args[0])
            body = json.loads(options["input"])
            self.assertFalse(body["allow_force_pushes"])
            self.assertFalse(body["allow_deletions"])
            self.assertIn("free plan", self.gh.protect("owner/paper", "main"))

    def test_auth_and_protection_process_errors_are_reported(self):
        with mock.patch.object(github.subprocess, "run", side_effect=OSError("fixture cannot start gh")):
            self.assertIn("cannot start gh", self.gh.status()["error"])
            self.assertIn("cannot start gh", self.gh.protect("owner/paper", "main"))


if __name__ == "__main__":
    unittest.main()
