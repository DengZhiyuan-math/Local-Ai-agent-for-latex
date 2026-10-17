"""The production GitHub adapter, with only the external gh process replaced."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import github


def result(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["gh"], code, stdout, stderr)


class GitHubCLI(unittest.TestCase):
    def setUp(self):
        self.gh = github.GhCli()
        patch = mock.patch.object(github.tools, "find", return_value="fixture-gh")
        patch.start()
        self.addCleanup(patch.stop)

    def test_login_is_cached_and_can_be_refreshed(self):
        with mock.patch.object(github.proc, "run", side_effect=[
                result(stderr="Logged in to github.com account owner (keyring)"),
                result(1, stderr="not logged in")]) as run:
            self.assertEqual(self.gh.status()["account"], "owner")
            self.assertTrue(self.gh.status()["logged_in"])
            self.assertEqual(run.call_count, 1)
            self.assertFalse(self.gh.status(refresh=True)["logged_in"])
            self.assertEqual(run.call_count, 2)

    def test_login_cache_expires(self):
        with mock.patch.object(github.time, "time", return_value=100) as clock, \
                mock.patch.object(github.proc, "run", side_effect=[
                    result(stdout="Logged in to github.com as owner"),
                    result(stdout="Logged in to github.com as other")]) as run:
            self.assertEqual(self.gh.status()["account"], "owner")
            clock.return_value += github.STATUS_TTL + 1
            self.assertEqual(self.gh.status()["account"], "other")
            self.assertEqual(run.call_count, 2)

    def test_missing_cli_is_reported_without_starting_a_process(self):
        with mock.patch.object(github.tools, "find", return_value=None), \
                mock.patch.object(github.proc, "run") as run:
            self.assertIn("not installed", self.gh.status()["error"])
            self.assertIn("Home page", self.gh.status()["error"], "where to install it, not a command")
            self.assertIn("not installed", self.gh.create(Path("."), "owner/paper"))
        run.assert_not_called()

    def test_create_uses_the_project_and_sanitizes_inherited_git_selectors(self):
        with mock.patch.object(self.gh, "status", return_value={"gh": "fixture-gh"}), \
                mock.patch.dict(os.environ, {"GIT_DIR": "foreign", "GIT_WORK_TREE": "foreign",
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.url",
                    "GIT_CONFIG_VALUE_0": "foreign"}), \
                mock.patch.object(github.proc, "run", return_value=result()) as run:
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
                mock.patch.object(github.proc, "run", side_effect=[
                    result(1, stderr="no network"), subprocess.TimeoutExpired("gh", 180)]):
            self.assertEqual(self.gh.create(Path("."), "owner/paper"), "no network")
            self.assertIn("timed out", self.gh.create(Path("."), "owner/paper"))

    def test_branch_protection_and_unavailable_plan(self):
        with mock.patch.object(self.gh, "status", return_value={"gh": "fixture-gh"}), \
                mock.patch.object(github.proc, "run", side_effect=[
                    result(), result(1, stderr="Upgrade to GitHub Pro")]) as run:
            self.assertTrue(self.gh.protect("owner/paper", "main").startswith("on:"))
            args, options = run.call_args
            self.assertIn("repos/owner/paper/branches/main/protection", args[0])
            body = json.loads(options["input"])
            self.assertFalse(body["allow_force_pushes"])
            self.assertFalse(body["allow_deletions"])
            self.assertIn("free plan", self.gh.protect("owner/paper", "main"))

    def test_auth_and_protection_process_errors_are_reported(self):
        with mock.patch.object(github.proc, "run", side_effect=OSError("fixture cannot start gh")):
            self.assertIn("cannot start gh", self.gh.status()["error"])
            self.assertIn("cannot start gh", self.gh.protect("owner/paper", "main"))


FAKE_GH = r'''
import os, sys, time
from pathlib import Path
here = Path(os.environ["FAKE_GH_DIR"])
args = sys.argv[1:]
if args[:2] == ["auth", "login"]:
    (here / "login-args").write_text(" ".join(args))
    print("! First copy your one-time code: AB12-CD34", flush=True)
    print("Open this URL to continue in your web browser: https://github.com/login/device", flush=True)
    while not (here / "approve").exists() and not (here / "deny").exists():
        time.sleep(0.05)
    if (here / "deny").exists():
        print("X Authentication failed", flush=True)
        sys.exit(1)
    (here / "logged-in").write_text("me")
    print("Logged in as me", flush=True)
elif args[:2] == ["auth", "setup-git"]:
    (here / "setup-git").write_text(" ".join(args))
elif args[:2] == ["auth", "status"]:
    if (here / "logged-in").exists():
        print("github.com\n  Logged in to github.com account me (keyring)")
    else:
        print("You are not logged into any GitHub hosts.", file=sys.stderr)
        sys.exit(1)
'''


class LoggingIn(unittest.TestCase):
    """Logging in from the Home page: gh's device flow, its code on the page, git set up."""

    def setUp(self):
        from tmpdirs import tmpdir
        self.dir = tmpdir()
        script = self.dir / "fake_gh.py"
        script.write_text(FAKE_GH, encoding="utf-8")
        if os.name == "nt":
            exe = self.dir / "gh.cmd"
            exe.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        else:
            exe = self.dir / "gh"
            exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            exe.chmod(0o755)
        for patch in (mock.patch.dict(os.environ, {"FAKE_GH_DIR": str(self.dir)}),
                      mock.patch.object(github.tools, "find", return_value=str(exe))):
            patch.start()
            self.addCleanup(patch.stop)
        self.gh = github.GhCli()

    def until(self, state, wait=15):
        end = time.monotonic() + wait
        while time.monotonic() < end:
            st = self.gh.login_state()
            if st["state"] == state:
                return st
            time.sleep(0.05)
        self.fail(f"not {state}: {self.gh.login_state()}")

    def test_the_code_is_shown_and_git_set_up_once_authorized(self):
        self.assertFalse(self.gh.status(refresh=True)["logged_in"])
        self.gh.login()
        st = self.until("waiting")
        self.assertEqual((st["code"], st["url"]), ("AB12-CD34", "https://github.com/login/device"))
        self.assertIn("--web", (self.dir / "login-args").read_text())
        self.assertEqual(self.gh.login()["state"], "waiting", "a second click does not start another")
        (self.dir / "approve").write_text("")
        self.until("done")
        self.assertIn("setup-git", (self.dir / "setup-git").read_text(), "git pushes with this login")
        self.assertEqual(self.gh.status()["account"], "me", "the status is asked again")

    def test_refused_cancelled_and_too_slow_say_so(self):
        self.gh.login()
        self.until("waiting")
        (self.dir / "deny").write_text("")
        st = self.until("failed")
        self.assertEqual(st["error"], "Logging in to GitHub did not work. Try again.")
        self.assertIn("Authentication failed", st["details"])
        (self.dir / "deny").unlink()
        self.gh.login()
        self.until("waiting")
        self.gh.cancel_login()
        self.assertEqual(self.until("failed")["error"], "Logging in to GitHub was cancelled.")
        with mock.patch.object(github.GhCli, "LOGIN_TIME", 1):
            self.gh.login()
            self.assertIn("was not confirmed", self.until("failed")["error"])
        self.assertFalse((self.dir / "setup-git").exists())


if __name__ == "__main__":
    unittest.main()
