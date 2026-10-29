"""install.sh, the one-line install for macOS and Linux, run with sh against fake programs.

Nothing is installed and nothing leaves the computer: HOME and XDG_* point into a
temporary folder, PATH starts with fake git / python3 / xcode-select / uname / open / sleep
that only write down how they were called (one line per call, arguments separated by |),
and the system Applications folder is a temporary one too. Runs wherever there is an sh:
Git Bash on Windows, any Mac or Linux. Also with dash and busybox sh when they are there.
"""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import tmpdir  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.sh"
REPO = "https://github.com/DengZhiyuan-math/Local-Ai-agent-for-latex.git"
WIN = os.name == "nt"
SH = shutil.which("sh")
# Shells to try the script with: always sh, and the strict small ones when present.
SHELLS = [s for s in (SH, shutil.which("dash")) if s]
if shutil.which("busybox"):
    SHELLS.append(shutil.which("busybox") + " sh")

# Every fake starts by writing down its call.
RECORD = """#!/bin/sh
{ printf '%s' NAME; for a in "$@"; do printf '|%s' "$a"; done; printf '\\n'; } >>"$FAKE_LOG"
"""
# Apple's /usr/bin stand-ins work only once the developer tools are there.
TIED = """if [ "${FAKE_NEEDS_DEVTOOLS:-}" = 1 ] && [ ! -f "$FAKE_STATE/devtools" ]; then exit 1; fi
"""
FAKES = {
    "git": RECORD + TIED + """
[ "$1" = -C ] && shift 2
case $1 in
    --version) echo "git version 2.47.0"; exit 0 ;;
    clone)
        if [ "${FAKE_CLONE_RC:-0}" != 0 ]; then echo "fatal: unable to access the server" >&2; exit 128; fi
        for t; do :; done
        mkdir -p "$t/.git" "$t/prism_local" "$t/launcher" "$t/bin"
        for f in prism_local/hub.py launcher/make_macos_app.py launcher/make_desktop_entry.py \\
                 launcher/prism_launcher.pyw bin/prism-home; do : >"$t/$f"; done
        exit 0 ;;
    pull) exit "${FAKE_PULL_RC:-0}" ;;
esac
exit 0
""",
    "python3": RECORD + TIED + """
case $1 in
    -c) if [ -n "${FAKE_PY_OLD:-}" ]; then exit 1; fi; exit 0 ;;
    */make_macos_app.py)
        if [ "${FAKE_SHORTCUT_RC:-0}" != 0 ]; then echo "osacompile failed" >&2; exit 1; fi
        mkdir -p "$3/prism-local.app"; echo "Installed $3/prism-local.app"; exit 0 ;;
    */make_desktop_entry.py)
        if [ "${FAKE_SHORTCUT_RC:-0}" != 0 ]; then echo "no applications folder" >&2; exit 1; fi
        exit 0 ;;
    */prism_launcher.pyw) exit "${FAKE_LAUNCH_RC:-0}" ;;
esac
exit 0
""",
    # Not installed; `--install` asks for them, and they arrive on the second check after.
    "xcode-select": RECORD + """
case $1 in
    -p)
        if [ -f "$FAKE_STATE/devtools" ]; then echo /Library/Developer/CommandLineTools; exit 0; fi
        if [ -f "$FAKE_STATE/requested" ] && [ -z "${FAKE_NEVER_INSTALLED:-}" ]; then
            read -r n <"$FAKE_STATE/requested"; n=$((n + 1)); echo "$n" >"$FAKE_STATE/requested"
            if [ "$n" -ge 2 ]; then : >"$FAKE_STATE/devtools"; fi
        fi
        exit 2 ;;
    --install)
        if [ -f "$FAKE_STATE/devtools" ]; then exit 1; fi
        echo 0 >"$FAKE_STATE/requested"; exit 0 ;;
esac
exit 1
""",
    "uname": RECORD + 'echo "${FAKE_UNAME:-Linux}"\n',
    "open": RECORD + "exit 0\n",
    "sleep": RECORD + "exit 0\n",      # the waits cost nothing, and are written down
}
# The ordinary programs install.sh and the fakes use, for a PATH without the real git/python3.
TOOLS = ("mkdir", "ls", "dirname")


