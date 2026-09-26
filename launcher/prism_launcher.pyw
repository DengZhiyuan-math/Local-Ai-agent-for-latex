#!/usr/bin/env python3
"""Prism launcher: open a LaTeX project, or the Home page, with one click.

    pythonw launcher/prism_launcher.pyw PROJECT_DIR [--browser window|app|default|none] [--port N]
    pythonw launcher/prism_launcher.pyw --home      [--browser window|app|default|none]

1. If prism-local already runs for PROJECT_DIR, open another page on it.
2. Otherwise start prism-local with --exit-when-idle, wait until it answers,
   and open the page (by default in a new Chrome/Edge/Chromium window of its
   own, where the pop-out PDF opens as a second tab).
3. Exit. The server runs on its own and stops shortly after its last page is
   closed, so no launcher process stays behind.

--home (or no PROJECT_DIR) does the same for the Home page (prism_local/hub.py),
which lists your projects and opens each one in its own prism-local server.

Run it with pythonw on Windows so no console window appears; on Linux the
desktop entries of make_desktop_entry.py run it with python3. The server's
output goes to a log file in the state folder (%LOCALAPPDATA%\\prism-local\\logs,
~/.local/state/prism-local/logs). Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent / "prism_local"
SERVER = PKG / "server.py"
HUB = PKG / "hub.py"
sys.path.insert(0, str(PKG))
from registry import WIN, ensure_server  # noqa: E402

QUIET = False


# ---------------------------------------------------------------- helpers

def message(text: str, error: bool = True) -> None:
    """Tell the user something went wrong. Started from a shortcut or a desktop menu there
    is no console to print to, so this is a dialog wherever one can be shown."""
    if sys.stderr is not None:
        print(f"prism-launcher: {text}", file=sys.stderr, flush=True)
    if QUIET:
        return
    if WIN:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Prism", 0x10 if error else 0x40)
        return
    for cmd in dialog_commands(text, error):
        try:
            if subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=600).returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            continue
    try:                                                    # python3-tk, where installed
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        (messagebox.showerror if error else messagebox.showinfo)("Prism", text)
        root.destroy()
    except Exception:  # noqa: BLE001 — no display, no tkinter: stderr is all there is
        pass


def dialog_commands(text: str, error: bool) -> list[list[str]]:
    """Programs that show a message on Linux and macOS, most likely ones first."""
    if sys.platform == "darwin":
        import json
        return [["osascript", "-e", f"display alert \"Prism\" message {json.dumps(text)}"
                 + (" as critical" if error else "")]]
    cmds = []
    if shutil.which("zenity"):
        cmds.append(["zenity", "--error" if error else "--info", "--title=Prism", "--no-markup",
                     f"--text={text}"])
    if shutil.which("kdialog"):
        cmds.append(["kdialog", "--title", "Prism", "--error" if error else "--msgbox", text])
    if shutil.which("notify-send"):
        cmds.append(["notify-send", "--app-name=Prism"] + (["--urgency=critical"] if error else [])
                    + ["Prism", text])
    return cmds


# ---------------------------------------------------------------- browser

def default_browser_progid() -> str:
    try:
        import winreg
        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            return winreg.QueryValueEx(k, "ProgId")[0]
    except OSError:
        return ""


# Chromium-family browsers on Linux, by command, and the desktop-file names that mark
# one of them as the default browser.
LINUX_CHROMIUM = [("google-chrome", ("google-chrome",)), ("google-chrome-stable", ("google-chrome",)),
                  ("chromium", ("chromium",)), ("chromium-browser", ("chromium",)),
                  ("microsoft-edge", ("microsoft-edge",)), ("microsoft-edge-stable", ("microsoft-edge",)),
                  ("brave-browser", ("brave",)), ("vivaldi-stable", ("vivaldi",)),
                  ("vivaldi", ("vivaldi",))]


def linux_default_browser() -> str:
    """The desktop file of the default browser ("firefox.desktop" …), or "" if unknown."""
    try:
        r = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True,
                           text=True, timeout=10)
        return r.stdout.strip().lower() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def chromium_browser() -> str | None:
    """The Chromium-family browser to open Prism in, or None for the default browser.

    Windows: Edge or Chrome, whichever is the default browser; Edge first otherwise.
    Linux: the default browser if it is Chromium-based; none if another browser (such as
    Firefox) is the default; when the default is unknown, the first one installed."""
    if not WIN:
        default = linux_default_browser()
        installed = [(shutil.which(cmd), keys) for cmd, keys in LINUX_CHROMIUM]
        installed = [(exe, keys) for exe, keys in installed if exe]
        if default:
            return next((exe for exe, keys in installed if any(k in default for k in keys)), None)
        return installed[0][0] if installed else None
    progid = default_browser_progid().lower()
    if "firefox" in progid:
        return None                 # use the ordinary default browser
    edge, chrome = [], []
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            edge.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
            chrome.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    order = chrome + edge if "chrome" in progid else edge + chrome
    return next((str(p) for p in order if p.exists()), None)


def browser_command(url: str, mode: str, exe: str | None) -> list[str] | None:
    """How to start Chrome/Edge for `mode`, or None for the default browser.

    window: a new ordinary window with a tab strip, holding only Prism; the pop-out
            PDF (window.open) then opens as a tab in that same window.
    app:    an app window without tabs or address bar; the pop-out PDF gets its own
            app window.
    """
    if not exe or mode not in ("window", "app"):
        return None
    return [exe, "--new-window", url] if mode == "window" else [exe, f"--app={url}"]


def open_page(url: str, mode: str) -> None:
    if mode == "none":
        return
    cmd = browser_command(url, mode, chromium_browser())
    if cmd:
        # A session of its own: closing the terminal the launcher ran in leaves it open.
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         **({} if WIN else {"start_new_session": True}))
        return
    if WIN:
        os.startfile(url)
    else:
        webbrowser.open(url)


# ---------------------------------------------------------------- main

def main() -> int:
    global QUIET
    ap = argparse.ArgumentParser(description="Open a LaTeX project, or the Home page, in prism-local.")
    ap.add_argument("project", type=Path, nargs="?",
                    help="LaTeX project directory (omit it, or pass --home, for the Home page)")
    ap.add_argument("--home", action="store_true", help="open the Home page with your projects")
    ap.add_argument("--browser", choices=("window", "app", "default", "none"), default="window",
                    help="window: new Chrome/Edge/Chromium window of its own (default); app: app window "
                         "without tabs; default: a tab in the default browser")
    ap.add_argument("--port", type=int, help="preferred port (default: stable per project)")
    ap.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)  # no dialogs (tests)
    ap.add_argument("--idle-timings", help=argparse.SUPPRESS)                # passed to server
    a = ap.parse_args()
    QUIET = a.quiet
    extra = ["--idle-timings", a.idle_timings] if a.idle_timings else []

    if a.home or a.project is None:
        if not HUB.is_file():
            message(f"prism-local Home page not found:\n{HUB}")
            return 2
        project, what = None, "the Home page"
    else:
        project = a.project.expanduser().resolve()
        if not project.is_dir():
            message(f"Project folder not found:\n{project}")
            return 2
        if not SERVER.is_file():
            message(f"prism-local server not found:\n{SERVER}")
            return 2
        what = str(project)

    # Run with pythonw there is no console: whatever goes wrong must end in a dialog.
    try:
        r = ensure_server(project, a.port, extra)
        if "error" in r:
            message(f"prism-local did not start for\n{what}\n\n{r['error']}\n\n{r['log']}"
                    f"\n\nLog: {r['logfile']}")
            return 1
        open_page(r["url"], a.browser)
    except Exception as e:  # noqa: BLE001
        message(f"prism-local could not be opened for\n{what}\n\n{type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
