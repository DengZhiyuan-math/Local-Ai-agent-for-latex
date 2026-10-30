"""Input capabilities and durable receipt authenticity, with isolated user state."""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from research_edits import EditTokens, KEY_FILE, WITNESS_FILE, make_receipt, verify_receipt


class EditorEvidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = 100
        self.tokens = EditTokens(clock=lambda: self.now, lifetime=10, limit=3)

    def test_new_identifier_is_bound_to_exact_versions(self):
        before, after = b"$x$", b"$x + y_i$"
        cap = self.tokens.issue("p", "main.tex", before)
        self.assertEqual(cap["before_sha256"], hashlib.sha256(before).hexdigest())
        valid, receipt = self.tokens.consume(cap["token"], "p", "main.tex", before, after)
        self.assertTrue(valid)
        self.assertEqual([a["canonical"] for a in receipt["added"]], ["y_i"])
        self.assertEqual(receipt["before_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(receipt["after_sha256"], hashlib.sha256(after).hexdigest())
        self.assertTrue(verify_receipt(receipt, "p"))
        self.assertNotIn(cap["token"], str(receipt))

    def test_existing_unknown_and_whitespace_are_not_adopted_by_save(self):
        cap = self.tokens.issue("p", "main.tex", b"$x + z$\n")
        valid, receipt = self.tokens.consume(cap["token"], "p", "main.tex", b"$x + z$\n", b"$x + z$\n\n")
        self.assertTrue(valid)
        self.assertIsNone(receipt)
        self.assertFalse((self.state / KEY_FILE).exists())

    def test_equivalent_spelling_and_operators_are_not_new(self):
        receipt = make_receipt("p", "main.tex", b"$x_i + \\ell_+$", b"$x_{i} + \\ell_{+} + \\sin x_i$")
        self.assertIsNone(receipt)

    def test_capability_cannot_be_replayed(self):
        cap = self.tokens.issue("p", "a.tex", b"$x$")
        self.assertTrue(self.tokens.consume(cap["token"], "p", "a.tex", b"$x$", b"$x+y$")[0])
        self.assertEqual(self.tokens.consume(cap["token"], "p", "a.tex", b"$x$", b"$x+z$"), (False, None))

    def test_other_project_path_or_version_cannot_use_capability(self):
        for project, path, before in [("q", "a.tex", b"$x$"), ("p", "b.tex", b"$x$"), ("p", "a.tex", b"$z$")]:
            cap = self.tokens.issue("p", "a.tex", b"$x$")
            with self.subTest(project=project, path=path, before=before):
                self.assertEqual(self.tokens.consume(cap["token"], project, path, before, b"$x+y$"), (False, None))

    def test_expired_or_evicted_capability_cannot_grant_origin(self):
        old = self.tokens.issue("p", "a.tex", b"$x$")
        self.now += 11
        self.assertEqual(self.tokens.consume(old["token"], "p", "a.tex", b"$x$", b"$y$"), (False, None))
        old = self.tokens.issue("p", "a.tex", b"$x$")
        for _ in range(3):
            self.tokens.issue("p", "a.tex", b"$x$")
        self.assertEqual(self.tokens.consume(old["token"], "p", "a.tex", b"$x$", b"$y$"), (False, None))

    def test_forged_human_flag_and_receipt_fields_do_not_grant_origin(self):
        self.assertEqual(self.tokens.consume({"human": True}, "p", "a.tex", b"", b"$z$"), (False, None))
        receipt = make_receipt("p", "a.tex", b"$x$", b"$x+z$")
        changed = copy.deepcopy(receipt)
        changed["added"][0]["notation"] = "q"
        self.assertFalse(verify_receipt(changed, "p"))
        self.assertFalse(verify_receipt(receipt, "other-project"))

    def test_receipt_survives_restart_but_not_another_local_key(self):
        receipt = make_receipt("p", "a.tex", b"$x$", b"$x+z$")
        self.assertTrue(verify_receipt(copy.deepcopy(receipt), "p"))
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state / "another-machine")}):
            self.assertFalse(verify_receipt(receipt, "p"))
            self.assertFalse((self.state / "another-machine").exists())

    def test_evidence_snapshot_reads_material_once_and_checks_each_payload(self):
        import research_edits as evidence
        receipt = make_receipt("p", "a.tex", b"$x$", b"$x+z$")
        forged = {**receipt, "id": "not-an-editor-event"}
        forged["signature"] = hmac.new((self.state / KEY_FILE).read_bytes(),
            evidence._payload(forged), hashlib.sha256).hexdigest()
        with mock.patch.object(evidence, "_key", wraps=evidence._key) as key_reads, \
                mock.patch.object(evidence.registry, "read_state", wraps=evidence.registry.read_state) as witness_reads, \
                mock.patch.object(evidence.hmac, "new", wraps=evidence.hmac.new) as signatures:
            with evidence.evidence_snapshot():
                for _ in range(5):
                    self.assertTrue(verify_receipt(receipt, "p"))
                self.assertFalse(verify_receipt({**receipt, "path": "changed.tex"}, "p"))
                self.assertFalse(verify_receipt(forged, "p"))
                self.assertFalse(verify_receipt(receipt, "other-project"))
            self.assertEqual(key_reads.call_count, 1)
            self.assertEqual(witness_reads.call_count, 1)
            self.assertEqual(signatures.call_count, 7)

    def test_snapshot_nested_signing_invalidates_outer_and_failed_witness_is_not_visible(self):
        import research_edits as evidence
        receipt = make_receipt("p", "a.tex", b"$x$", b"$x+z$")
        (self.state / "alias").mkdir()
        with evidence.evidence_snapshot():
            self.assertTrue(verify_receipt(receipt, "p"))
            with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state / "alias" / "..")}), \
                    evidence.evidence_snapshot():
                newer = make_receipt("p", "b.tex", b"$x$", b"$x+q$")
                self.assertTrue(verify_receipt(newer, "p"))
            self.assertTrue(verify_receipt(newer, "p"))
            payload = {"id": "failed-witness", "project_key": "p", "actor": "human"}
            with mock.patch.object(evidence.registry, "write_state", side_effect=OSError("fixture witness failure")):
                with self.assertRaisesRegex(OSError, "witness failure"):
                    evidence.sign_receipt(payload)
            forged = {**payload, "signature": hmac.new((self.state / KEY_FILE).read_bytes(),
                evidence._payload(payload), hashlib.sha256).hexdigest()}
            self.assertFalse(verify_receipt(forged, "p"))
            self.assertTrue(verify_receipt(receipt, "p"))

    def test_snapshot_exit_and_exception_observe_authority_revocation_on_next_read(self):
        import research_edits as evidence
        receipt = make_receipt("p", "a.tex", b"$x$", b"$x+z$")
        with self.assertRaisesRegex(RuntimeError, "fixture end"):
            with evidence.evidence_snapshot():
                self.assertTrue(verify_receipt(receipt, "p"))
                raise RuntimeError("fixture end")
        witness = self.state / WITNESS_FILE
        witness.unlink()
        self.assertFalse(verify_receipt(receipt, "p"))
        with evidence.evidence_snapshot():
            self.assertFalse(verify_receipt(receipt, "p"))

    def test_snapshot_authority_roots_and_request_threads_are_independent(self):
        from contextvars import copy_context
        import research_edits as evidence
        receipt = make_receipt("p", "a.tex", b"$x$", b"$x+z$")
        with evidence.evidence_snapshot():
            self.assertTrue(verify_receipt(receipt, "p"))
            with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state / "other-root")}):
                with evidence.evidence_snapshot():
                    self.assertFalse(verify_receipt(receipt, "p"))
            self.assertTrue(verify_receipt(receipt, "p"))
            (self.state / WITNESS_FILE).unlink()
            result = []
            def another_request():
                with evidence.evidence_snapshot():
                    result.append(verify_receipt(receipt, "p"))
            thread = threading.Thread(target=copy_context().run, args=(another_request,))
            thread.start(); thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [False])
            self.assertTrue(verify_receipt(receipt, "p"))
        self.assertFalse(verify_receipt(receipt, "p"))

    def test_snapshot_does_not_merge_case_distinct_authority_paths(self):
        import research_edits as evidence
        receipt = make_receipt("p", "a.tex", b"$x$", b"$x+z$")
        key = (self.state / KEY_FILE).read_bytes()
        witnesses = json.loads((self.state / WITNESS_FILE).read_text())["receipts"]
        upper, lower = self.state / "Authority", self.state / "authority"
        def material():
            return (key, witnesses) if evidence.registry.state_dir() == upper else (None, {})
        # Windows normcase also folds names in case-sensitive directories.
        with mock.patch.object(evidence.registry, "norm", side_effect=lambda path: str(path).lower()), \
                mock.patch.object(evidence, "_verification_material", side_effect=material), \
                mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(upper)}), \
                evidence.evidence_snapshot():
            self.assertTrue(verify_receipt(receipt, "p"))
            with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(lower)}):
                self.assertFalse(verify_receipt(receipt, "p"))

    def test_copied_context_cannot_reuse_authority_after_thread_identifier_reuse(self):
        from contextvars import copy_context
        import research_edits as evidence
        receipt = make_receipt("p", "a.tex", b"$x$", b"$x+z$")
        first, later = object(), object()
        with mock.patch.object(evidence.threading, "get_ident", return_value=123), \
                mock.patch.object(evidence.threading, "current_thread", return_value=first):
            with evidence.evidence_snapshot():
                self.assertTrue(verify_receipt(receipt, "p"))
                copied = copy_context()
        (self.state / WITNESS_FILE).unlink()
        with mock.patch.object(evidence.threading, "get_ident", return_value=123), \
                mock.patch.object(evidence.threading, "current_thread", return_value=later):
            self.assertFalse(copied.run(verify_receipt, receipt, "p"))

    def test_snapshot_missing_or_damaged_material_never_grants_authority(self):
        import research_edits as evidence
        receipt = make_receipt("p", "a.tex", b"$x$", b"$x+z$")
        for name in (KEY_FILE, WITNESS_FILE):
            path = self.state / name
            saved = path.read_bytes()
            with self.subTest(name=name):
                path.write_bytes(b"damaged")
                with evidence.evidence_snapshot():
                    self.assertFalse(verify_receipt(receipt, "p"))
                path.write_bytes(saved)
                with evidence.evidence_snapshot():
                    self.assertTrue(verify_receipt(receipt, "p"))

    def test_research_status_route_reuses_material_and_releases_it_before_response(self):
        from types import SimpleNamespace
        from repository import FilesLock
        import research_edits as evidence
        import server
        project = SimpleNamespace(root=self.state, files_lock=FilesLock(lambda: None))
        routes = ("/api/research", "/api/research/initialize", "/api/research/correct",
                  "/api/research/discussion", "/api/research/github/proposals")
        for route in routes:
            with self.subTest(route=route):
                receipt = make_receipt("p", "a.tex", b"$x$", b"$x+z$")
                knowledge, discussion, handler = mock.Mock(), mock.Mock(), mock.Mock()
                knowledge.project = project
                knowledge.status.return_value = {"initialized": True}
                handler._foreign_project.return_value = False
                changed = []

                def mutate(*args, **kwargs):
                    self.assertFalse(project.files_lock.held())
                    with mock.patch.object(evidence, "_key", wraps=evidence._key) as keys:
                        self.assertTrue(verify_receipt(receipt, "p"))
                        self.assertTrue(verify_receipt(receipt, "p"))
                        self.assertEqual(keys.call_count, 2)
                    changed.append(True)
                    return {"initialized": True}

                def render(*args):
                    self.assertEqual(changed, [True])
                    self.assertTrue(project.files_lock.held())
                    with mock.patch.object(evidence, "_key", wraps=evidence._key) as keys, \
                            mock.patch.object(evidence.registry, "read_state", wraps=evidence.registry.read_state) as witnesses:
                        for _ in range(20):
                            self.assertTrue(verify_receipt(receipt, "p"))
                        self.assertEqual((keys.call_count, witnesses.call_count), (1, 1))
                    return {"ready": True}

                def respond(view):
                    self.assertFalse(project.files_lock.held())
                    (self.state / WITNESS_FILE).unlink()
                    self.assertFalse(verify_receipt(receipt, "p"))
                    return view

                for method in (knowledge.refresh, knowledge.initialize, knowledge.correct,
                               discussion.configure, discussion.share_proposals):
                    method.side_effect = mutate
                handler._json.side_effect = respond
                with mock.patch.object(server, "PROJECT", project), \
                        mock.patch.object(server, "ResearchKnowledge", return_value=knowledge), \
                        mock.patch.object(server, "DISCUSSIONS", discussion), \
                        mock.patch.object(server.research_topics, "retry"), \
                        mock.patch.object(server, "research_view", side_effect=render):
                    if route == "/api/research":
                        result = server.Handler._get(handler, route, {})
                    else:
                        result = server.Handler._post(handler, route, {"project_key": evidence.registry.project_key(project.root)})
                    self.assertEqual(result, {"ready": True})

    def test_readable_signing_key_cannot_forge_an_editor_witness(self):
        receipt = make_receipt("p", "a.tex", b"$x$", b"$x+z$")
        forged = copy.deepcopy(receipt)
        forged["id"] = "forged-editor-action"
        forged["added"][0].update(notation="q", canonical="q")
        payload = json.dumps({k: v for k, v in forged.items() if k != "signature"},
                             ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        forged["signature"] = hmac.new((self.state / KEY_FILE).read_bytes(), payload, hashlib.sha256).hexdigest()
        self.assertFalse(verify_receipt(forged, "p"))
        self.assertTrue(verify_receipt(receipt, "p"))
        (self.state / WITNESS_FILE).unlink()
        self.assertFalse(verify_receipt(receipt, "p"))

    def test_damaged_local_key_is_not_silently_replaced(self):
        path = self.state / KEY_FILE
        path.write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "damaged"):
            make_receipt("p", "a.tex", b"$x$", b"$x+z$")
        self.assertEqual(path.read_bytes(), b"damaged")
