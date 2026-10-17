"""Tests for install.ps1, the one-line Windows install (`irm …/install.ps1 | iex`).

The script runs in a real PowerShell (pwsh, and Windows PowerShell 5.1 where present) with
a PATH holding only fake winget, git, py and python (.cmd files that run a small Python
script and write each call to a log), and with LOCALAPPDATA, APPDATA, USERPROFILE and
ProgramFiles in temporary folders. Nothing is installed, nothing goes over the network, and
the real Start menu, desktop and %LOCALAPPDATA%\\Prism are not touched.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path

from tmpdirs import tmpdir

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.ps1"
SHORTCUT = ROOT / "launcher" / "make-shortcut.ps1"
REPO = "https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex.git"
WIN = sys.platform == "win32"
SHELLS = [s for s in (shutil.which("pwsh"), shutil.which("powershell")) if s] if WIN else []
WINPS = shutil.which("powershell") if WIN else None

# The fake programs. argv: TOOL ARGS…; every call is appended to $FAKE_LOG as JSON.
FAKE = r'''
import json, os, sys
from pathlib import Path

tool, args = sys.argv[1], sys.argv[2:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps([tool] + args) + "\n")

def wrapper(folder, name):
    Path(folder, name + ".cmd").write_text(
        '@echo off\r\n"%s" "%s" %s %%*\r\nexit /b %%errorlevel%%\r\n' % (sys.executable, __file__, name))

if tool in ("py", "python", "alias"):                  # answers install.ps1's probe
    print("True")
    print("3.12.4")
    print(os.environ["FAKE_PYTHON_EXE"].encode("utf-8").hex())
elif tool == "winget":
    ident = args[args.index("--id") + 1]
    name = {"Python.Python.3.12": "python", "Git.Git": "git"}.get(ident)
    if name:
        wrapper(os.environ["FAKE_PROVIDES"], name)
elif tool == "git":
    cwd = Path.cwd()
    if args[:1] == ["-C"]:
        cwd, args = Path(args[1]), args[2:]
    if args[:1] == ["--version"]:
        print("git version 2.47.0.windows.1")
    elif args[:3] == ["config", "--get", "remote.origin.url"]:
        config = cwd / ".git" / "config"
        urls = [l.split("=", 1)[1].strip() for l in
                (config.read_text().splitlines() if config.is_file() else []) if l.strip().startswith("url")]
        if not urls:
            sys.exit(1)
        print(urls[0])
    elif args[:1] == ["pull"]:
        sys.exit(int(os.environ.get("FAKE_PULL_EXIT", "0")))
    elif args[:1] == ["clone"]:
        url, dest = args[-2], Path(args[-1])
        (dest / ".git").mkdir(parents=True, exist_ok=True)
        (dest / ".git" / "config").write_text('[remote "origin"]\n\turl = %s\n' % url)
        for f in ("launcher/prism_launcher.pyw", "launcher/make-shortcut.ps1", "prism_local/hub.py"):
            (dest / f).parent.mkdir(parents=True, exist_ok=True)
            (dest / f).write_text("# fake\n")
    else:
        sys.exit(1)
'''


def make_checkout(path: Path, url: str = REPO) -> None:
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')
    (path / "launcher").mkdir()
    (path / "launcher" / "prism_launcher.pyw").write_text("# old\n")


@unittest.skipUnless(WIN and SHELLS, "needs Windows and PowerShell")
class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.bin = self.tmp / "bin"                  # the fakes the test starts with
        self.provides = self.tmp / "provides"        # where the fake winget "installs" to
        self.home = self.tmp / "home"
        for d in (self.bin, self.provides, self.home, self.tmp / "local", self.tmp / "roaming",
                  self.tmp / "programs"):
            d.mkdir()
        self.fake = self.tmp / "fake_tool.py"
        self.fake.write_text(FAKE, encoding="utf-8")
        self.log = self.tmp / "calls.jsonl"
        self.dir = self.tmp / "local" / "Prism" / "prism-local"

    def tools(self, *names):
        for name in names:
            (self.bin / f"{name}.cmd").write_text(
                f'@echo off\r\n"{sys.executable}" "{self.fake}" {name} %*\r\nexit /b %errorlevel%\r\n')

    def calls(self, tool=None):
        if not self.log.exists():
            return []
        rows = [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines() if l]
        return [r[1:] for r in rows if tool is None or r[0] == tool]

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith("PRISM_") and k.upper() not in ("PSMODULEPATH",)}
        system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        env.update({
            "PATH": os.pathsep.join([str(self.bin), str(self.provides), system32]),
            "LOCALAPPDATA": str(self.tmp / "local"), "APPDATA": str(self.tmp / "roaming"),
            "USERPROFILE": str(self.home), "HOME": str(self.home),
            "ProgramFiles": str(self.tmp / "programs"),
            "PRISM_INSTALL_NO_REGISTRY": "1", "PRISM_NO_SHORTCUT": "1", "PRISM_NO_LAUNCH": "1",
            "FAKE_LOG": str(self.log), "FAKE_PROVIDES": str(self.provides),
            "FAKE_PYTHON_EXE": str(self.tmp / "Python312" / "python.exe"),
        })
        env.update(extra)
        return env

    def run_file(self, shell, *args, **env):
        cmd = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), *args]
        return subprocess.run(cmd, env=self.env(**env), capture_output=True, text=True, timeout=180)

    def run_iex(self, shell, **env):
        """The way users run it: the script's text piped into iex, then more commands."""
        command = (f"$ErrorActionPreference = 'Continue'; Get-Content -LiteralPath '{SCRIPT}' -Raw | iex; "
                   "Write-Host \"ALIVE $LASTEXITCODE $ErrorActionPreference "
                   "$([bool](Get-Command Find-Python -ErrorAction SilentlyContinue))\"; exit $LASTEXITCODE")
        return subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                              env=self.env(**env), capture_output=True, text=True, timeout=180)

    def each_shell(self):
        """Each PowerShell in turn, starting afresh: no calls logged, no Prism folder."""
        for shell in SHELLS:
            if self.log.exists():
                self.log.unlink()
            for d in (self.dir, self.provides):
                shutil.rmtree(d, ignore_errors=True)
            self.provides.mkdir()
            yield shell

    def assertOk(self, r):
        self.assertEqual(r.returncode, 0, f"{r.args[0]}\n{r.stdout}{r.stderr}")
        self.assertIn("Prism is installed", r.stdout, r.args[0])

    # ---------------------------------------------------------------- tests

    def test_python_and_git_found_fresh_clone(self):
        self.tools("py", "python", "git", "winget")
        for shell in self.each_shell():
            r = self.run_file(shell, PRISM_DIR=str(self.dir))
            self.assertOk(r)
            self.assertIn("Checking Python... found 3.12.4", r.stdout)
            self.assertIn("Checking git... found", r.stdout)
            self.assertEqual(self.calls("py")[0][0], "-3")
            self.assertEqual(self.calls("winget"), [])
            self.assertIn(["clone", "--quiet", REPO, str(self.dir)], self.calls("git"))
            self.assertTrue((self.dir / "launcher" / "prism_launcher.pyw").is_file())

    def test_parameters_when_run_as_a_file(self):
        self.tools("python", "git")
        target = self.tmp / "elsewhere" / "prism"
        for shell in self.each_shell():
            shutil.rmtree(target, ignore_errors=True)
            r = self.run_file(shell, "-Dir", str(target), "-NoShortcut", "-NoLaunch",
                              PRISM_NO_SHORTCUT="", PRISM_NO_LAUNCH="")
            self.assertOk(r)
            self.assertIn(["clone", "--quiet", REPO, str(target)], self.calls("git"))
            self.assertNotIn("Making the Prism shortcut", r.stdout)
            self.assertNotIn("Opening the Prism Home page", r.stdout)

    def test_default_folder_is_under_localappdata(self):
        self.tools("python", "git")
        for shell in self.each_shell():
            r = self.run_file(shell)
            self.assertOk(r)
            self.assertIn(["clone", "--quiet", REPO, str(self.tmp / "local" / "Prism" / "prism-local")],
                          self.calls("git"))

    def test_python_missing_is_installed_with_winget_for_the_user(self):
        self.tools("git", "winget")
        for shell in self.each_shell():
            r = self.run_file(shell, PRISM_DIR=str(self.dir))
            self.assertOk(r)
            self.assertIn("Checking Python... not found.", r.stdout)
            winget = self.calls("winget")
            self.assertEqual(len(winget), 1, winget)
            self.assertEqual(winget[0][:4], ["install", "--id", "Python.Python.3.12", "-e"])
            self.assertIn("--scope", winget[0])
            self.assertEqual(winget[0][winget[0].index("--scope") + 1], "user")
            self.assertIn("--silent", winget[0])
            self.assertEqual(winget[0][winget[0].index("--source") + 1], "winget")
            self.assertIn("Python 3.12.4 is installed.", r.stdout)

    def test_python_missing_without_winget_is_never_installed_for_real(self):
        self.tools("git")
        for shell in self.each_shell():
            r = self.run_file(shell, PRISM_DIR=str(self.dir))
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("Python could not be installed", r.stdout)
            self.assertNotIn("Downloading Python", r.stdout)
            self.assertFalse(self.dir.exists())

    def test_python_store_alias_is_skipped(self):
        # A python.cmd under a WindowsApps folder stands for the Microsoft Store's alias.
        alias = self.tmp / "Microsoft" / "WindowsApps"
        alias.mkdir(parents=True)
        (alias / "python.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{self.fake}" alias %*\r\n')
        self.tools("git", "winget")
        for shell in self.each_shell():
            r = self.run_file(shell, PRISM_DIR=str(self.dir),
                              PATH=os.pathsep.join([str(alias), str(self.bin), str(self.provides)]))
            self.assertOk(r)
            self.assertEqual(self.calls("alias"), [], "the Store alias was run")
            self.assertEqual(self.calls("winget")[0][2], "Python.Python.3.12")

    def test_git_missing_is_installed_with_winget(self):
        self.tools("python", "winget")
        for shell in self.each_shell():
            r = self.run_file(shell, PRISM_DIR=str(self.dir))
            self.assertOk(r)
            self.assertIn("Installing git (Windows may ask for your permission)...", r.stdout)
            winget = self.calls("winget")
            self.assertEqual(len(winget), 1, winget)
            self.assertEqual(winget[0][:3], ["install", "--id", "Git.Git"])
            self.assertIn(["clone", "--quiet", REPO, str(self.dir)], self.calls("git"))

    def test_without_git_or_winget_a_zip_is_unpacked(self):
        self.tools("python")
        archive = self.tmp / "main.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Local-Ai-agent-for-latex-main/launcher/prism_launcher.pyw", "# zip\n")
            z.writestr("Local-Ai-agent-for-latex-main/prism_local/hub.py", "# zip\n")
        for shell in self.each_shell():
            r = self.run_file(shell, PRISM_DIR=str(self.dir), PRISM_INSTALL_ZIP_URL=str(archive))
            self.assertOk(r)
            self.assertEqual((self.dir / "prism_local" / "hub.py").read_text(), "# zip\n")
            self.assertIn("does not update itself until git is installed", r.stdout)
            # Run again: the downloaded copy is refreshed, not refused.
            r = self.run_file(shell, PRISM_DIR=str(self.dir), PRISM_INSTALL_ZIP_URL=str(archive))
            self.assertOk(r)
            self.assertIn("Downloading the newest Prism", r.stdout)

    def test_existing_checkout_is_updated_fast_forward(self):
        self.tools("python", "git")
        for shell in self.each_shell():
            make_checkout(self.dir)
            r = self.run_file(shell, PRISM_DIR=str(self.dir))
            self.assertOk(r)
            git = self.calls("git")
            self.assertIn(["-C", str(self.dir), "pull", "--ff-only", "--quiet"], git)
            self.assertFalse([c for c in git if c[:1] == ["clone"]], git)
            self.assertIn("Prism is up to date.", r.stdout)

    def test_existing_checkout_that_cannot_update_is_kept(self):
        self.tools("python", "git")
        for shell in self.each_shell():
            make_checkout(self.dir)
            r = self.run_file(shell, PRISM_DIR=str(self.dir), FAKE_PULL_EXIT="1")
            self.assertOk(r)
            self.assertIn("the copy you have is kept", r.stdout)
            self.assertEqual((self.dir / "launcher" / "prism_launcher.pyw").read_text(), "# old\n")

    def test_foreign_folder_is_refused(self):
        self.tools("python", "git")
        for shell in self.each_shell():
            self.dir.mkdir(parents=True)
            (self.dir / "thesis.tex").write_text("mine")
            r = self.run_file(shell, PRISM_DIR=str(self.dir))
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("already has other files in it, so Prism was not put there", r.stdout)
            self.assertNotIn("Prism is installed", r.stdout)
            self.assertFalse([c for c in self.calls("git") if "clone" in c])
            self.assertEqual((self.dir / "thesis.tex").read_text(), "mine")
            self.assertNotIn("Exception", r.stdout + r.stderr)

    def test_a_file_or_a_bad_name_is_refused_in_a_sentence(self):
        self.tools("python", "git")
        file = self.tmp / "a-file"
        file.write_text("x")
        for shell in self.each_shell():
            r = self.run_file(shell, PRISM_DIR=str(file))
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("is a file, so Prism was not put there", r.stdout)
            r = self.run_file(shell, PRISM_DIR="C:\\a|b")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("is not a folder name Windows accepts", r.stdout + r.stderr)

    def test_checkout_of_another_repository_is_refused(self):
        self.tools("python", "git")
        for shell in self.each_shell():
            make_checkout(self.dir, "https://github.com/someone/else.git")
            r = self.run_file(shell, PRISM_DIR=str(self.dir))
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertIn("already has other files in it", r.stdout)
            self.assertFalse([c for c in self.calls("git") if "pull" in c])

    def test_piped_into_iex(self):
        self.tools("py", "git")
        for shell in self.each_shell():
            r = self.run_iex(shell, PRISM_DIR=str(self.dir))
            self.assertOk(r)
            self.assertIn(["clone", "--quiet", REPO, str(self.dir)], self.calls("git"))
            # The user's PowerShell carries on, with its own settings and no leftover helpers.
            self.assertIn("ALIVE 0 Continue False", r.stdout)

    def test_failure_piped_into_iex_keeps_the_shell_open(self):
        self.tools("py", "git")
        for shell in self.each_shell():
            self.dir.mkdir(parents=True)
            (self.dir / "notes.txt").write_text("mine")
            r = self.run_iex(shell, PRISM_DIR=str(self.dir))
            self.assertIn("already has other files in it", r.stdout)
            self.assertIn("ALIVE 1 Continue False", r.stdout)
            self.assertEqual(r.returncode, 1)


