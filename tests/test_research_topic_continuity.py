"""A received topic continues through an actual local clone without importing trust."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "prism_local"), str(Path(__file__).parent)]
from backends import Job
from errors import Refused
from project import Project
from research import ResearchKnowledge
from research_access import NotationStop, ResearchAccess
import research_topics
from tmpdirs import tmpdir


@unittest.skipUnless(shutil.which("git"), "needs local Git")
class TopicContinuity(unittest.TestCase):
    def setUp(self):
        self.work = tmpdir()
        self.source, self.clone = self.work / "author-a", self.work / "author-b"
        self.source.mkdir()
        (self.source / "main.tex").write_text("$x$\n")
        self.state_a, self.state_b = self.work / "state-a", self.work / "state-b"

    @staticmethod
    def git(root, *arguments):
        return subprocess.run(["git", "-C", str(root), *arguments], check=True,
                              capture_output=True, text=True).stdout.strip()

    @staticmethod
    def record(knowledge, statement, *, topic_id=None, human=False, number=1):
        job = Job(number)
        job.root, job.provider = knowledge.project.root, "continuity-fixture"
        job.human_text = statement if human else "Continue the current estimate."
        entry = {"kind": "decision" if human else "idea", "text": statement,
                 "source": "human" if human else "assistant"}
        if human:
            entry["quote"] = statement
        arguments = {"title": "Direct estimate", "entries": [entry]}
        if topic_id is not None:
            arguments["topic_id"] = topic_id
        access = ResearchAccess(knowledge)
        queued = research_topics.stage(access, job, arguments)
        research_topics.finish(access, job, success=True, session_id=f"conversation-{number}")
        return queued, job.events

    def make_clone(self):
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_a)}):
            knowledge = ResearchKnowledge(Project(self.source))
            knowledge.initialize(notation="z")
            queued, _ = self.record(knowledge, "We decided to compare $z$ with the direct estimate.", human=True)
            self.topic_id, self.topic_path = queued["topic_id"], queued["path"]
            self.received = (self.source / self.topic_path).read_bytes()
        self.git(self.source, "init", "-q")
        self.git(self.source, "config", "user.name", "Local Fixture")
        self.git(self.source, "config", "user.email", "fixture@example.invalid")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-qm", "Fixture topic")
        subprocess.run(["git", "clone", "-q", str(self.source), str(self.clone)], check=True,
                       capture_output=True)

    def test_clone_initialize_and_continue_keeps_one_id_and_exact_received_prefix(self):
        self.make_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge = ResearchKnowledge(Project(self.clone))
            self.assertFalse(knowledge.status()["initialized"])
            self.assertEqual(knowledge.classify("$x+z$")["known"], [])
            knowledge.initialize()
            reference, = research_topics.status(knowledge)["topics"]
            self.assertEqual(reference["id"], self.topic_id)
            self.assertEqual(reference["source_state"], "unverified_received_reference")
            queued, events = self.record(knowledge, "Check the endpoint hypothesis.", number=2)
            files = sorted(path.relative_to(self.clone).as_posix()
                           for path in (self.clone / "research/topics").glob("*.md"))
            proof = {"boundary": "Actual isolated local git clone; no model or remote service.",
                     "source_topic_id": self.topic_id, "continued_topic_id": queued["topic_id"],
                     "source_path": self.topic_path, "continued_path": queued["path"],
                     "topic_files": files, "received_sha256": hashlib.sha256(self.received).hexdigest(),
                     "received_prefix_unchanged": (self.clone / queued["path"]).read_bytes().startswith(self.received),
                     "known_after_local_initialization": [row["canonical"] for row in knowledge.classify("$x+z$")["known"]],
                     "events": events}
            if os.environ.get("PRISM_TOPIC_CONTINUITY_EVIDENCE"):
                Path(os.environ["PRISM_TOPIC_CONTINUITY_EVIDENCE"]).write_text(json.dumps(proof, indent=2) + "\n")
            self.assertEqual(queued["topic_id"], self.topic_id)
            self.assertEqual(queued["path"], self.topic_path)
            self.assertEqual(files, [self.topic_path])
            updated = (self.clone / self.topic_path).read_bytes()
            self.assertTrue(updated.startswith(self.received))
            self.assertIn(b"unverified received reference", updated[len(self.received):].lower())
            self.assertIn(b"Check the endpoint hypothesis.", updated[len(self.received):])
            topic, = knowledge.status()["discussion"]["topics"]
            self.assertEqual(len(topic["entries"]), 1)
            self.assertEqual(topic["entries"][0]["text"], "Check the endpoint hypothesis.")
            self.assertEqual(topic["entries"][0]["provenance"]["source"], "assistant")
            self.assertEqual([row["canonical"] for row in knowledge.classify("$x+z$")["known"]], ["x"])

    def test_explicit_id_restart_replay_and_failed_save_keep_one_document(self):
        self.make_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge = ResearchKnowledge(Project(self.clone))
            knowledge.initialize()
            with mock.patch.object(knowledge, "_write", side_effect=OSError("controlled index failure")):
                queued, events = self.record(knowledge, "Check the endpoint hypothesis.", topic_id=self.topic_id, number=2)
            self.assertEqual(queued["topic_id"], self.topic_id)
            self.assertEqual([row["t"] for row in events], ["research_record_error"])
            self.assertTrue((self.clone / self.topic_path).read_bytes().startswith(self.received))
            reopened = ResearchKnowledge(Project(self.clone))
            research_topics.retry(reopened)
            topic, = reopened.status()["discussion"]["topics"]
            self.assertEqual(topic["id"], self.topic_id)
            self.assertEqual(topic["revision"], 2)
            before = (self.clone / self.topic_path).read_bytes(), (self.clone / self.topic_path).stat().st_mtime_ns
            repeated, repeated_events = self.record(reopened, "Check the endpoint hypothesis.", number=3)
            self.assertFalse(repeated["changed"])
            self.assertEqual(repeated_events, [])
            self.assertEqual(((self.clone / self.topic_path).read_bytes(),
                              (self.clone / self.topic_path).stat().st_mtime_ns), before)
            self.assertEqual(len(list((self.clone / "research/topics").glob("*.md"))), 1)

    def test_malformed_received_identity_title_path_hash_and_duplicates_fail_closed(self):
        self.make_clone()
        record_path = self.clone / "research/knowledge.json"
        original = record_path.read_bytes()
        mutations = (
            lambda data: data["discussion"]["topics"][0].update(id="f" * 24),
            lambda data: data["discussion"]["topics"][0].update(title="A fabricated topic"),
            lambda data: data["discussion"]["topics"][0].update(path="../outside.md"),
            lambda data: data["discussion"]["topics"][0].update(sha256="0" * 64),
            lambda data: data["discussion"]["topics"].append(data["discussion"]["topics"][0]),
        )
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            for mutate in mutations:
                data = json.loads(original)
                mutate(data)
                bad = json.dumps(data).encode()
                record_path.write_bytes(bad)
                with self.assertRaises((OSError, ValueError)):
                    ResearchKnowledge(Project(self.clone)).initialize()
                self.assertEqual(record_path.read_bytes(), bad)
                self.assertEqual((self.clone / self.topic_path).read_bytes(), self.received)
                self.assertEqual(len(list((self.clone / "research/topics").glob("*.md"))), 1)
            record_path.write_bytes(original)

    def test_a_received_document_changed_after_initialize_is_not_overwritten(self):
        self.make_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge = ResearchKnowledge(Project(self.clone))
            knowledge.initialize()
            changed = self.received + b"\nA concurrent received edit.\n"
            (self.clone / self.topic_path).write_bytes(changed)
            with self.assertRaises((OSError, ValueError)):
                self.record(knowledge, "Check the endpoint hypothesis.", topic_id=self.topic_id, number=2)
            self.assertEqual((self.clone / self.topic_path).read_bytes(), changed)
            self.assertEqual(len(list((self.clone / "research/topics").glob("*.md"))), 1)

    def test_edited_reference_projection_after_initialize_blocks_continuation(self):
        self.make_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge = ResearchKnowledge(Project(self.clone))
            knowledge.initialize()
            record_path = self.clone / "research/knowledge.json"
            record = json.loads(record_path.read_bytes())
            record["discussion"]["topic_references"][0]["topic_id"] = "f" * 24
            received = json.dumps(record).encode()
            record_path.write_bytes(received)
            with self.assertRaises((OSError, ValueError)):
                self.record(knowledge, "Check the endpoint hypothesis.", topic_id=self.topic_id, number=2)
            self.assertEqual(record_path.read_bytes(), received)
            self.assertEqual((self.clone / self.topic_path).read_bytes(), self.received)

    def test_missing_linked_or_oversized_received_documents_fail_before_initialize(self):
        self.make_clone()
        path, record_path = self.clone / self.topic_path, self.clone / "research/knowledge.json"
        original = record_path.read_bytes()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            path.unlink()
            with self.assertRaises((OSError, ValueError)):
                ResearchKnowledge(Project(self.clone)).initialize()
            self.assertEqual(record_path.read_bytes(), original)
            outside = self.work / "outside.md"
            outside.write_bytes(self.received)
            try:
                path.symlink_to(outside)
            except OSError:
                pass
            else:
                with self.assertRaises((OSError, ValueError)):
                    ResearchKnowledge(Project(self.clone)).initialize()
                self.assertEqual(outside.read_bytes(), self.received)
                self.assertEqual(record_path.read_bytes(), original)
                path.unlink()
            large = self.received + b"x" * research_topics.MAX_QUEUE
            path.write_bytes(large)
            data = json.loads(original)
            data["discussion"]["topics"][0]["sha256"] = hashlib.sha256(large).hexdigest()
            received = json.dumps(data).encode()
            record_path.write_bytes(received)
            with self.assertRaises((OSError, ValueError)):
                ResearchKnowledge(Project(self.clone)).initialize()
            self.assertEqual(path.stat().st_size, len(large))
            self.assertEqual(record_path.read_bytes(), received)

    def test_received_prefix_is_not_a_license_to_generate_its_unknown_notation(self):
        self.make_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge = ResearchKnowledge(Project(self.clone))
            knowledge.initialize()
            with self.assertRaises(NotationStop):
                self.record(knowledge, "Reuse $z$ in the local summary.", topic_id=self.topic_id, number=2)
            self.assertEqual((self.clone / self.topic_path).read_bytes(), self.received)
            self.assertFalse(knowledge.status()["discussion"].get("topics"))
            self.assertEqual([row["canonical"] for row in knowledge.classify("$x+z$")["known"]], ["x"])
            self.assertEqual(len(list((self.clone / "research/topics").glob("*.md"))), 1)

    def test_changed_retained_reference_history_cannot_authorize_an_append(self):
        self.make_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            knowledge = ResearchKnowledge(Project(self.clone))
            initialized = knowledge.initialize()
            reference = initialized["discussion"]["topic_references"][0]
            from research import MAX_RECORD, RECORD
            from research_provenance import Provenance
            archive = Provenance(self.clone, RECORD, MAX_RECORD).folder / "received" / (
                reference["document_sha256"] + ".record")
            archive.write_bytes(b"Edited private reference history.\n")
            before = (self.clone / "research/knowledge.json").read_bytes()
            with self.assertRaises((OSError, ValueError)):
                self.record(knowledge, "Check the endpoint hypothesis.", topic_id=self.topic_id, number=2)
            self.assertEqual((self.clone / self.topic_path).read_bytes(), self.received)
            self.assertEqual((self.clone / "research/knowledge.json").read_bytes(), before)

    def test_a_reference_only_clone_can_relay_the_same_topic_to_another_root(self):
        self.make_clone()
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state_b)}):
            ResearchKnowledge(Project(self.clone)).initialize()
        self.git(self.clone, "config", "user.name", "Local Fixture")
        self.git(self.clone, "config", "user.email", "fixture@example.invalid")
        self.git(self.clone, "add", ".")
        self.git(self.clone, "commit", "-qm", "Fixture received topic reference")
        third = self.work / "author-c"
        subprocess.run(["git", "clone", "-q", str(self.clone), str(third)], check=True, capture_output=True)
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.work / "state-c")}):
            knowledge = ResearchKnowledge(Project(third))
            knowledge.initialize()
            queued, _ = self.record(knowledge, "Check the endpoint hypothesis.", number=3)
            self.assertEqual(queued["topic_id"], self.topic_id)
            self.assertEqual(queued["path"], self.topic_path)
            self.assertEqual(len(list((third / "research/topics").glob("*.md"))), 1)
            self.assertTrue((third / self.topic_path).read_bytes().startswith(self.received))
            self.assertEqual([row["canonical"] for row in knowledge.classify("$x+z$")["known"]], ["x"])


if __name__ == "__main__":
    unittest.main()
