"""Topic records use the same protected tool on all controlled CLI providers."""
import json
import unittest

import test_claude_research_protocol as protocol


class ResearchTopicsProtocol(unittest.TestCase):
    setUp = protocol.ClaudeResearchProtocol.setUp
    call = staticmethod(protocol.ClaudeResearchProtocol.call)

    def test_record_tool_is_shared_in_ask_mode_for_all_cli_providers(self):
        from claude_research import Controller
        self.job.mode = "ask"
        for provider in ("claude", "codex", "deepcode"):
            with self.subTest(provider=provider):
                controller = Controller(self.job, provider=provider)
                self.assertIn("record_research_topic", controller.names())
                self.assertNotIn("write_file", controller.names())

    def test_cli_tool_queues_and_editor_saves_without_manuscript_permission(self):
        from claude_research import Controller
        from research_topics import finish
        original = (self.project.root / "main.tex").read_bytes()
        self.job.mode = "ask"
        self.job.human_text = "Maybe compare the direct estimate."
        for index, provider in enumerate(("claude", "codex", "deepcode"), 1):
            with self.subTest(provider=provider):
                self.job.provider = provider
                self.job.id = index
                controller = Controller(self.job, provider=provider)
                result = controller.answer(self.call("record_research_topic", {
                    "title": "Direct estimate " + provider,
                    "entries": [{"kind": "idea", "text": "Compare the direct estimate.",
                                 "source": "assistant"}]}))
                self.assertFalse(result["result"]["isError"], result)
                if index == 1:
                    self.assertFalse(any(self.project.root.glob("research/topics/*.md")))
                finish(self.job.research_access, self.job, success=True, session_id=provider + "-fixture")
                saved = [event for event in self.job.events if event["t"] == "research_record"][-1]
                self.assertIn("Compare the direct estimate.", (self.project.root / saved["path"]).read_text())
                self.assertEqual((self.project.root / "main.tex").read_bytes(), original)

    def test_stopped_cli_draft_does_not_materialize_on_status_retry(self):
        from claude_research import Controller
        from research_topics import finish, retry
        self.job.mode = "ask"
        self.job.human_text = "Maybe compare estimates."
        result = Controller(self.job).answer(self.call("record_research_topic", {
            "title": "Stopped discussion", "entries": [
                {"kind": "idea", "text": "Compare estimates.", "source": "assistant"}]}))
        self.assertFalse(result["result"]["isError"], result)
        self.job.cancel.set()
        finish(self.job.research_access, self.job, success=False)
        retry(self.knowledge)
        self.assertFalse(any(self.project.root.glob("research/topics/*.md")))
        self.assertFalse(any(event["t"] == "research_record" for event in self.job.events))


if __name__ == "__main__":
    unittest.main()
