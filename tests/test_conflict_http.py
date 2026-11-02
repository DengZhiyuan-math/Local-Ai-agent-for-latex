"""Compare an unsaved editor draft with disk without changing either version."""
import shutil
import unittest

import test_research_http as base
from test_agent import call_chunks
from test_repoview import git


class FileComparisonHTTP(unittest.TestCase):
    setUp = base.ResearchHTTP.setUp
    tearDown = base.ResearchHTTP.tearDown
    start = base.ResearchHTTP.start
    stop = base.ResearchHTTP.stop
    request = base.ResearchHTTP.request
    initialize = base.ResearchHTTP.initialize
    turn = base.ResearchHTTP.turn
    events = base.ResearchHTTP.events
    finish = base.ResearchHTTP.finish

    def test_unsaved_comparison_needs_no_git_and_does_not_save_the_draft(self):
        source = self.root / "main.tex"
        source.write_bytes(b"agent result\r\n")
        before = source.read_bytes(), source.stat().st_mtime_ns
        code, result = self.request("/api/file/compare", {
            "path": "main.tex", "content": "manual draft\n"})
        self.assertEqual(code, 200, result)
        self.assertEqual(result["diff"], "--- main.tex (disk)\n+++ main.tex (my unsaved edits)\n"
                         "@@ -1 +1 @@\n-agent result\n+manual draft\n")
        self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), before)

    def test_equal_text_and_missing_final_newline_are_described_correctly(self):
        source = self.root / "main.tex"
        source.write_bytes(b"same\r\n")
        code, result = self.request("/api/file/compare", {"path": "main.tex", "content": "same\n"})
        self.assertEqual((code, result), (200, {"diff": ""}))
        source.write_bytes(b"agent result")
        code, result = self.request("/api/file/compare", {"path": "main.tex", "content": "manual draft"})
        self.assertEqual(code, 200, result)
        self.assertIn("-agent result\n\\ No newline at end of file\n+manual draft\n", result["diff"])

    def test_comparison_keeps_file_project_and_input_checks(self):
        for body, expected in [({"path": "../outside.tex", "content": "draft"}, 400),
                               ({"path": "missing.tex", "content": "draft"}, 404),
                               ({"path": "main.tex", "content": None}, 400),
                               ({"path": "main.tex", "content": "x" * 1_048_577}, 413),
                               ({"path": "main.tex", "content": "x\n" * 20_001}, 413)]:
            with self.subTest(expected=expected, path=body["path"]):
                code, result = self.request("/api/file/compare", body)
                self.assertEqual(code, expected, result)
        code, result = self.request("/api/file/compare", {"path": "main.tex", "content": "draft"}, project="other-project")
        self.assertEqual(code, 409, result)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_agent_commit_comparison_and_undo_keep_a_later_manual_save(self):
        self.initialize()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Fixture")
        git(self.root, "config", "user.email", "fixture@example.invalid")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "Original manuscript")
        self.assertEqual(self.request("/api/git/recheck", {})[0], 200)
        source = self.root / "main.tex"
        original = source.read_text()
        changed = original.replace("finite covering property", "checked covering property")
        done = next(event for event in self.finish(self.turn([
            call_chunks("read", "read_file", {"path": "main.tex"}),
            call_chunks("edit", "edit_file", {"path": "main.tex", "old_string": "finite covering property",
                                                "new_string": "checked covering property"}),
            [base.say("The proof description was updated.")]], mode="edit")) if event["t"] == "done")
        self.assertEqual(source.read_text(), changed)
        self.assertEqual([item["path"] for item in done["changed"]], ["main.tex"])
        self.assertEqual(git(self.root, "show", "HEAD:main.tex"), changed)
        self.assertTrue(git(self.root, "log", "-1", "--format=%s").startswith("Agent:"))
        draft = original + "A later manual paragraph.\n"
        code, compared = self.request("/api/file/compare", {"path": "main.tex", "content": draft})
        self.assertEqual(code, 200, compared)
        self.assertIn("-\\emph{compact} is a checked covering property.", compared["diff"])
        self.assertIn("+A later manual paragraph.", compared["diff"])
        self.assertEqual(source.read_text(), changed, "comparison must leave the Agent result intact")
        current = self.request("/api/file?path=main.tex")[1]
        self.assertEqual(self.request("/api/file", {"path": "main.tex", "content": draft,
                                                  "base_mtime": current["mtime"]})[0], 200)
        code, undone = self.request("/api/agent/undo", {"project_key": self.key, "turn": done["turn"]})
        self.assertEqual((code, undone["restored"], undone["skipped"]), (200, [], ["main.tex"]))
        self.assertEqual(source.read_text(), draft, "Undo must never replace the later manual save")


if __name__ == "__main__":
    unittest.main()
