"""GitHub itself, as prism-local asks it things: whether you are logged in, a new private
repository, protection for its branch. Through the GitHub CLI, `gh` (GhCli); the tests put
a stand-in of their own in GITHUB (one with local bare repositories).

    GITHUB.status()                 # {"gh", "logged_in", "account", "error"?}
    GITHUB.create(root, "me/paper") # None, or why not: a private repository, as root's origin
    GITHUB.protect("me/paper", "main")   # what became of the branch's protection, in words
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from gitenv import git_env
from proc import NO_WINDOW

STATUS_TTL = 120         # seconds gh's login is not asked again


class GhCli:
    """GitHub through the GitHub CLI (gh), logged in by `gh auth login`."""

    def __init__(self):
        self._status: tuple[float, dict] | None = None

    def status(self, refresh: bool = False) -> dict:
        """Whether gh is installed and logged in, and as whom."""
        if self._status and not refresh and time.time() - self._status[0] < STATUS_TTL:
            return self._status[1]
        exe = shutil.which("gh")
        status = {"gh": exe, "logged_in": False, "account": None}
        if not exe:
            status["error"] = "GitHub CLI not found. Install it from https://cli.github.com, then run: gh auth login"
        else:
            try:
                r = self._gh(exe, "auth", "status", "--hostname", "github.com", timeout=20)
                m = re.search(r"Logged in to github\.com (?:account|as) (\S+)", r.stdout + r.stderr)
                if m and r.returncode == 0:
                    status.update(logged_in=True, account=m.group(1).strip("()"))
                else:
                    status["error"] = "GitHub CLI is not logged in. Run: gh auth login"
            except (OSError, subprocess.SubprocessError) as e:
                status["error"] = f"gh auth status failed: {e}"
        self._status = (time.time(), status)
        return status

    def create(self, root: Path, full: str) -> str | None:
        """A new private repository `full` ("owner/name", or "name" for your own), added
        to the repository at `root` as its origin. None, or why it was not created."""
        try:
            r = self._gh(self.status()["gh"], "repo", "create", full, "--private", "--source", ".",
                         "--remote", "origin", cwd=root, timeout=180, env=git_env())
        except (OSError, subprocess.SubprocessError) as e:
            return str(e)
        return None if r.returncode == 0 else ((r.stderr or r.stdout).strip()[:500] or "gh repo create failed")

    def protect(self, full: str, branch: str) -> str:
        """Ask GitHub to refuse force pushes to, and deletion of, `branch`, for every
        co-author whatever tool they use. GitHub offers this for private repositories only
        on paid plans (Pro, Team …). Returns what happened, in a few words."""
        body = json.dumps({"required_status_checks": None, "enforce_admins": False,
                           "required_pull_request_reviews": None, "restrictions": None,
                           "allow_force_pushes": False, "allow_deletions": False})
        try:
            r = self._gh(self.status()["gh"], "api", "-X", "PUT", f"repos/{full}/branches/{branch}/protection",
                         "--input", "-", input=body, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return f"not set ({e})"
        if r.returncode == 0:
            return "on: GitHub refuses force pushes and branch deletion"
        if "Upgrade to GitHub Pro" in (r.stdout + r.stderr):
            return "not available for private repositories on GitHub's free plan"
        return "not set: " + (r.stderr or r.stdout).strip()[-200:]

    @staticmethod
    def _gh(exe, *args, cwd=None, timeout=60, env=None, input=None) -> subprocess.CompletedProcess:
        if not exe:
            raise OSError("GitHub CLI not found")
        return subprocess.run([exe, *args], cwd=cwd, input=input, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env={**(env or os.environ), "GH_PROMPT_DISABLED": "1"}, **NO_WINDOW)


GITHUB = GhCli()
