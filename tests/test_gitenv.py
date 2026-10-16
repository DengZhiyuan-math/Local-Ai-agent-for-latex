"""Running git the one way prism-local does (gitenv.py)."""
import os
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gitenv  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class Git(unittest.TestCase):
    def setUp(self):
        self.repo = tmpdir() / "练习 1"
        self.repo.mkdir()
        gitenv.git(self.repo, "init", "-q", check=True)

    def test_output_is_utf8_whatever_the_code_page(self):
        top = gitenv.git(self.repo, "rev-parse", "--show-toplevel").stdout.strip()
        self.assertEqual(Path(top).resolve(), self.repo.resolve())

    def test_the_repository_is_found_from_cwd_only(self):
        other = tmpdir()
        gitenv.git(other, "init", "-q", check=True)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}):
            top = gitenv.git(self.repo, "rev-parse", "--show-toplevel").stdout.strip()
        self.assertEqual(Path(top).resolve(), self.repo.resolve())

    def test_bytes_failures_and_check(self):
        (self.repo / "a.bin").write_bytes(b"\x00\xff")
        gitenv.git(self.repo, "add", "a.bin", check=True)
        blob = gitenv.git(self.repo, "show", ":a.bin", binary=True).stdout
        self.assertEqual(blob, b"\x00\xff")
        self.assertNotEqual(gitenv.git(self.repo, "show", "nope").returncode, 0)
        with self.assertRaises(subprocess.CalledProcessError):
            gitenv.git(self.repo, "show", "nope", check=True)

    def test_git_never_waits_for_input(self):
        # stdin is closed: a git that reads it (hash-object --stdin) gets EOF at once.
        self.assertEqual(gitenv.git(self.repo, "hash-object", "--stdin", timeout=10).returncode, 0)

    def test_only_an_interactive_git_may_ask_for_a_login(self):
        # A stand-in for a login window (Git Credential Manager's, an askpass program's):
        # it leaves a mark when asked and answers "nobody".
        where = tmpdir()
        marker = where / "asked"
        if os.name == "nt":
            ask = where / "ask.bat"
            ask.write_text('@echo asked> "%~dp0asked"\n@echo nobody\n', encoding="ascii")
        else:
            ask = where / "ask.sh"
            ask.write_text('#!/bin/sh\necho asked > "$(dirname "$0")/asked"\necho nobody\n',
                           encoding="ascii")
            ask.chmod(0o755)
        # A remote that wants a login. No credential helper (not yours, in a test) is asked.
        srv = HTTPServer(("127.0.0.1", 0), LoginWanted)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        question = ["-c", "credential.helper=", "-c", "http.proxy=", "ls-remote",
                    f"http://127.0.0.1:{srv.server_address[1]}/repo.git"]
        login = {"GIT_ASKPASS": str(ask), "SSH_ASKPASS": str(ask), "GCM_INTERACTIVE": "auto",
                 "NO_PROXY": "127.0.0.1"}
        with mock.patch.dict(os.environ, login), \
                mock.patch.object(gitenv.proc, "run", wraps=gitenv.proc.run) as run:
            self.assertNotEqual(gitenv.git(self.repo, *question, timeout=30).returncode, 0)
            self.assertFalse(marker.exists(), "in the background nobody is asked")
            self.assertEqual(run.call_args.kwargs["env"]["GCM_INTERACTIVE"], "never")
            gitenv.git(self.repo, *question, timeout=30, interactive=True)
            self.assertTrue(marker.exists(), "a button may ask")
            self.assertEqual(run.call_args.kwargs["env"]["GCM_INTERACTIVE"], "auto")


class LoginWanted(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="test"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    unittest.main()
