#!/usr/bin/env python3
"""macOS: install prism-local.app in Applications for Finder and Spotlight.

    python3 launcher/make_macos_app.py
    python3 launcher/make_macos_app.py --applications ~/Applications

Uses the existing launcher, Python and repository in place. Standard library
and macOS tools only; keep the repository and its volume available.
"""
from __future__ import annotations

import argparse
import json
import plistlib
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE_ID = "local.prismlocal.launcher"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")


def build_app(app: Path, python: str, launcher: Path) -> None:
    """Build and sign the native applet that opens the existing Home launcher."""
    command = ('export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:'
               '/usr/local/bin:/Library/TeX/texbin:/usr/bin:/bin:/usr/sbin:/sbin"; exec ' +
               shlex.join([python, str(launcher), "--home", "--browser", "default"]))
    source = ('on run\n    try\n        do shell script ' + json.dumps(command, ensure_ascii=False) +
              '\n    on error errorMessage\n        display alert "prism-local could not start" '
              'message errorMessage as critical\n    end try\nend run\n')
    subprocess.run(["/usr/bin/osacompile", "-o", str(app), "-"], input=source,
                   text=True, capture_output=True, check=True)
    plist = app / "Contents/Info.plist"
    info = plistlib.loads(plist.read_bytes())
    # osacompile supplies generic privacy messages for unrelated applet capabilities.
    info = {key: value for key, value in info.items() if not key.endswith("UsageDescription")}
    info.update({"CFBundleName": "prism-local", "CFBundleDisplayName": "prism-local",
                 "CFBundleIdentifier": BUNDLE_ID, "CFBundleShortVersionString": "1.0",
                 "CFBundleVersion": "1", "LSApplicationCategoryType": "public.app-category.productivity",
                 "NSAppleEventsUsageDescription": "Open the Prism Home page in your browser."})
    plist.write_bytes(plistlib.dumps(info))
    subprocess.run(["/usr/bin/sips", "-s", "format", "icns", str(HERE / "prism.ico"), "--out",
                    str(app / "Contents/Resources/applet.icns")], capture_output=True, check=True)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)],
                   capture_output=True, check=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(app)],
                   capture_output=True, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Install prism-local.app for Finder and Spotlight.")
    ap.add_argument("--applications", type=Path, default=Path("/Applications"),
                    help="installation folder (default: /Applications; ~/Applications also works)")
    a = ap.parse_args()
    if sys.platform != "darwin":
        ap.error("This installer requires macOS.")
    apps = a.applications.expanduser().resolve()
    apps.mkdir(parents=True, exist_ok=True)
    target = apps / "prism-local.app"
    with tempfile.TemporaryDirectory(prefix=".prism-local-", dir=apps) as tmp:
        app = Path(tmp) / "prism-local.app"
        build_app(app, sys.executable, HERE / "prism_launcher.pyw")
        if target.is_symlink():
            ap.error(f"Refusing to replace a symbolic link: {target}")
        if target.exists():
            info = plistlib.loads((target / "Contents/Info.plist").read_bytes())
            if info.get("CFBundleIdentifier") != BUNDLE_ID:
                ap.error(f"Refusing to replace another application: {target}")
        previous = Path(tmp) / "previous.app"
        if target.exists():
            target.rename(previous)
        try:
            app.rename(target)
        except OSError:
            if previous.exists():
                previous.rename(target)
            raise
    subprocess.run([LSREGISTER, "-f", str(target)], capture_output=True, check=True)
    subprocess.run(["/usr/bin/mdimport", str(target)], capture_output=True, check=True)
    print(f"Installed {target}\nSearch for prism-local in Spotlight (Command-Space).")
    print(f"Keep the repository at {HERE.parent}; rerun this installer if you move it.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        sys.exit(f"Could not install prism-local: {getattr(e, 'stderr', None) or e}")
