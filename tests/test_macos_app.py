"""Native macOS bundle acceptance, without opening a browser."""
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "launcher/make_macos_app.py"


@unittest.skipUnless(sys.platform == "darwin", "requires native macOS tools")
class MacApp(unittest.TestCase):
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
