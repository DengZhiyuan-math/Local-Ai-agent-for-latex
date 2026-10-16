"""What a request is refused for, and the HTTP status that says so (httpbase._answer).

    raise NotFound("unknown project")         # 404, the message shown on the page

A Refused is a ValueError, so code that catches ValueError around a call still catches
it. Request parameters come as Params: a missing one is a Refused ("missing path"), not
a KeyError, which is left to mean a bug (a logged 500).
"""
from __future__ import annotations


class Refused(ValueError):
    """The request cannot be done as asked; `status` and the message say why. Not logged."""
    status = 400

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status


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


class Params(dict):
    """A request's query or JSON body."""

    def __missing__(self, key):
        raise Refused(f"missing {key}")
