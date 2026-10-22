"""The editor's per-turn target, observed by an agent backend without running a CLI."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backends import Backend
from test_agent import manager, wait_done


class EditTargets(unittest.TestCase):
    def test_active_draft_is_the_default_and_explicit_targets_take_priority(self):
        prompts = []

        class Capture(Backend):
            def run(self, job):
                prompts.append(job.prompt)
                return {"exit": 0}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("main.tex", "main-copy.tex"):
                (root / rel).write_text("draft", encoding="utf-8")
            m = manager(root, capture=Capture("capture"))
            result = m.start("Improve this paragraph", None, "edit", active_file="main-copy.tex")
            wait_done(m.jobs[result["job"]])
            self.assertIn('Default edit target: "main-copy.tex"', prompts[0])
            self.assertIn("explicitly names other files", prompts[0])
            self.assertIn("Do not switch to the compile main file", prompts[0])

            result = m.start("Edit @main.tex instead", None, "edit", scope=["main.tex"],
                             active_file="main-copy.tex")
            wait_done(m.jobs[result["job"]])
            self.assertIn("ONLY these files in this turn: main.tex", prompts[-1])
            self.assertIn("scope for this turn take priority", prompts[-1])

            result = m.start("/improve", None, "edit", active_file="main.tex")
            wait_done(m.jobs[result["job"]])
            self.assertTrue(prompts[-1].startswith("/improve"))
            self.assertIn('Default edit target: "main.tex"', prompts[-1])
            self.assertNotIn("main-copy.tex", prompts[-1])

            result = m.start("Improve this", None, "edit")
            wait_done(m.jobs[result["job"]])
            self.assertIn("ask which file to edit", prompts[-1])

            result = m.start("Explain", None, "ask", active_file="main.tex")
            wait_done(m.jobs[result["job"]])
            self.assertNotIn("Default edit target", prompts[-1])
            self.assertIn("do not create, change or delete files", prompts[-1])

            self.assertIn("error", m.start("Edit", None, "edit", active_file="../outside.tex"))
            self.assertIn("error", m.start("Edit", None, "edit", active_file="missing.tex"))


if __name__ == "__main__":
    unittest.main()
