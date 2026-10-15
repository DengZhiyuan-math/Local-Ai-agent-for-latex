"""When the editor's sync commits, pushes and pulls (syncschedule.py), on a clock the test moves."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from syncschedule import FETCH_EVERY, IDLE, MAX_WAIT, QUIET, RETRY, Schedule  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Timing(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.s = Schedule(self.clock)
        self.s.fetched()                    # just looked at GitHub
        self.s.push_failed_at = self.clock.now

    def test_an_autosave_once_you_stop_typing_or_at_the_latest_after_max_wait(self):
        self.s.edited()
        self.assertEqual(self.s.due(False), [])
        self.assertEqual(self.s.next_autosave(), IDLE)
        self.clock.advance(IDLE)
        self.assertIn("commit", self.s.due(False))
        typing = Schedule(self.clock)
        typing.fetched()
        typing.push_failed_at = self.clock.now
        for _ in range(MAX_WAIT // 10):     # a save every 10 seconds, never IDLE apart
            typing.edited()
            self.assertNotIn("commit", typing.due(False))
            self.clock.advance(10)
        typing.edited()
        self.assertIn("commit", typing.due(False), "at the latest MAX_WAIT after the first save")

    def test_no_pull_while_you_type_and_none_during_an_agent_turn(self):
        self.clock.advance(FETCH_EVERY)
        self.s.edited()
        self.assertNotIn("pull", self.s.due(False))
        self.clock.advance(QUIET)
        self.assertIn("pull", self.s.due(False))
        self.assertNotIn("pull", self.s.due(True), "not while the agent works")
        self.s.fetched()
        self.assertNotIn("pull", self.s.due(False))

    def test_a_failed_push_is_tried_again_after_retry(self):
        self.s.push_failed()
        self.clock.advance(RETRY - 1)
        self.assertNotIn("push", self.s.due(False))
        self.clock.advance(1)
        self.assertIn("push", self.s.due(True), "pushing goes on during an agent's turn")

    def test_no_commit_during_an_agent_turn(self):
        self.s.edited()
        self.clock.advance(MAX_WAIT)
        self.assertNotIn("commit", self.s.due(True))

    def test_a_save_during_a_commit_keeps_its_autosave_due(self):
        self.s.edited()
        mark = self.s.mark()
        self.s.edited()                      # git may have looked before it wrote
        self.s.recorded(mark)
        self.assertTrue(self.s.pending)
        self.s.recorded(self.s.mark())
        self.assertFalse(self.s.pending)
        self.assertIsNone(self.s.next_autosave())


if __name__ == "__main__":
    unittest.main()
