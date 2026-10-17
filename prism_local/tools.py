"""The programs Prism needs besides itself, git and the GitHub CLI (gh): whether they are
there, and installing them from the Home page for someone who has never used either.

On Windows Prism installs them itself with winget (each asks Windows' permission once, as
any installer does). Elsewhere, or without winget, the page says in a sentence how to.
A program installed while Prism runs is found without a restart: the PATH Windows keeps
(in the registry) is read again (refresh_path).

    status()          # {"git", "gh": path or None, "can_install", "installing", "how"}
    install("git")    # in the background; status()["installing"]["git"] says how it goes
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import proc
from errors import Refused, say

PACKAGES = {"git": "Git.Git", "gh": "GitHub.cli"}          # winget's ids
LABELS = {"git": "git", "gh": "The GitHub CLI"}
INSTALL_TIME = 900                                          # a slow download, an installer
# Where their installers put them, also when Windows' PATH has not reached this process.
KNOWN_DIRS = {"git": [r"%ProgramFiles%\Git\cmd", r"%LOCALAPPDATA%\Programs\Git\cmd"],
              "gh": [r"%ProgramFiles%\GitHub CLI", r"%LOCALAPPDATA%\Programs\GitHub CLI"]}

INSTALL: dict[str, dict] = {}       # program -> {"state": running|done|failed, "error", "details"}
_lock = threading.Lock()


def refresh_path() -> None:
    """Windows: what Windows' PATH says now (the system's and yours, in the registry), and
    where git and gh install themselves, added to this process's PATH: a program installed
    since Prism started is found without restarting it."""
    if os.name != "nt":
        return
    parts = _registry_path() + [os.path.expandvars(d) for dirs in KNOWN_DIRS.values() for d in dirs]
    have = os.environ.get("PATH", "").split(os.pathsep)
    seen = {os.path.normcase(p.rstrip("\\/")) for p in have if p}
    add = []
    for p in parts:
        key = os.path.normcase(p.strip().rstrip("\\/"))
        if key and key not in seen and Path(p.strip()).is_dir():
            seen.add(key)
            add.append(p.strip())
    if add:
        os.environ["PATH"] = os.pathsep.join([*have, *add])


def _registry_path() -> list[str]:
    """The folders of Windows' PATH as it is now: the system's, then yours."""
    parts: list[str] = []
    try:
        import winreg
    except ImportError:
        return parts
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(hive, key) as k:
                parts += os.path.expandvars(str(winreg.QueryValueEx(k, "Path")[0])).split(";")
        except OSError:
            pass
    return parts


def find(name: str) -> str | None:
    """Where the program `name` ("git", "gh") is, or None."""
    refresh_path()
    return shutil.which(name)


def winget() -> str | None:
    return shutil.which("winget") if os.name == "nt" else None


def how_to(name: str) -> str:
    """How to install `name` by hand, in a sentence, where Prism cannot."""
    if os.name == "nt":
        return {"git": "Install git from https://git-scm.com/download/win, then come back here.",
                "gh": "Install the GitHub CLI from https://cli.github.com, then come back here."}[name]
    if sys.platform == "darwin":
        return {"git": "Open Terminal and run: xcode-select --install (Apple's developer tools include git).",
                "gh": "Install the GitHub CLI from https://cli.github.com (or, with Homebrew: brew install gh)."}[name]
    return {"git": "Install git with your system's software installer (the package is called git).",
            "gh": "Install the GitHub CLI from https://cli.github.com (the package is called gh)."}[name]


def status() -> dict:
    """What the Home page's Getting started panel shows."""
    with _lock:
        installing = {k: dict(v) for k, v in INSTALL.items()}
    return {"git": find("git"), "gh": find("gh"), "can_install": bool(winget()),
            "installing": installing, "how": {name: how_to(name) for name in PACKAGES}}


def install(name: str) -> dict:
    """Install `name` ("git", "gh") in the background, with winget; its state at once.
    Refused, saying how to install it by hand, where Prism cannot."""
    if name not in PACKAGES:
        raise Refused("unknown program")
    # Claimed with the same look that finds it free: two clicks at once start one install.
    with _lock:
        if INSTALL.get(name, {}).get("state") == "running":
            return dict(INSTALL[name])
        before = INSTALL.get(name)
        INSTALL[name] = {"state": "running", "error": None, "details": None, "at": time.time()}
        running = dict(INSTALL[name])
    try:
        if find(name):
            with _lock:
                INSTALL[name] = {"state": "done", "error": None, "details": None}
                return dict(INSTALL[name])
        exe = winget()
    except BaseException:
        _give_back(name, before)
        raise
    if not exe:
        _give_back(name, before)
        raise Refused(how_to(name))
    threading.Thread(target=_install, args=(name, exe), daemon=True).start()
    return running


def _give_back(name: str, before: dict | None) -> None:
    """Nothing was started after all: `name`'s state as it was before install() claimed it."""
    with _lock:
        if before is None:
            INSTALL.pop(name, None)
        else:
            INSTALL[name] = before


def _install(name: str, exe: str) -> None:
    label = LABELS[name]
    try:
        r = proc.run([exe, "install", "--id", PACKAGES[name], "--exact", "--silent",
                      "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"],
                     timeout=INSTALL_TIME, text=True, encoding="utf-8", errors="replace")
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if find(name):
            result = {"state": "done", "error": None, "details": None}
        else:
            result = {"state": "failed", "details": out[-600:] or f"winget exited with {r.returncode}",
                      "error": f"{label} could not be installed. Try again, or: " + how_to(name)}
    except (OSError, subprocess.SubprocessError) as e:
        result = {"state": "failed", "error": f"{label} could not be installed. " + how_to(name), "details": say(e)}
    if name == "gh" and result["state"] == "done":
        import github
        github.GITHUB.status(refresh=True)
    with _lock:
        INSTALL[name] = result