@unittest.skipUnless(WIN and SHELLS, "needs Windows and PowerShell")
class Script(unittest.TestCase):
    def test_parses_in_every_powershell(self):
        for shell in SHELLS:
            for path in (SCRIPT, SHORTCUT):
                with self.subTest(shell=Path(shell).name, script=path.name):
                    check = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                             f"'{path}', [ref]$null, [ref]$e); if ($e) {{ $e | ForEach-Object {{ $_.ToString() }}; exit 1 }}")
                    r = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", check],
                                       capture_output=True, text=True, timeout=120)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    @unittest.skipUnless(WINPS, "needs Windows PowerShell 5.1")
    def test_windows_powershell_is_5_1(self):
        r = subprocess.run([WINPS, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.Major"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.stdout.strip(), "5")

    def test_script_is_plain_ascii(self):
        # Windows PowerShell 5.1 reads a file without a BOM in the ANSI code page.
        SCRIPT.read_bytes().decode("ascii")

    def test_shortcut_uses_the_python_it_is_given(self):
        tmp = tmpdir()
        python = tmp / "Python312" / "python.exe"
        python.parent.mkdir()
        python.write_bytes(b"")
        (python.parent / "pythonw.exe").write_bytes(b"")
        for shell in SHELLS:
            folder = tmp / Path(shell).stem
            folder.mkdir()
            r = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                str(SHORTCUT), "-HomePage", "-Folder", str(folder), "-Python", str(python)],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((folder / "Prism.lnk").is_file())
            self.assertIn(f"Python:  {python.parent / 'pythonw.exe'}", r.stdout)


if __name__ == "__main__":
    unittest.main()
