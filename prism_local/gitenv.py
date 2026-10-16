"""Running git: the one way prism-local starts it.

Repository discovery stays local to the cwd (no GIT_DIR and the like from the
environment), git never asks on a terminal or waits for input, never opens a login
window unless you started it with a button (see NO_PROMPTS), never holds an optional
lock such as .git/index.lock (the editor asks every two seconds, and a git command of
yours must never find it taken), never opens a console window, always ends within its
timeout, and its output is UTF-8 whatever the system's code page (paths like 练习/).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import proc

SELECTORS = frozenset(("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                      "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                      "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
                      "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
                      "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_NAMESPACE"))


def selects_repository(name: str) -> bool:
    name = name.upper()
    return name in SELECTORS or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))


def git_env(base: dict | None = None) -> dict:
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if not selects_repository(k)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


# Git run in the background (the sync every few seconds, the Home page's fetch) must not
# open a login window nobody asked for. Git Credential Manager still hands over the
# credentials it has saved, but asks for none, and no askpass program runs, git's (an
# empty GIT_ASKPASS also hides core.askPass and SSH_ASKPASS from git) or ssh's. A missing
# login is then an error the page shows.
NO_PROMPTS = {"GCM_INTERACTIVE": "never", "GIT_ASKPASS": "", "SSH_ASKPASS_REQUIRE": "never"}


def git(cwd: str | Path, *args: str, timeout: float = 60, binary: bool = False,
        check: bool = False, interactive: bool = False) -> subprocess.CompletedProcess:
    """`git args` in `cwd`. Output is str (UTF-8, undecodable bytes replaced), or bytes with
    binary=True. Raises subprocess.TimeoutExpired after `timeout` seconds, OSError when git
    cannot start, and subprocess.CalledProcessError on failure with check=True; otherwise
    the caller reads returncode. Only with interactive=True (you pressed a button) may a
    credential helper ask you to log in."""
    text = {} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}
    env = git_env() if interactive else {**git_env(), **NO_PROMPTS}
    return proc.run(["git", "--no-optional-locks", *args], cwd=cwd, stdin=subprocess.DEVNULL,
                    timeout=timeout, check=check, env=env, **text)
