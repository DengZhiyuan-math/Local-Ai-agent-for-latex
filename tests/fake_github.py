"""A stand-in for GitHub (github.GITHUB) in the tests: logged in as `account`; a repository
it creates is a github.com address added as the project's origin, and what it was asked is
kept (created, protected). With `remotes`, git() routes fetch/push to local bare repositories
without changing the configured GitHub identity. Patch gitenv.git with it for offline tests.

    with mock.patch.object(github, "GITHUB", FakeGitHub()) as gh: ...
"""
from pathlib import Path

import gitenv


class FakeGitHub:
    def __init__(self, account: str = "me", logged_in: bool = True, fail: str | None = None,
                 remotes: Path | None = None):
        self.account, self.logged_in, self.fail = account, logged_in, fail
        self.created: list[str] = []
        self.protected: list[tuple[str, str]] = []
        self.remotes = remotes
        self.repositories: dict[str, Path] = {}
        self._git = gitenv.git

    def status(self, refresh: bool = False) -> dict:
        if not self.logged_in:
            return {"gh": "gh", "logged_in": False, "account": None,
                    "error": "GitHub CLI is not logged in. Run: gh auth login"}
        return {"gh": "gh", "logged_in": True, "account": self.account}

    def create(self, root: Path, full: str) -> str | None:
        if self.fail:
            return self.fail
        full = full if "/" in full else f"{self.account}/{full}"
        if self.remotes is not None:
            remote = self.remotes / (full + ".git")
            remote.parent.mkdir(parents=True, exist_ok=True)
            self._git(remote.parent, "init", "-q", "--bare", "-b", "main", str(remote), check=True)
            self.repositories[full] = remote
        self._git(root, "remote", "add", "origin", f"https://github.com/{full}.git", check=True)
        self.created.append(full)
        return None

    def git(self, cwd, *args, **kwargs):
        if args and args[0] in ("fetch", "push", "ls-remote"):
            # Only the transport sees the rewrite. Target discovery and binding still
            # inspect the real GitHub URL, and git itself performs the transfer.
            config = [item for full, path in self.repositories.items()
                      for item in ("-c", f"url.{path.as_posix()}.insteadOf=https://github.com/{full}.git")]
            return self._git(cwd, *config, *args, **kwargs)
        return self._git(cwd, *args, **kwargs)

    def protect(self, full: str, branch: str) -> str:
        self.protected.append((full, branch))
        return "on: GitHub refuses force pushes and branch deletion"
