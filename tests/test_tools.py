"""git and the GitHub CLI found, and installed from the Home page (tools.py). Nothing is
installed for real: winget is a stand-in that puts a program where its installer would."""
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import errors  # noqa: E402
import gitenv  # noqa: E402
import tools  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402

EXE = ".exe" if os.name == "nt" else ""


def program(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / (name + EXE)
    p.write_text("#!/bin/sh\n", encoding="utf-8")
    p.chmod(0o755)
    return p


class WindowsOS:
    name = "nt"

    def __getattr__(self, name):
        return getattr(os, name)


class Tools(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        env = mock.patch.dict(os.environ, {"PATH": str(self.bin), "PRISM_STATE_DIR": str(self.base / "state")})
        env.start()
        self.addCleanup(env.stop)
        tools.INSTALL.clear()
        self.addCleanup(tools.INSTALL.clear)
        # Nothing from this computer's own installs or registry.
        for patch in (mock.patch.object(tools, "os", WindowsOS()),
                      mock.patch.object(tools, "_system", return_value="windows"),
                      mock.patch.object(tools, "KNOWN_DIRS", {"git": [str(self.base / "Git" / "cmd")],
                                                               "gh": [str(self.base / "GitHub CLI")]}),
                      mock.patch.object(tools, "USER_BIN", self.base / "home" / ".local" / "bin"),
                      mock.patch.object(tools, "_registry_path", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def wait(self, name):
        for _ in range(200):
            if tools.INSTALL[name]["state"] != "running":
                return tools.INSTALL[name]
            time.sleep(0.02)
        self.fail("still running")

    def test_a_program_installed_meanwhile_is_found_without_a_restart(self):
        self.assertIsNone(tools.find("gh"))
        installed = program(self.base / "GitHub CLI", "gh")
        self.assertEqual(Path(tools.find("gh")).resolve(), installed.resolve(), "where its installer puts it")

    def test_install_runs_winget_and_says_when_it_is_there(self):
        calls = []

        def winget(cmd, **kw):
            calls.append(cmd)
            program(self.base / "Git" / "cmd", "git")             # what the installer does
            return subprocess.CompletedProcess(cmd, 0, "Successfully installed", "")
        with mock.patch.object(tools, "winget", return_value="winget"), \
                mock.patch.object(tools.proc, "run", side_effect=winget):
            self.assertEqual(tools.install("git")["state"], "running")
            self.assertEqual(self.wait("git")["state"], "done")
        self.assertEqual(calls[0][:4], ["winget", "install", "--id", "Git.Git"])
        self.assertIn("--silent", calls[0])
        self.assertTrue(tools.status()["git"])
        self.assertEqual(tools.install("git")["state"], "done", "there already: nothing to run")
        self.assertEqual(len(calls), 1)

    def test_two_clicks_at_once_start_one_install(self):
        calls, go = [], threading.Event()

        def winget(cmd, **kw):
            calls.append(cmd)
            go.wait(5)                                              # still installing
            program(self.base / "Git" / "cmd", "git")
            return subprocess.CompletedProcess(cmd, 0, "Successfully installed", "")

        def looked_up_slowly():                                     # the moment between two clicks, held open
            time.sleep(0.1)
            return "winget"
        both, said = threading.Barrier(2), []

        def click():
            both.wait(5)
            said.append(tools.install("git")["state"])
        with mock.patch.object(tools, "winget", side_effect=looked_up_slowly), \
                mock.patch.object(tools.proc, "run", side_effect=winget):
            clicks = [threading.Thread(target=click) for _ in range(2)]
            for t in clicks:
                t.start()
            for t in clicks:
                t.join(5)
            time.sleep(0.1)                                         # a second winget would have started by now
            go.set()
            self.assertEqual(self.wait("git")["state"], "done")
        self.assertEqual(said, ["running", "running"], "the second click sees the first one's install")
        self.assertEqual(len(calls), 1)

    def test_a_failed_install_says_what_to_do_and_keeps_winget_apart(self):
        with mock.patch.object(tools, "winget", return_value="winget"), \
                mock.patch.object(tools.proc, "run", return_value=subprocess.CompletedProcess(
                    [], 1, "", "Installer failed with exit code: 1602")):
            tools.install("gh")
            done = self.wait("gh")
        self.assertEqual(done["state"], "failed")
        self.assertIn("could not be installed", done["error"])
        self.assertIn("cli.github.com", done["error"])
        self.assertIn("1602", done["details"])

    def test_without_winget_it_says_how_to_install_by_hand(self):
        with mock.patch.object(tools, "winget", return_value=None):
            with self.assertRaises(errors.Refused) as said:
                tools.install("git")
        self.assertEqual(str(said.exception), tools.how_to("git"))
        self.assertNotIn("git", tools.status()["installing"], "nothing left running")
        self.assertFalse(tools.status()["can_install"]["git"])
        with self.assertRaises(errors.Refused):
            tools.install("python")

    def test_git_missing_is_said_in_plain_words(self):
        with self.assertRaises(errors.NotInstalled) as said:
            gitenv.git(self.base, "status")
        self.assertEqual(str(said.exception), "Git is not installed on this computer. Install it from the Home page.")
        self.assertIsInstance(said.exception, OSError, "callers catch it as before")


if __name__ == "__main__":
    unittest.main()
