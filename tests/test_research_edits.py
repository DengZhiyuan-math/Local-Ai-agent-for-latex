"""Input capabilities and durable receipt authenticity, with isolated user state."""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import tempfile
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
