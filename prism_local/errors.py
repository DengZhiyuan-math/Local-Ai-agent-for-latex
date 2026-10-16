"""What a request is refused for, and the HTTP status that says so (httpbase._answer).

    raise NotFound("unknown project")         # 404, the message shown on the page

A Refused is a ValueError, so code that catches ValueError around a call still catches
it. Request parameters come as Params: a missing one is a Refused ("missing path"), not
a KeyError, which is left to mean a bug (a logged 500).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class Refused(ValueError):
    """The request cannot be done as asked; `status` and the message say why. Not logged.
    `details`: more for the page, sent beside the message."""
    status = 400
    details: dict = {}

    def __init__(self, message: str, status: int | None = None, details: dict | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status
        if details:
            self.details = details


class NotFound(Refused):
    status = 404


class Conflict(Refused):
    """Not now: something else holds what it needs (a build, an agent's turn, git)."""
    status = 409


class LockBusy(TimeoutError):
    """A lock between Prism processes (registry.FileLock) was not had in time: another
    window is saving the same state. A TimeoutError, as callers have always caught it;
    a request it reaches is answered 409 with this message (httpbase), not dropped as
    though the page had gone."""
    status = 409


NETWORK_GIT = ("push", "fetch", "pull", "ls-remote", "clone")


def say(e: BaseException) -> str:
    """An exception as a sentence for the page. A command that did not finish in time says
    what it was and what to do, not its whole command line; others say what they say."""
    if isinstance(e, subprocess.TimeoutExpired):
        args = [str(a) for a in (e.cmd if isinstance(e.cmd, (list, tuple)) else [e.cmd])]
        name, secs = Path(args[0]).stem if args else "A command", int(e.timeout or 0)
        if name == "git" and any(a in NETWORK_GIT for a in args):
            return (f"GitHub did not answer within {secs} seconds; check your internet connection. "
                    "It is tried again by itself.")
        return f"{name} did not finish within {secs} seconds"
    return str(e)


class Params(dict):
    """A request's query or JSON body."""

    def __missing__(self, key):
        raise Refused(f"missing {key}")
