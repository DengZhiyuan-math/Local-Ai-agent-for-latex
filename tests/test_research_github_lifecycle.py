"""The editor lifecycle owns issue synchronization and ignores old-project callbacks."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "prism_local")]


class DiscussionLifecycle(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        agents = self.folder / "agents.json"
        agents.write_text("{}\n")
        self.enterContext(mock.patch.dict(os.environ, {
            "PRISM_STATE_DIR": str(self.folder / "state"), "PRISM_AGENTS": str(agents),
            "PRISM_AUTO_UPDATE": "0"}))
        import server
        from presence import Presence
        self.server, self.events = server, []
        for name in ("PROJECT", "DISCUSSIONS"):
            self.enterContext(mock.patch.object(server, name, None))
        self.enterContext(mock.patch.object(server, "PRESENCE", Presence()))
        self.enterContext(mock.patch.object(server.AGENT, "server_url", None))
        self.root = self.folder / "paper"
        self.root.mkdir()
        (self.root / "main.tex").write_text("source")
        events = self.events

        class Worker:
            def __init__(self, knowledge, **kwargs):
                self.project, self.is_open = knowledge.project, kwargs["is_open"]

            def start(self): events.append("issues:start")
            def close(self): events.append("issues:close")
            def wake(self): events.append("issues:wake")

        self.enterContext(mock.patch.object(server, "GitHubDiscussions", Worker))

    def test_server_start_and_stop_order_owns_the_issue_worker(self):
        server, events = self.server, self.events

        def listen(handler):
            server.PROJECT.stop_build = lambda: events.append("build:stop")

        def serve(**callbacks):
            callbacks["started"]()
            callbacks["stopping"]()

        app = SimpleNamespace(args=SimpleNamespace(exit_when_idle=True),
                              url="http://127.0.0.1:1/", listen=listen, serve=serve)
        with mock.patch.object(server, "APP", app), \
                mock.patch.object(server, "is_scratch", return_value=True), \
                mock.patch.object(server.GITSYNC, "start", side_effect=lambda: events.append("git:start")), \
                mock.patch.object(server.GITSYNC, "flush", side_effect=lambda: events.append("git:flush")), \
                mock.patch.object(server.AGENT, "shutdown", side_effect=lambda: events.append("agent:stop")):
            server.serve(self.root)
        self.assertEqual(events, ["git:start", "issues:start", "issues:close",
                                  "build:stop", "agent:stop", "git:flush"])

    def test_presence_and_old_project_callbacks_do_not_cross_projects(self):
        server, events = self.server, self.events
        server.set_root(self.root)
        self.assertFalse(server.DISCUSSIONS.is_open())
        server.PRESENCE.beat("editor-page-one")
        self.assertTrue(server.DISCUSSIONS.is_open())
        server.PRESENCE.bye("editor-page-one")
        self.assertFalse(server.DISCUSSIONS.is_open())
        previous = server.PROJECT.on_change
        other = self.folder / "other"
        other.mkdir()
        server.set_root(other)
        events.clear()
        with mock.patch.object(server.GITSYNC, "touched") as touched:
            previous()
            touched.assert_not_called()
            self.assertEqual(events, [])
            server.PROJECT.on_change()
            touched.assert_called_once()
            self.assertEqual(events, ["issues:wake"])


if __name__ == "__main__":
    unittest.main()
