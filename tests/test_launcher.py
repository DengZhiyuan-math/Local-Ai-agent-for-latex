"""Unit tests for how launcher/prism_launcher.pyw starts the browser."""
import importlib.util
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / "launcher" / "prism_launcher.pyw"
loader = SourceFileLoader("prism_launcher", str(PATH))
spec = importlib.util.spec_from_loader("prism_launcher", loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)

URL = "http://127.0.0.1:9544/"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class BrowserCommand(unittest.TestCase):
    def test_window_mode_opens_a_new_normal_window(self):
        self.assertEqual(launcher.browser_command(URL, "window", CHROME),
                         [CHROME, "--new-window", URL])

    def test_app_mode_opens_an_app_window(self):
        self.assertEqual(launcher.browser_command(URL, "app", CHROME), [CHROME, f"--app={URL}"])

    def test_default_mode_uses_the_default_browser(self):
        self.assertIsNone(launcher.browser_command(URL, "default", CHROME))

    def test_without_chrome_or_edge_falls_back(self):
        self.assertIsNone(launcher.browser_command(URL, "window", None))

    def test_window_is_the_default_mode(self):
        self.assertIn('default="window"', PATH.read_text(encoding="utf-8"))


class MacBrowser(unittest.TestCase):
    def test_repeated_home_launch_selects_existing_page(self):
        with mock.patch.object(launcher.sys, "platform", "darwin"), \
                mock.patch.object(launcher.sys, "argv", [str(PATH), "--home", "--browser", "default"]), \
                mock.patch.object(launcher, "ensure_server", return_value={"url": URL, "started": False}), \
                mock.patch.object(launcher.subprocess, "run", side_effect=[
                    mock.Mock(returncode=0, stdout="com.google.Chrome\n"),
                    mock.Mock(returncode=0, stdout="true\n"),
                ] * 2), \
                mock.patch.object(launcher, "chromium_browser", return_value=None), \
                mock.patch.object(launcher.webbrowser, "open") as opened:
            self.assertEqual(launcher.main(), 0)
            self.assertEqual(launcher.main(), 0)
        opened.assert_not_called()

    def test_unavailable_page_opens_in_default_browser(self):
        for replies in ([mock.Mock(returncode=0, stdout="com.apple.Safari"),
                         mock.Mock(returncode=0, stdout="false")],
                        [mock.Mock(returncode=0, stdout="org.mozilla.firefox")],
                        [mock.Mock(returncode=0, stdout="com.google.Chrome"),
                         mock.Mock(returncode=1, stdout="")],
                        [OSError("osascript unavailable")]):
            with self.subTest(replies=replies), \
                    mock.patch.object(launcher.sys, "platform", "darwin"), \
                    mock.patch.object(launcher.subprocess, "run", side_effect=replies), \
                    mock.patch.object(launcher, "chromium_browser", return_value=None), \
                    mock.patch.object(launcher.webbrowser, "open") as opened:
                launcher.open_page(URL, "default")
                opened.assert_called_once_with(URL)

    def test_none_mode_does_not_access_browser(self):
        with mock.patch.object(launcher.sys, "platform", "darwin"), \
                mock.patch.object(launcher.subprocess, "run") as native, \
                mock.patch.object(launcher.webbrowser, "open") as opened:
            launcher.open_page(URL, "none")
        native.assert_not_called()
        opened.assert_not_called()


class LinuxBrowser(unittest.TestCase):
    """Which browser the launcher uses on Linux (tested on any system with mocks)."""

    def choose(self, default, installed):
        which = lambda cmd: f"/usr/bin/{cmd}" if cmd in installed else None  # noqa: E731
        with mock.patch.object(launcher, "WIN", False), \
                mock.patch.object(launcher, "linux_default_browser", return_value=default), \
                mock.patch.object(launcher.shutil, "which", side_effect=which):
            return launcher.chromium_browser()

    def test_the_default_browser_when_it_is_chromium_based(self):
        self.assertEqual(self.choose("chromium_chromium.desktop", {"google-chrome", "chromium"}),
                         "/usr/bin/chromium")
        self.assertEqual(self.choose("google-chrome.desktop", {"google-chrome-stable"}),
                         "/usr/bin/google-chrome-stable")

    def test_firefox_as_the_default_means_the_default_browser(self):
        self.assertIsNone(self.choose("firefox.desktop", {"google-chrome"}))

    def test_unknown_default_takes_the_first_installed(self):
        self.assertEqual(self.choose("", {"brave-browser"}), "/usr/bin/brave-browser")
        self.assertIsNone(self.choose("", set()))

    def test_error_dialogs_where_there_is_no_console(self):
        with mock.patch.object(launcher.sys, "platform", "linux"), \
                mock.patch.object(launcher.shutil, "which", side_effect=lambda c: c in ("zenity", "notify-send") and c):
            cmds = launcher.dialog_commands("It broke", True)
        self.assertEqual([c[0] for c in cmds], ["zenity", "notify-send"])
        self.assertIn("--text=It broke", cmds[0])


ENTRY_PATH = Path(__file__).resolve().parents[1] / "launcher" / "make_desktop_entry.py"
entry_loader = SourceFileLoader("make_desktop_entry", str(ENTRY_PATH))
make_entry = importlib.util.module_from_spec(importlib.util.spec_from_loader("make_desktop_entry",
                                                                              entry_loader))
entry_loader.exec_module(make_entry)


class DesktopEntry(unittest.TestCase):
    def test_exec_arguments_are_quoted_as_the_spec_says(self):
        q = make_entry.quote
        self.assertEqual(q("/usr/bin/python3"), "/usr/bin/python3")
        self.assertEqual(q("/home/me/My Papers/论文"), '"/home/me/My Papers/论文"')
        self.assertEqual(q('say "hi" $HOME'), '"say \\\\"hi\\\\" \\\\$HOME"')
        self.assertEqual(q("100%"), '"100%%"')

    def test_icon_and_entry(self):
        png = make_entry.ico_png(Path(__file__).resolve().parents[1] / "launcher" / "prism.ico")
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(int.from_bytes(png[16:20], "big"), 256, "the 256-pixel image")
        text = make_entry.entry("Prism · 论文", "Open it", ["/usr/bin/python3", "/opt/p/l.pyw", "--home"],
                                Path("/icons/p.png"), Path("/home/me"))
        self.assertTrue(text.startswith("[Desktop Entry]\nType=Application\n"))
        self.assertIn("Exec=/usr/bin/python3 /opt/p/l.pyw --home\n", text)
        self.assertIn("Name=Prism · 论文\n", text)
        self.assertIn("Terminal=false\n", text)


if __name__ == "__main__":
    unittest.main()