def cygpath(p) -> str:
    return subprocess.run([str(Path(SH).with_name("cygpath")), "-u", str(p)],
                          capture_output=True, text=True, check=True).stdout.strip()


def posix(p) -> str:
    """The path as the shell writes it (/c/Users/... under Git Bash)."""
    return cygpath(p) if WIN else str(p)


@unittest.skipUnless(SH, "needs sh")
class InstallSh(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        for name, text in FAKES.items():
            self.fake(name, text)
        self.sys_apps = self.tmp / "Applications"       # stands in for /Applications
        self.sys_apps.mkdir()
        self.H = posix(self.home)
        self.mac_dir = self.H + "/Library/Application Support/Prism/prism-local"
        self.linux_dir = self.H + "/.local/share/prism/prism-local"

    def fake(self, name, text):
        p = self.bin / name
        with p.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text.replace("NAME", name, 1))
        p.chmod(0o755)

    def tools_dir(self) -> str:
        if WIN:     # Git's usr/bin: coreutils, and neither git nor python3
            return posix(Path(SH).parent)
        d = self.tmp / "tools"
        if not d.exists():
            d.mkdir()
            for t in TOOLS:
                (d / t).symlink_to(shutil.which(t))
        return str(d)

    def env(self, os_name, **extra):
        """A whole environment of its own: nothing of the real user's comes through."""
        e = {"PATH": posix(self.bin) + ":" + self.tools_dir(), "HOME": self.H,
             "FAKE_LOG": posix(self.log), "FAKE_STATE": posix(self.state),
             "PRISM_INSTALL_SYSTEM_APPS": posix(self.sys_apps), "LC_ALL": "C"}
        if os_name:
            e["PRISM_INSTALL_OS"] = os_name
        if WIN:
            e.update({k: os.environ[k] for k in ("SYSTEMROOT", "TEMP", "TMP") if k in os.environ})
        e.update(extra)
        return e

    def run_install(self, os_name="Darwin", shell=None, stdin_form=False, devtools=True, **env):
        """Run install.sh; return (process, calls), calls as lists [program, arg, ...]."""
        if devtools:
            (self.state / "devtools").touch()
        e = self.env(os_name, **env)
        cmd = (shell or SH).split(" ") if (shell or "").endswith(" sh") else [shell or SH]
        if stdin_form:
            with open(SCRIPT, "rb") as f:
                p = subprocess.run(cmd, stdin=f, env=e, capture_output=True, text=True, timeout=120)
        else:
            p = subprocess.run(cmd + [posix(SCRIPT)], stdin=subprocess.DEVNULL, env=e,
                               capture_output=True, text=True, timeout=120)
        lines = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return p, [line.split("|") for line in lines]

    def assertOk(self, p):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def calls_of(self, calls, *prefix):
        return [c for c in calls if c[:len(prefix)] == list(prefix)]

    # ---- macOS

    def test_mac_with_tools_downloads_adds_the_app_and_opens_it(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                self.setUp()
                p, calls = self.run_install(shell=shell)
                self.assertOk(p)
                self.assertEqual(calls[0], ["xcode-select", "-p"])   # asked before git or python3
                self.assertFalse(self.calls_of(calls, "xcode-select", "--install"))
                self.assertIn(["git", "clone", "-q", REPO, self.mac_dir], calls)
                apps = posix(self.sys_apps)
                self.assertIn(["python3", self.mac_dir + "/launcher/make_macos_app.py",
                               "--applications", apps], calls)
                self.assertIn(["open", apps + "/prism-local.app"], calls)
                self.assertFalse(self.calls_of(calls, "python3", self.mac_dir + "/launcher/prism_launcher.pyw"))
                self.assertTrue((self.home / "Library/Application Support/Prism/prism-local/.git").is_dir())
                self.assertIn("Prism is ready.", p.stdout)
                self.assertIn("Spotlight", p.stdout)

    def test_mac_without_a_writable_applications_folder_uses_the_users_own(self):
        p, calls = self.run_install(PRISM_INSTALL_SYSTEM_APPS=posix(self.tmp / "missing"))
        self.assertOk(p)
        self.assertIn(["python3", self.mac_dir + "/launcher/make_macos_app.py",
                       "--applications", self.H + "/Applications"], calls)
        self.assertIn(["open", self.H + "/Applications/prism-local.app"], calls)

    def test_mac_without_tools_installs_them_waits_and_carries_on(self):
        p, calls = self.run_install(devtools=False, FAKE_NEEDS_DEVTOOLS="1")
        self.assertOk(p)
        self.assertEqual(calls[0], ["xcode-select", "-p"])
        names = [c[:2] for c in calls]
        install = names.index(["xcode-select", "--install"])
        clone = names.index(["git", "clone"])
        self.assertLess(install, clone)
        between = calls[install + 1:clone]
        self.assertIn(["sleep", "5"], between)                            # every 5 seconds
        self.assertGreaterEqual(between.count(["xcode-select", "-p"]), 3)  # until it says yes
        self.assertIn("click Install", p.stdout)
        self.assertIn("The developer tools are installed.", p.stdout)
        self.assertIn(["open", posix(self.sys_apps) + "/prism-local.app"], calls)

    def test_mac_gives_up_waiting_with_a_sentence(self):
        p, calls = self.run_install(devtools=False, FAKE_NEEDS_DEVTOOLS="1",
                                    FAKE_NEVER_INSTALLED="1", PRISM_INSTALL_WAIT="15",
                                    PRISM_INSTALL_POLL="0")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("run this command again and click Install", p.stderr)
        self.assertEqual(self.calls_of(calls, "sleep"), [["sleep", "0"]] * 3)
        self.assertFalse(self.calls_of(calls, "git", "clone"))

    def test_mac_tools_without_a_working_python_say_how_to_repair(self):
        p, calls = self.run_install(FAKE_PY_OLD="1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("could not find Python 3.9 or newer, although Apple's developer tools", p.stderr)
        self.assertIn("python.org", p.stderr)
        self.assertFalse(self.calls_of(calls, "xcode-select", "--install"))
        self.assertFalse(self.calls_of(calls, "git", "clone"))
        # Just after the tools arrive, Python gets a few more checks before that.
        self.setUp()
        p, calls = self.run_install(devtools=False, FAKE_NEEDS_DEVTOOLS="1", FAKE_PY_OLD="1",
                                    PRISM_INSTALL_POLL="0")
        self.assertEqual(p.returncode, 1)
        self.assertIn("The developer tools are installed.", p.stdout)
        self.assertIn("python.org", p.stderr)
        after = calls[[c[:2] for c in calls].index(["xcode-select", "--install"]):]
        self.assertGreaterEqual(after.count(["sleep", "0"]), 2 + 6)

    def test_mac_python_from_elsewhere_needs_no_developer_tools(self):
        # A Homebrew or python.org python3, and a git, outside /usr/bin are used as they are.
        p, calls = self.run_install(devtools=False)
        self.assertOk(p)
        self.assertFalse(self.calls_of(calls, "xcode-select", "--install"))
        self.assertIn(["git", "clone", "-q", REPO, self.mac_dir], calls)

    def test_mac_without_shortcut_opens_home_with_the_launcher(self):
        p, calls = self.run_install(PRISM_NO_SHORTCUT="1")
        self.assertOk(p)
        self.assertFalse(self.calls_of(calls, "python3", self.mac_dir + "/launcher/make_macos_app.py"))
        self.assertFalse(self.calls_of(calls, "open"))
        self.assertIn(["python3", self.mac_dir + "/launcher/prism_launcher.pyw", "--home",
                       "--browser", "default"], calls)
        self.assertIn("prism_launcher.pyw", p.stdout)          # how to start it next time

    # ---- Linux

    def test_linux_adds_the_menu_entry_and_opens_home(self):
        p, calls = self.run_install("Linux")
        self.assertOk(p)
        self.assertFalse(self.calls_of(calls, "xcode-select"))
        self.assertIn(["git", "clone", "-q", REPO, self.linux_dir], calls)
        self.assertIn(["python3", self.linux_dir + "/launcher/make_desktop_entry.py"], calls)
        self.assertIn(["python3", self.linux_dir + "/launcher/prism_launcher.pyw", "--home",
                       "--browser", "default"], calls)
        self.assertFalse(self.calls_of(calls, "open"))
        self.assertIn("application menu", p.stdout)

    def test_linux_follows_xdg_data_home(self):
        data = posix(self.tmp / "data")
        p, calls = self.run_install("Linux", XDG_DATA_HOME=data)
        self.assertOk(p)
        self.assertIn(["git", "clone", "-q", REPO, data + "/prism/prism-local"], calls)

    def test_linux_without_git_names_the_command_and_stops(self):
        (self.bin / "git").unlink()
        for manager, command in (("apt-get", "sudo apt install python3 python3-tk git"),
                                 ("dnf", "sudo dnf install python3 python3-tkinter git"),
                                 ("pacman", "sudo pacman -S --needed python tk git"),
                                 ("zypper", "sudo zypper install python3 python3-tk git")):
            with self.subTest(manager=manager):
                for m in ("apt-get", "dnf", "pacman", "zypper"):
                    (self.bin / m).unlink(missing_ok=True)
                self.fake(manager, RECORD + "exit 0\n")
                self.log.unlink(missing_ok=True)
                p, calls = self.run_install("Linux")
                self.assertNotEqual(p.returncode, 0)
                self.assertIn("Prism needs git,", p.stderr)
                self.assertIn("  " + command + "\n", p.stderr)
                self.assertFalse(self.calls_of(calls, manager))    # named, never run
                self.assertFalse(self.calls_of(calls, "python3", self.linux_dir + "/launcher/make_desktop_entry.py"))
                self.assertFalse((self.home / ".local/share/prism").exists())

    def test_linux_with_old_python_and_no_known_manager(self):
        p, _ = self.run_install("Linux", FAKE_PY_OLD="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Prism needs Python 3.9 or newer,", p.stderr)
        self.assertIn("software manager", p.stderr)

    def test_uname_decides_without_the_override(self):
        p, calls = self.run_install(None, FAKE_UNAME="Linux")
        self.assertOk(p)
        self.assertIn(["uname", "-s"], calls)
        self.assertIn(["python3", self.linux_dir + "/launcher/make_desktop_entry.py"], calls)
        p, _ = self.run_install(None, FAKE_UNAME="MINGW64_NT-10.0")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("install.ps1", p.stderr)

    # ---- the folder

    def test_an_existing_copy_is_updated(self):
        target = self.home / ".local/share/prism/prism-local"
        (target / ".git").mkdir(parents=True)
        (target / "prism_local").mkdir()
        (target / "prism_local/hub.py").touch()
        p, calls = self.run_install("Linux")
        self.assertOk(p)
        self.assertIn(["git", "-C", self.linux_dir, "pull", "--ff-only", "-q"], calls)
        self.assertFalse(self.calls_of(calls, "git", "clone"))
        self.assertIn("Prism is up to date.", p.stdout)

    def test_an_update_that_fails_keeps_the_copy(self):
        target = self.home / ".local/share/prism/prism-local"
        (target / ".git").mkdir(parents=True)
        (target / "prism_local").mkdir()
        (target / "prism_local/hub.py").touch()
        p, calls = self.run_install("Linux", FAKE_PULL_RC="1")
        self.assertOk(p)
        self.assertIn("the copy already there is kept", p.stdout)
        self.assertIn(["python3", self.linux_dir + "/launcher/prism_launcher.pyw", "--home",
                       "--browser", "default"], calls)

    def test_a_folder_holding_something_else_is_left_alone(self):
        target = self.home / ".local/share/prism/prism-local"
        target.mkdir(parents=True)
        (target / "notes.txt").write_text("mine")
        p, calls = self.run_install("Linux")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("already something else", p.stderr)
        self.assertIn("PRISM_DIR=", p.stderr)
        self.assertFalse(self.calls_of(calls, "git", "clone"))
        self.assertFalse(self.calls_of(calls, "git", "-C"))
        self.assertEqual(sorted(os.listdir(target)), ["notes.txt"])

    def test_an_empty_folder_is_used(self):
        (self.home / ".local/share/prism/prism-local").mkdir(parents=True)
        p, calls = self.run_install("Linux")
        self.assertOk(p)
        self.assertIn(["git", "clone", "-q", REPO, self.linux_dir], calls)

    def test_a_failed_download_says_what_to_do(self):
        p, calls = self.run_install("Linux", FAKE_CLONE_RC="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("could not be downloaded. Check the internet connection", p.stderr)
        self.assertIn("Details: fatal: unable to access the server", p.stderr)
        self.assertFalse(self.calls_of(calls, "python3", self.linux_dir + "/launcher/make_desktop_entry.py"))

    def test_prism_dir_with_a_space(self):
        target = posix(self.tmp / "My Programs") + "/Prism here"
        for os_name in ("Darwin", "Linux"):
            with self.subTest(os=os_name):
                self.log.unlink(missing_ok=True)
                shutil.rmtree(self.tmp / "My Programs", ignore_errors=True)
                p, calls = self.run_install(os_name, PRISM_DIR=target)
                self.assertOk(p)
                self.assertIn(["git", "clone", "-q", REPO, target], calls)
                self.assertTrue((self.tmp / "My Programs/Prism here/prism_local/hub.py").is_file())
                entry = "make_macos_app.py" if os_name == "Darwin" else "make_desktop_entry.py"
                self.assertTrue(self.calls_of(calls, "python3", target + "/launcher/" + entry))

    # ---- settings and failures after the download

    def test_no_launch_and_no_shortcut(self):
        for os_name in ("Darwin", "Linux"):
            with self.subTest(os=os_name):
                self.log.unlink(missing_ok=True)
                p, calls = self.run_install(os_name, PRISM_NO_LAUNCH="1", PRISM_NO_SHORTCUT="1")
                self.assertOk(p)
                ran = [c[1].rsplit("/", 1)[-1] for c in self.calls_of(calls, "python3") if c[1] != "-c"]
                self.assertEqual(ran, [])
                self.assertFalse(self.calls_of(calls, "open"))

    def test_no_launch_keeps_the_shortcut(self):
        p, calls = self.run_install("Linux", PRISM_NO_LAUNCH="1")
        self.assertOk(p)
        ran = [c[1].rsplit("/", 1)[-1] for c in self.calls_of(calls, "python3") if c[1] != "-c"]
        self.assertEqual(ran, ["make_desktop_entry.py"])

    def test_a_failed_shortcut_still_opens_home_and_says_so(self):
        p, calls = self.run_install(FAKE_SHORTCUT_RC="1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("could not be added to", p.stdout)
        self.assertIn("osacompile failed", p.stdout)
        self.assertFalse(self.calls_of(calls, "open"))
        self.assertIn(["python3", self.mac_dir + "/launcher/prism_launcher.pyw", "--home",
                       "--browser", "default"], calls)
        self.assertIn("To start Prism, run:", p.stderr)

    # ---- as `curl ... | sh` runs it

    def test_from_standard_input(self):
        for shell in SHELLS:
            for os_name in ("Darwin", "Linux"):
                with self.subTest(shell=shell, os=os_name):
                    self.setUp()
                    p, calls = self.run_install(os_name, shell=shell, stdin_form=True,
                                                devtools=os_name == "Linux", FAKE_NEEDS_DEVTOOLS="1",
                                                PRISM_INSTALL_POLL="0")
                    self.assertOk(p)
                    self.assertIn("Prism is ready.", p.stdout)
                    self.assertTrue(self.calls_of(calls, "git", "clone"))

    def test_a_download_cut_short_does_nothing(self):
        # Everything is in main, called on the last line: any shorter part of the script
        # only defines things, or is a syntax error.
        text = SCRIPT.read_bytes()
        last = text.rstrip(b"\n").rfind(b"\n") + 1                # where `main "$@"` starts
        self.assertEqual(text[last:], b'main "$@"\n')
        for cut in sorted({last * i // 10 for i in range(1, 11)} | {last + 3}):
            with self.subTest(cut=cut):
                part = self.tmp / "part.sh"
                part.write_bytes(text[:cut])
                with open(part, "rb") as f:
                    subprocess.run([SH], stdin=f, env=self.env("Linux"), capture_output=True,
                                   timeout=60)
                self.assertFalse(self.log.exists(), self.log.read_text() if self.log.exists() else "")
                self.assertFalse((self.home / ".local").exists())

    def test_syntax(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                cmd = shell.split(" ") if shell.endswith(" sh") else [shell]
                p = subprocess.run(cmd + ["-n", posix(SCRIPT)], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn(b"\r", SCRIPT.read_bytes())          # CRLF would break sh


if __name__ == "__main__":
    unittest.main()
