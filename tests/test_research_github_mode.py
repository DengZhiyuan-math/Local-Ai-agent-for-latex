"""GitHub discussion mode preserves the protected local topic pipeline."""
import unittest

import test_research_topics as local_topics


class GitHubTopicMode(unittest.TestCase):
    setUp = local_topics.TopicRecords.setUp
    job = local_topics.TopicRecords.job
    draft = local_topics.TopicRecords.draft

    def test_github_mode_keeps_the_same_local_document_and_notation_checks(self):
        import research_topics
        from research_access import NotationStop
        original = (self.project.root / "main.tex").read_bytes()
        state = self.knowledge._load()
        state["discussion"]["mode"] = "github"
        self.knowledge._write(state)
        job = self.job()
        research_topics.stage(self.access, job, self.draft())
        research_topics.finish(self.access, job, success=True)
        saved = [event for event in job.events if event["t"] == "research_record"]
        self.assertEqual(len(saved), 1)
        self.assertTrue((self.project.root / saved[0]["path"]).is_file())
        self.assertEqual((self.project.root / "main.tex").read_bytes(), original)
        with self.assertRaises(NotationStop):
            research_topics.stage(self.access, self.job(2), self.draft(text="Try $z=x+x$."))


if __name__ == "__main__":
    unittest.main()
