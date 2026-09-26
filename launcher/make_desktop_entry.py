#!/usr/bin/env python3
"""Linux: put Prism in the application menu, and on the desktop if you like.

    python3 launcher/make_desktop_entry.py                        # "Prism": the Home page
    python3 launcher/make_desktop_entry.py --project ~/papers/my-paper
    python3 launcher/make_desktop_entry.py --project ~/papers/my-paper --name "My paper"
    python3 launcher/make_desktop_entry.py --desktop              # also an icon on the desktop
    python3 launcher/make_desktop_entry.py --browser app          # window (default), app, default
    python3 launcher/make_desktop_entry.py --remove               # take an entry away again

Writes a freedesktop.org desktop entry to ~/.local/share/applications, where GNOME, KDE,
Xfce and the other desktops find it, that runs launcher/prism_launcher.pyw with the Python
running this script. The icon is the 256-pixel image inside launcher/prism.ico. This is
the Linux counterpart of make-shortcut.ps1. Standard library only.
"""
from __future__ import annotations

import argparse
import os
import re
import struct
import subprocess
import sys
from pathlib import Path
from shutil import which

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "prism_launcher.pyw"
ICO = HERE / "prism.ico"
sys.path.insert(0, str(HERE.parent / "prism_local"))
from registry import project_key  # noqa: E402


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def desktop_dir() -> Path | None:
    """The user's desktop folder (xdg-user-dir DESKTOP), if there is one."""
    try:
        d = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True,
                           timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        d = ""
    p = Path(d) if d else Path.home() / "Desktop"
    return p if p.is_dir() and p != Path.home() else None


def ico_png(ico: Path) -> bytes:
    """The largest PNG image inside an .ico file."""
    data = ico.read_bytes()
    count = struct.unpack_from("<HHH", data, 0)[2]
    best = None
    for i in range(count):
        w, _, _, _, _, _, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        img = data[offset:offset + size]
        if img.startswith(b"\x89PNG") and (best is None or (w or 256) > best[0]):
            best = (w or 256, img)
    if best is None:
        raise ValueError(f"{ico} holds no PNG image")
    return best[1]


def quote(arg: str) -> str:
    """One argument of an Exec line, quoted as the Desktop Entry Specification says:
    double quotes with \\ before " ` $ and \\, then every \\ doubled for the file's own
    escaping, and % written as %%."""
    q = arg if re.fullmatch(r"[A-Za-z0-9_@+=:,./-]+", arg) else \
        '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    return q.replace("\\", "\\\\").replace("%", "%%")


def entry(name: str, comment: str, argv: list[str], icon: Path, workdir: Path | None) -> str:
    one_line = lambda s: s.replace("\n", " ").strip()  # noqa: E731
    lines = ["[Desktop Entry]", "Type=Application", "Version=1.0", f"Name={one_line(name)}",
             f"Comment={one_line(comment)}", "Exec=" + " ".join(quote(a) for a in argv),
             f"Icon={icon}", "Terminal=false", "Categories=Office;Publishing;",
             "StartupNotify=false"]
    if workdir:
        lines.append(f"Path={workdir}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Add Prism to the Linux application menu.")
    ap.add_argument("--project", type=Path, help="open this LaTeX project directly "
                                                 "(default: the Home page with all projects)")
    ap.add_argument("--name", help="the name in the menu (default: Prism, or Prism · <folder>)")
    ap.add_argument("--browser", choices=("window", "app", "default"), default="window",
                    help="window: a Chrome/Chromium window of its own (default); app: an app "
                         "window without tabs; default: a tab in the default browser")
    ap.add_argument("--desktop", action="store_true", help="also put an icon on the desktop")
    ap.add_argument("--remove", action="store_true", help="remove the entry instead")
    a = ap.parse_args()
    if os.name == "nt":
        sys.exit("On Windows, use launcher\\make-shortcut.ps1 instead.")

    if a.project:
        project = a.project.expanduser().resolve()
        if not project.is_dir():
            sys.exit(f"not a folder: {project}")
        file = f"prism-local-{project_key(project)}.desktop"
        name = a.name or f"Prism · {project.name}"
        comment = f"Open {project} in prism-local"
        argv = [sys.executable, str(LAUNCHER), str(project), "--browser", a.browser]
    else:
        project, file, name = None, "prism-local.desktop", a.name or "Prism"
        comment = "Open the Prism Home page with your LaTeX projects"
        argv = [sys.executable, str(LAUNCHER), "--home", "--browser", a.browser]

    apps, desk = data_home() / "applications", desktop_dir()
    targets = [apps / file] + ([desk / file] if a.desktop and desk else [])
    if a.remove:
        for t in [apps / file] + ([desk / file] if desk else []):
            if t.exists():
                t.unlink()
                print(f"Removed {t}")
        return 0

    icon = data_home() / "icons" / "hicolor" / "256x256" / "apps" / "prism-local.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes(ico_png(ICO))
    text = entry(name, comment, argv, icon, project or Path.home())
    for t in targets:
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(text, encoding="utf-8")
        t.chmod(0o755)                   # desktops launch only executable desktop files
        if t.parent == desk and which("gio"):     # else GNOME asks to "Allow Launching"
            subprocess.run(["gio", "set", str(t), "metadata::trusted", "true"],
                           capture_output=True, check=False)
        print(f"Created {t}")
    if which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(apps)], capture_output=True, check=False)
    if a.desktop and not desk:
        print("No desktop folder was found; only the menu entry was created.")
    print(f"Python:  {sys.executable}")
    print(f"Project: {project}" if project else "Home page")
    return 0


if __name__ == "__main__":
    sys.exit(main())
