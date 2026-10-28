"""Programs Prism downloads itself (tools.py): Tectonic everywhere, the GitHub CLI on macOS
and Linux; LaTeX and Claude Code installed, Claude Code's login window, and what the Home
page is told (/api/tools). Never the network: a local HTTP server stands in for GitHub's
release pages and their files, which the tests build themselves."""
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
import unittest
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import errors  # noqa: E402
import github  # noqa: E402
import hub  # noqa: E402
import tools  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402

TARGETS = ("x86_64-pc-windows-msvc", "x86_64-apple-darwin", "aarch64-apple-darwin",
           "x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl")


def zipped(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def tarred(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeGitHub:
    """GitHub's API and its release files, on 127.0.0.1: path -> bytes."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.asked: list[str] = []
        files, asked = self.files, self.asked

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                asked.append(self.path)
                body = files.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def tectonic_releases(self, version="0.15.0", member=None):
        """A release list like GitHub's: newest first, with a prerelease and a draft that
        must not be taken. Each file holds the name of the release it is from."""
        rels = []
        for tag, extra in (("continuous", {"prerelease": True}), ("tectonic@9.9.9", {"draft": True}),
                           (f"tectonic@{version}", {}), ("tectonic@0.14.1", {})):
            assets = []
            for target in TARGETS:
                win = "windows" in target
                v = tag.split("@")[-1]
                name = f"tectonic-{v}-{target}.{'zip' if win else 'tar.gz'}"
                path = f"/dl/{tag}/{name}"
                exe = member or ("tectonic.exe" if win else "tectonic")
                body = f"{tag} {target}".encode()
                self.files[path] = zipped({exe: body}) if win else tarred({exe: body})
                assets.append({"name": name, "browser_download_url": self.url + path})
            rels.append({"tag_name": tag, "draft": False, "prerelease": False, **extra, "assets": assets})
        self.files[tools.TECTONIC_RELEASES] = json.dumps(rels).encode()

    def gh_release(self, version="2.99.0", break_checksum=False):
        assets, sums = [], []
        for name, maker in ((f"gh_{version}_macOS_universal.zip", zipped),
                            (f"gh_{version}_linux_amd64.tar.gz", tarred),
                            (f"gh_{version}_linux_arm64.tar.gz", tarred)):
            top = name.rsplit(".", 2 if name.endswith(".tar.gz") else 1)[0]
            body = maker({f"{top}/bin/gh": f"gh {name}".encode(), f"{top}/share/man/man1/gh.1": b"man"})
            self.files[f"/dl/{name}"] = body
            digest = hashlib.sha256(body + (b"x" if break_checksum else b"")).hexdigest()
            sums.append(f"{digest}  {name}")
            assets.append({"name": name, "browser_download_url": f"{self.url}/dl/{name}"})
        sums_name = f"gh_{version}_checksums.txt"
        self.files[f"/dl/{sums_name}"] = ("\n".join(sums) + "\n").encode()
        assets.append({"name": sums_name, "browser_download_url": f"{self.url}/dl/{sums_name}"})
        self.files[tools.GH_RELEASE] = json.dumps({"tag_name": f"v{version}", "assets": assets}).encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir()
        self.state = self.base / "state"
        self.path = self.base / "path"
        self.path.mkdir()
        self.gh = FakeGitHub()
        self.addCleanup(self.gh.stop)
        tools.INSTALL.clear()
        self.addCleanup(tools.INSTALL.clear)
        no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for patch in (mock.patch.dict(os.environ, {"PATH": str(self.path), "PRISM_STATE_DIR": str(self.state)}),
                      mock.patch.object(tools, "GITHUB_API", self.gh.url),
                      mock.patch.object(tools, "_opener", return_value=no_proxy),
                      mock.patch.object(tools, "KNOWN_DIRS", {}),
                      mock.patch.object(tools, "USER_BIN", self.base / "home" / ".local" / "bin"),
                      mock.patch.object(tools, "_registry_path", return_value=[]),
                      mock.patch.object(tools, "_machine", return_value="x86_64"),
                      mock.patch.object(github.GITHUB, "status", return_value={"gh": None})):
            patch.start()
            self.addCleanup(patch.stop)

    def wait(self, name):
        for _ in range(500):
            if tools.INSTALL[name]["state"] != "running":
                return tools.INSTALL[name]
            time.sleep(0.02)
        self.fail("still running")

    def leftovers(self):
        return sorted(p.name for p in (self.state / "bin").iterdir() if p.name.startswith("."))


class Tectonic(Base):
    def test_downloaded_for_this_computer_and_found_at_once(self):
        self.gh.tectonic_releases()
        self.assertIsNone(tools.status()["tex"]["tectonic"])
        self.assertEqual(tools.install("tectonic")["state"], "running")
        done = self.wait("tectonic")
        self.assertEqual(done["state"], "done", done)
        system = tools._system()
        exe = self.state / "bin" / ("tectonic.exe" if system == "windows" else "tectonic")
        target = tools._tectonic_target(system, "x86_64")
        self.assertEqual(exe.read_bytes(), f"tectonic@0.15.0 {target}".encode(),
                         "the newest published release: not the prerelease, not the draft")
        self.assertEqual(self.leftovers(), [], "no half download left behind")
        self.assertEqual(Path(tools.status()["tex"]["tectonic"]).resolve(), exe.resolve())
        self.assertIn(str(self.state / "bin"), os.environ["PATH"].split(os.pathsep))
        self.assertIn(str(self.state / "bin"), build.build_env()["PATH"], "the builds find it")
        self.assertEqual(tools.install("tectonic")["state"], "done", "there already")

    def test_on_linux_the_tar_gz_is_unpacked(self):
        self.gh.tectonic_releases()
        with mock.patch.object(tools, "_system", return_value="linux"):
            tools.install("tectonic")
            done = self.wait("tectonic")
        self.assertEqual(done["state"], "done", done)
        exe = self.state / "bin" / "tectonic"
        self.assertEqual(exe.read_bytes(), b"tectonic@0.15.0 x86_64-unknown-linux-musl")
        if os.name != "nt":
            self.assertTrue(os.access(exe, os.X_OK))

    def test_the_file_for_each_computer(self):
        self.gh.tectonic_releases()
        rels = json.loads(self.gh.files[tools.TECTONIC_RELEASES])
        for (system, machine), name in {
                ("macos", "aarch64"): "tectonic-0.15.0-aarch64-apple-darwin.tar.gz",
                ("macos", "x86_64"): "tectonic-0.15.0-x86_64-apple-darwin.tar.gz",
                ("linux", "x86_64"): "tectonic-0.15.0-x86_64-unknown-linux-musl.tar.gz",
                ("linux", "aarch64"): "tectonic-0.15.0-aarch64-unknown-linux-musl.tar.gz",
                ("windows", "x86_64"): "tectonic-0.15.0-x86_64-pc-windows-msvc.zip"}.items():
            asset = tools._tectonic_asset(rels, system, machine)
            self.assertEqual(asset["name"], name, (system, machine))
            self.assertEqual(asset["kind"], "zip" if system == "windows" else "tar.gz")
        self.assertIsNone(tools._tectonic_asset(rels, "linux", "riscv64"))
        self.assertIsNone(tools._tectonic_asset(rels, "windows", "aarch64"))
        with mock.patch.object(tools, "_system", return_value="linux"), \
                mock.patch.object(tools, "_machine", return_value="riscv64"):
            self.assertFalse(tools.can_install()["tectonic"])
            with self.assertRaises(errors.Refused):
                tools.install("tectonic")
        self.assertNotIn("tectonic", tools.INSTALL)

    def test_a_name_that_leaves_the_folder_is_not_unpacked(self):
        self.gh.tectonic_releases(member="../tectonic")
        with mock.patch.object(tools, "_system", return_value="linux"):
            tools.install("tectonic")
            done = self.wait("tectonic")
        self.assertEqual(done["state"], "failed")
        self.assertIn("did not contain the program", done["error"])
        self.assertFalse((self.state / "tectonic").exists())
        self.assertFalse((self.state / "bin" / "tectonic").exists())
        self.assertEqual(self.leftovers(), [])

    def test_github_not_answering_is_said_plainly(self):
        with mock.patch.object(tools, "_system", return_value="linux"):  # nothing served: a 404
            tools.install("tectonic")
            done = self.wait("tectonic")
        self.assertEqual(done["state"], "failed")
        self.assertIn("Check your internet connection", done["error"])
        self.assertNotIn("404", done["error"])
        self.assertIn("404", done["details"])
        self.assertIn(tools.TECTONIC_RELEASES, done["details"])


class GitHubCli(Base):
    def test_downloaded_on_linux_and_checked(self):
        self.gh.gh_release()
        with mock.patch.object(tools, "_system", return_value="linux"):
            self.assertTrue(tools.can_install()["gh"])
            tools.install("gh")
            done = self.wait("gh")
        self.assertEqual(done["state"], "done", done)
        self.assertEqual((self.state / "bin" / "gh").read_bytes(), b"gh gh_2.99.0_linux_amd64.tar.gz")
        self.assertEqual(self.leftovers(), [])
        github.GITHUB.status.assert_called_with(refresh=True)

    def test_on_a_mac_the_universal_zip(self):
        self.gh.gh_release()
        with mock.patch.object(tools, "_system", return_value="macos"), \
                mock.patch.object(tools, "_machine", return_value="aarch64"):
            tools.install("gh")
            done = self.wait("gh")
        self.assertEqual(done["state"], "done", done)
        self.assertEqual((self.state / "bin" / "gh").read_bytes(), b"gh gh_2.99.0_macOS_universal.zip")

    def test_a_download_that_does_not_match_its_checksum_is_not_installed(self):
        self.gh.gh_release(break_checksum=True)
        with mock.patch.object(tools, "_system", return_value="linux"):
            tools.install("gh")
            done = self.wait("gh")
        self.assertEqual(done["state"], "failed")
        self.assertEqual(done["error"], "The GitHub CLI's download was not the file GitHub published for it, "
                                        "so it was not installed. Try again.")
        self.assertIn("SHA-256", done["details"])
        self.assertFalse((self.state / "bin" / "gh").exists())
        self.assertEqual(self.leftovers(), [])

    def test_windows_still_uses_winget(self):
        with mock.patch.object(tools, "_system", return_value="windows"), \
                mock.patch.object(tools, "winget", return_value=None):
            self.assertFalse(tools.can_install()["gh"])
            with self.assertRaises(errors.Refused):
                tools.install("gh")
        self.assertEqual(self.gh.asked, [], "nothing downloaded")


class LaTeXAndClaude(Base):
    def test_full_latex(self):
        # A Mac, even with Homebrew: MacTeX's installer wants the administrator's password,
        # which a background install cannot give it, so the page says how instead.
        with mock.patch.object(tools, "_system", return_value="macos"), \
                mock.patch.object(tools, "tex_engine", return_value=None), \
                mock.patch.object(tools.proc, "run", side_effect=AssertionError("nothing is run")):
            self.assertFalse(tools.can_install()["tex"])
            with self.assertRaises(errors.Refused) as said:
                tools.install("tex")
            self.assertIn("https://tug.org/mactex/", str(said.exception))
        with mock.patch.object(tools, "_system", return_value="linux"), \
                mock.patch.object(tools, "tex_engine", return_value=None):
            self.assertFalse(tools.can_install()["tex"])
            self.assertIn("texlive-latex-extra", tools.how_to("tex"))
            with self.assertRaises(errors.Refused):
                tools.install("tex")
        with mock.patch.object(tools, "_system", return_value="windows"), \
                mock.patch.object(tools, "winget", return_value="winget"), \
                mock.patch.object(tools, "tex_engine", return_value=None), \
                mock.patch.object(tools.proc, "run", return_value=subprocess.CompletedProcess([], 1, "", "no")):
            tools.install("tex")
            done = self.wait("tex")
        self.assertEqual(done["state"], "failed")
        self.assertIn("miktex.org", done["error"])

    def test_claude_code_installer(self):
        which = {"powershell": r"C:\ps\powershell.exe", "bash": "/bin/bash", "curl": "/usr/bin/curl"}
        with mock.patch.object(tools.shutil, "which", side_effect=lambda n, *a, **k: which.get(n)):
            with mock.patch.object(tools, "_system", return_value="windows"):
                self.assertEqual(tools._claude_installer(),
                                 [r"C:\ps\powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                                  "irm https://claude.ai/install.ps1 | iex"])
            with mock.patch.object(tools, "_system", return_value="macos"):
                self.assertEqual(tools._claude_installer(),
                                 ["/bin/bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"])
            del which["curl"]
            with mock.patch.object(tools, "_system", return_value="linux"):
                self.assertIsNone(tools._claude_installer())
                self.assertFalse(tools.can_install()["claude"])

    @unittest.skipIf(any(Path(p).exists() for p in ("/usr/local/bin/claude", "/opt/homebrew/bin/claude")),
                     "Claude Code is installed on this computer, where it is looked for too")
    def test_claude_code_is_found_in_local_bin_once_installed(self):
        home_bin = tools.USER_BIN
        exe = home_bin / ("claude.exe" if os.name == "nt" else "claude")

        def run(cmd, **kw):                      # what the installer does
            home_bin.mkdir(parents=True)
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            exe.chmod(0o755)
            return subprocess.CompletedProcess(cmd, 0, "Claude Code installed", "")
        with mock.patch.dict(os.environ), \
                mock.patch.object(tools, "_claude_installer", return_value=["installer"]), \
                mock.patch.object(Path, "home", return_value=self.base / "home"), \
                mock.patch.object(tools.proc, "run", side_effect=run):
            os.environ.pop("CLAUDE_BIN", None)
            self.assertIsNone(tools.claude_path())
            tools.install("claude")
            done = self.wait("claude")
            self.assertEqual(done["state"], "done", done)
            self.assertEqual(Path(tools.status()["claude"]).resolve(), exe.resolve())

    def test_the_login_window(self):
        with mock.patch.object(tools, "claude_path", return_value=None):
            with self.assertRaises(errors.Refused) as said:
                tools.claude_login()
            self.assertIn("not installed", str(said.exception))
        opened = []
        popen = mock.patch.object(tools.subprocess, "Popen", side_effect=lambda cmd, **kw: opened.append((cmd, kw)))
        with popen, mock.patch.object(tools, "claude_path", return_value="/u/claude"):
            with mock.patch.object(tools, "_system", return_value="windows"):
                self.assertEqual(tools.claude_login(), {"ok": True})
            self.assertEqual(opened[-1][0][1:], ["/k", "/u/claude"])
            self.assertTrue(opened[-1][1]["creationflags"] & 0x10, "a console window of its own")
            with mock.patch.object(tools, "_system", return_value="macos"):
                tools.claude_login()
            self.assertEqual(opened[-1][0][:3], ["osascript", "-e",
                                                 'tell application "Terminal" to do script "/u/claude"'])
            with mock.patch.object(tools, "_system", return_value="linux"), \
                    mock.patch.object(tools.shutil, "which",
                                      side_effect=lambda n, *a, **k: "/usr/bin/xterm" if n == "xterm" else None):
                tools.claude_login()
            self.assertEqual(opened[-1][0], ["/usr/bin/xterm", "-e", "/u/claude"])
            with mock.patch.object(tools, "_system", return_value="linux"), \
                    mock.patch.object(tools.shutil, "which", return_value=None):
                with self.assertRaises(errors.Refused) as said:
                    tools.claude_login()
            self.assertIn("No terminal program", str(said.exception))


class WhatHomeIsTold(Base):
    def test_status_shape(self):
        s = tools.status()
        for key in ("git", "gh", "tex", "claude", "can_install", "installing", "how"):
            self.assertIn(key, s)
        self.assertEqual(set(s["tex"]), {"engine", "tectonic"})
        self.assertEqual(set(s["can_install"]), set(tools.PROGRAMS))
        self.assertEqual(set(s["how"]), set(tools.PROGRAMS))
        self.assertTrue(all(isinstance(v, bool) for v in s["can_install"].values()))
        self.assertTrue(all(isinstance(v, str) and v for v in s["how"].values()))
        self.assertEqual(tools.PROGRAMS, ("git", "gh", "tectonic", "tex", "claude"))

    def test_api_tools(self):
        answered = []
        h = hub.Handler.__new__(hub.Handler)
        h._json = lambda obj, code=200: answered.append(obj)
        with mock.patch.object(github.GITHUB, "login_state", return_value={"state": None}):
            h.get("/api/tools", {})
        body = json.loads(json.dumps(answered[0]))           # what the page gets
        self.assertEqual(set(body), {"git", "gh", "tex", "claude", "can_install", "installing", "how",
                                     "github", "login"})
        self.assertEqual(body["login"], {"state": None})
        with mock.patch.object(tools, "claude_login", return_value={"ok": True}):
            h.post("/api/tools/claude-login", {})
        self.assertEqual(answered[-1], {"ok": True})

    def test_refresh_path_adds_prisms_own_folder(self):
        tools.refresh_path()
        self.assertNotIn(str(self.state / "bin"), os.environ["PATH"].split(os.pathsep), "not there yet")
        (self.state / "bin").mkdir(parents=True)
        tools.refresh_path()
        tools.refresh_path()
        self.assertEqual(os.environ["PATH"].split(os.pathsep).count(str(self.state / "bin")), 1)
        tools.USER_BIN.mkdir(parents=True)
        tools.refresh_path()
        self.assertIn(str(tools.USER_BIN), os.environ["PATH"].split(os.pathsep))

    def test_a_missing_engine_points_at_getting_started(self):
        root = tmpdir()
        (root / "main.tex").write_text("\\documentclass{article}\\begin{document}x\\end{document}", encoding="utf-8")
        with mock.patch("build.shutil.which", return_value=None):
            r = build.Build(root, "main.tex", "build").run()
        self.assertIn("pdflatex was not found. Install LaTeX from the Home page (Getting started)", r["output"])
        self.assertIn("miktex.org", r["output"])


if __name__ == "__main__":
    unittest.main()
