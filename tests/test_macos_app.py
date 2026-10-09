"""Native macOS bundle acceptance, without opening a browser."""
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "launcher/make_macos_app.py"


@unittest.skipUnless(sys.platform == "darwin", "requires native macOS tools")
class MacApp(unittest.TestCase):
    def test_browser_selection_scripts_compile_natively(self):
        loader = SourceFileLoader("prism_launcher_macos", str(SCRIPT.with_name("prism_launcher.pyw")))
        launcher = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(launcher)
        run = subprocess.run
        browsers = ["com.apple.Safari"]
        if any(p.is_dir() for p in (Path("/Applications/Google Chrome.app"),
                                   Path.home() / "Applications/Google Chrome.app")):
            browsers.append("com.google.Chrome")
        with tempfile.TemporaryDirectory() as tmp:
            for browser in browsers:
                # Compile the exact native scripts at the process boundary. Do not control user tabs.
                def native(args, **kwargs):
                    result = run(["/usr/bin/osacompile", "-o", str(Path(tmp) / "browser.scpt"), "-"],
                                 input=args[2], text=True, capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return subprocess.CompletedProcess(args, 0, browser if "AppKit" in args[2] else "true", "")

                with self.subTest(browser=browser), \
                        mock.patch.object(launcher.subprocess, "run", side_effect=native), \
                        mock.patch.object(launcher.webbrowser, "open") as opened:
                    launcher.open_page("http://127.0.0.1:8790/", "default")
                    opened.assert_not_called()

    def test_bundle_runs_launcher_with_quoted_paths(self):
        spec = importlib.util.spec_from_file_location("make_macos_app", SCRIPT)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve() / "Prism '论文' $HOME `quoted`"
            base.mkdir()
            launcher = base / "launcher.pyw"
            marker = base / "started.json"
            launcher.write_text("import json, os, sys\nfrom pathlib import Path\n"
                                "Path(__file__).with_name('started.json').write_text("
                                "json.dumps({'argv': sys.argv[1:], 'path': os.environ['PATH']}))\n")
            python = base / "python3"
            python.symlink_to(sys.executable)
            app = base / "prism-local.app"
            installer.build_app(app, str(python), launcher)
            info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
            self.assertEqual(info["CFBundleDisplayName"], "prism-local")
            self.assertEqual(info["CFBundleIdentifier"], "local.prismlocal.launcher")
            subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)], check=True,
                           capture_output=True, timeout=30)
            subprocess.run(["/usr/bin/osascript", str(app / "Contents/Resources/Scripts/main.scpt")],
                           check=True, capture_output=True, timeout=30)
            started = json.loads(marker.read_text())
            self.assertEqual(started["argv"], ["--home", "--browser", "default"])
            self.assertIn("/opt/homebrew/bin", started["path"].split(os.pathsep))
            self.assertIn("/Library/TeX/texbin", started["path"].split(os.pathsep))

    def test_installer_refuses_an_unrelated_existing_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            apps = Path(tmp)
            target = apps / "prism-local.app"
            (target / "Contents").mkdir(parents=True)
            info = target / "Contents/Info.plist"
            info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "example.some-other-app"}))
            marker = target / "keep.txt"
            marker.write_text("keep this application")
            result = subprocess.run([sys.executable, str(SCRIPT), "--applications", str(apps)],
                                    capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing", result.stderr)
            self.assertEqual(marker.read_text(), "keep this application")
            self.assertEqual(plistlib.loads(info.read_bytes())["CFBundleIdentifier"],
                             "example.some-other-app")


if __name__ == "__main__":
    unittest.main()
