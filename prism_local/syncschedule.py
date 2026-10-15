"""When the editor's sync does what (gitsync.GitSync asks every TICK seconds).

    schedule.edited()                  # you saved a file
    for step in schedule.due(busy):    # "commit", "push", "pull"
        ...
    schedule.recorded(mark)            # everything saved up to `mark` is committed

An autosave commit follows IDLE seconds after your last save, and at the latest MAX_WAIT
after the first one it has not recorded yet; a push is tried again RETRY seconds after
one failed; GitHub is looked at every FETCH_EVERY seconds, but not within QUIET seconds
of a save (no merge while you type). Nothing while an agent's turn runs but pushing: a
commit would record its half-done writes, a merge would change the files under it.

No git here: the clock is given (time.time), so a test moves it instead of reaching into
the sync's fields.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

IDLE = 120               # seconds without an edit before an autosave commit
MAX_WAIT = 600           # ... but at the latest this long after the first change
FETCH_EVERY = 60         # seconds between fetches from the remote (co-authors' changes arrive soon)
QUIET = 20               # automatic pulls wait this long after your last save (no clash with typing)
RETRY = 60               # seconds before a failed push is tried again


class Schedule:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._lock = threading.Lock()     # edited() against recorded()'s "all recorded"
        self.last_edit: float | None = None
        self.dirty_since: float | None = None
        self.saves = 0                    # edited() so far
        self.last_fetch = 0.0
        self.push_failed_at = 0.0

    def edited(self) -> None:
        """You saved a file."""
        with self._lock:
            now = self.clock()
            self.last_edit = now
            self.saves += 1
            if self.dirty_since is None:
                self.dirty_since = now

    def mark(self) -> int:
        """Taken before a commit of everything: see recorded."""
        return self.saves

    def recorded(self, mark: int) -> None:
        """Everything saved is recorded, unless a save came in since `mark`: its autosave
        stays due (at worst one that finds nothing). A save still writing calls edited()
        after."""
        with self._lock:
            if self.saves == mark:
                self.dirty_since = None

    def leftover(self) -> None:
        """Changes left from before the editor started: recorded like new ones."""
        with self._lock:
            self.last_edit = self.dirty_since = self.clock()

    def fetched(self) -> None:
        self.last_fetch = self.clock()

    def push_failed(self) -> None:
        self.push_failed_at = self.clock()

    @property
    def pending(self) -> bool:
        """An autosave is due sometime: there are saves not committed."""
        return self.dirty_since is not None

    def next_autosave(self) -> float | None:
        """Seconds until the autosave commit; None when none is due."""
        if self.dirty_since is None:
            return None
        return max(0, min((self.last_edit or 0) + IDLE, self.dirty_since + MAX_WAIT) - self.clock())

    def due(self, busy: bool) -> list[str]:
        """What to do now, in this order. `busy`: an agent's turn runs."""
        now, steps = self.clock(), []
        if self.dirty_since and not busy and (
                now - (self.last_edit or now) >= IDLE or now - self.dirty_since >= MAX_WAIT):
            steps.append("commit")
        if now - self.push_failed_at >= RETRY:
            steps.append("push")
        if now - self.last_fetch >= FETCH_EVERY and not busy and now - (self.last_edit or 0) >= QUIET:
            steps.append("pull")
        return steps
