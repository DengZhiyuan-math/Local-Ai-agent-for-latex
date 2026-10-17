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
import subprocess
import threading
import time
from pathlib import Path

from errors import NotInstalled, Refused
from gitenv import git_env
import proc
import tools

STATUS_TTL = 120         # seconds gh's login is not asked again


class GhCli:
    """GitHub through the GitHub CLI (gh), logged in by `gh auth login`."""

    def __init__(self):
        self._status: tuple[float, dict] | None = None
        self._login_lock = threading.Lock()
        self._login: dict = {"state": None, "warning": None}     # see login()
        self._login_proc: subprocess.Popen | None = None
        self._login_stop: str | None = None          # why it was ended: "cancelled", "expired"

    def status(self, refresh: bool = False) -> dict:
        """Whether gh is installed and logged in, and as whom."""
        if self._status and not refresh and time.time() - self._status[0] < STATUS_TTL:
            return self._status[1]
        exe = tools.find("gh")              # also one installed since Prism started
        status = {"gh": exe, "logged_in": False, "account": None}
        if not exe:
            status["error"] = "The GitHub CLI is not installed. Install it from the Home page (Getting started)."
        else:
            try:
                r = self._gh(exe, "auth", "status", "--hostname", "github.com", timeout=20)
                m = re.search(r"Logged in to github\.com (?:account|as) (\S+)", r.stdout + r.stderr)
                if m and r.returncode == 0:
                    status.update(logged_in=True, account=m.group(1).strip("()"))
                else:
                    status["error"] = "You are not logged in to GitHub. Log in from the Home page (Getting started)."
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

    def repos(self) -> list[dict]:
        """Your repositories on GitHub, the most recently changed first: {"full" ("owner/name"),
        "name", "private", "updated"}. To open one on this computer (Home: Open from GitHub)."""
        exe = tools.find("gh")
        if not exe:
            raise NotInstalled("gh")
        r = self._gh(exe, "repo", "list", "--json", "nameWithOwner,name,isPrivate,updatedAt",
                     "--limit", "200", timeout=60)
        if r.returncode != 0:
            raise Refused("Could not list your repositories on GitHub.\n" + (r.stderr or r.stdout).strip()[-300:])
        rows = [{"full": x["nameWithOwner"], "name": x["name"], "private": bool(x.get("isPrivate")),
                 "updated": x.get("updatedAt") or ""} for x in json.loads(r.stdout or "[]")]
        return sorted(rows, key=lambda x: x["updated"], reverse=True)

    # ------------------------------------------------------------ logging in, from Home
    LOGIN_TIME = 900            # seconds the browser's authorization is waited for

    def login(self) -> dict:
        """Log in to GitHub in the browser, for someone who has never used a terminal: gh's
        device flow (`gh auth login --web`) in the background. Its one-time code is shown
        on the page (login_state: "code", "url"), which opens GitHub's page to enter it;
        once it is authorized, git is set to push with this login too (`gh auth setup-git`:
        no second login window on the first push). Should that fail twice, the login is
        still "done", with a `warning` for the page and gh's words in `details`. A cancel
        while it starts ends it too. Its state at once."""
        exe = tools.find("gh")
        if not exe:
            raise NotInstalled("gh")
        with self._login_lock:
            if self._login.get("state") in ("starting", "waiting"):
                return dict(self._login)
            self._login = {"state": "starting", "code": None, "url": None, "error": None,
                           "details": None, "warning": None}
            self._login_stop = None          # a cancel from now on ends this login
        threading.Thread(target=self._log_in, args=(exe,), daemon=True).start()
        return self.login_state()

    def login_state(self) -> dict:
        with self._login_lock:
            return dict(self._login)

    def cancel_login(self) -> None:
        with self._login_lock:
            p = self._login_proc
            self._login_stop = "cancelled"
        if p is not None:
            proc.kill_tree(p)

    def _expire_login(self, p: subprocess.Popen) -> None:
        with self._login_lock:
            self._login_stop = "expired"
        proc.kill_tree(p)

    def _log_in(self, exe: str) -> None:
        lines: list[str] = []
        try:
            p = proc.start([exe, "auth", "login", "--web", "--hostname", "github.com",
                            "--git-protocol", "https", "--skip-ssh-key"],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace", env=git_env())
        except OSError as e:
            return self._login_ended("failed", "GitHub's login could not be started.", str(e))
        with self._login_lock:
            self._login_proc = p
            stopped = self._login_stop       # cancelled while it was starting
        if stopped:
            proc.kill_tree(p)
        try:
            p.stdin.write("\n")          # "Press Enter to open github.com in your browser"
            p.stdin.close()
        except OSError:
            pass
        timer = threading.Timer(self.LOGIN_TIME, self._expire_login, args=(p,))
        timer.start()
        try:
            for line in p.stdout:
                lines.append(line.rstrip())
                code = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b", line)
                url = re.search(r"https://\S*github\.com/login/device\S*", line)
                with self._login_lock:
                    if code:
                        self._login.update(state="waiting", code=code.group(1))
                    if url:
                        self._login["url"] = url.group(0).rstrip(".")
            p.wait()
        finally:
            timer.cancel()
            p.stdout.close()
            with self._login_lock:
                self._login_proc = None
        said = "\n".join(lines)[-600:]
        if p.returncode != 0:
            return self._login_ended("failed", {
                "cancelled": "Logging in to GitHub was cancelled.",
                "expired": f"GitHub's page was not confirmed within {self.LOGIN_TIME // 60} minutes. Try again.",
            }.get(self._login_stop, "Logging in to GitHub did not work. Try again."), said)
        setup = self._setup_git(exe)
        if setup:
            setup = self._setup_git(exe)     # once more: a passing hiccup
        self.status(refresh=True)
        self._login_ended("done", None, said + ("\nsetup-git: " + setup if setup else ""),
                          warning=self.SETUP_FAILED if setup else None)

    # Logged in, but git was not set to push with this login: the first push may open
    # git's own login window. Said so, in words for someone who does not know git.
    SETUP_FAILED = ("You are logged in to GitHub, but this computer could not be fully set up to use "
                    "that login. The first time a project is saved to GitHub, a GitHub window may ask "
                    "you to log in once more: log in there, and it is remembered.")

    def _setup_git(self, exe: str) -> str | None:
        """`gh auth setup-git`: None, or gh's words for why not."""
        try:
            r = self._gh(exe, "auth", "setup-git", "--hostname", "github.com", timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return str(e) or type(e).__name__
        return None if r.returncode == 0 else ((r.stderr or r.stdout).strip()[-300:] or "gh auth setup-git failed")

    def _login_ended(self, state: str, error: str | None, details: str | None,
                     warning: str | None = None) -> None:
        with self._login_lock:
            self._login.update(state=state, error=error, details=details or None, warning=warning)

    @staticmethod
    def _gh(exe, *args, cwd=None, timeout=60, env=None, input=None) -> subprocess.CompletedProcess:
        if not exe:
            raise NotInstalled("gh")
        return proc.run([exe, *args], cwd=cwd, input=input, text=True, encoding="utf-8",
                        errors="replace", timeout=timeout,
                        env={**(env or os.environ), "GH_PROMPT_DISABLED": "1"})


GITHUB = GhCli()
