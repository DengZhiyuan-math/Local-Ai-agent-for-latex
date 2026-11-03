"""Provenance is compiler-recorded, read-only and local to the successful result."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import build_results
from project import Project
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import tmpdir


DOC = r"\documentclass{article}\begin{document}\input{section}\end{document}"


class Results(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir()
        (self.root / "main.tex").write_text(DOC)
        (self.root / "section.tex").write_text("Some words.")
        (self.root / "unrelated.txt").write_text("Unrelated.")
        self.project = Project(self.root)

    def compile(self, during=lambda: None, *, exit=0, recorder=True, kind="builtin", extra="",
                diagnostics=None):
        root = self.root

        class Fake:
            def __init__(self, *args, **kwargs):
                pass

            def run(self):
                during()
                (root / "build").mkdir(exist_ok=True)
                (root / "build/main.pdf").write_bytes(b"%PDF synthetic")
                if recorder:
                    if kind == "tectonic":
                        text = "build/main.pdf : main.tex section.tex" + extra + "\n"
                        (root / "build/main.prism-deps.mk").write_text(text)
                    else:
                        text = "INPUT main.tex\nINPUT section.tex\nINPUT build/main.aux\n" + extra
                        (root / "build/main.fls").write_text(text)
                return {"exit": exit, "cancelled": False, "timed_out": False,
                        "builder": kind, "engine": "synthetic", "diagnostics": diagnostics or [],
                        "output": ""}

        self.project.builder = Fake
        return self.project.build("draft")

    def test_receipt_only_includes_actual_compiler_inputs_and_survives_reopen(self):
        self.compile()
        receipt = build_results.read(self.project.config)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(set(receipt["files"]), {"main.tex", "section.tex"})
        (self.root / "unrelated.txt").write_text("Different")
        (self.root / "prism.json").write_text('{"engine":"xelatex"}')
        self.assertEqual(build_results.read(Project(self.root).config), receipt)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(build_results.read(self.project.config), receipt)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_input_edit_and_undo_during_build_has_no_success_proof(self):
        for undo in (False, True):
            original = (self.root / "section.tex").read_bytes()

            def during():
                (self.root / "section.tex").write_text("Changed during build")
                if undo:
                    (self.root / "section.tex").write_bytes(original)

            self.compile(during)
            self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_unrelated_change_during_build_does_not_revoke_proof(self):
        self.compile(lambda: (self.root / "unrelated.txt").write_text("Changed"))
        self.assertEqual(build_results.read(self.project.config)["status"], "complete")

    def test_oversized_unrelated_file_does_not_revoke_proof(self):
        (self.root / "unused-video.bin").write_bytes(b"x" * 4_000_001)
        self.compile()
        self.assertEqual(build_results.read(self.project.config)["status"], "complete")

    def test_unrelated_symlink_does_not_revoke_proof(self):
        (self.root / "unused-link").symlink_to(self.root / "missing")
        self.compile()
        self.assertEqual(build_results.read(self.project.config)["status"], "complete")

    def test_excess_unrelated_notes_do_not_revoke_small_actual_input_set(self):
        for index in range(build_results.MAX_FILES + 1):
            (self.root / f"note-{index}.md").write_text("Unused note")
        self.compile()
        self.assertEqual(build_results.read(self.project.config)["status"], "complete")

    def test_actual_oversized_input_stays_unsupported(self):
        (self.root / "section.tex").write_bytes(b"x" * 4_000_001)
        self.compile()
        self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_external_tool_feedback_has_no_complete_input_proof(self):
        (self.root / "refs.bib").write_text("Bibliography input")
        for suffix in ("bbl", "ind", "gls", "acr", "nls", "alg"):
            with self.subTest(suffix=suffix):
                self.compile(extra=f"INPUT build/main.{suffix}\n")
                self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_error_diagnostic_prevents_success_even_when_engine_exit_is_zero(self):
        self.compile(diagnostics=[{"severity": "error", "message": "bibtex tool failed"}])
        self.assertEqual(build_results.read(self.project.config)["status"], "failed")

    def test_unrelated_check_command_does_not_reject_native_draft_result(self):
        (self.root / "prism.json").write_text('{"build":{"check":"unused-check"}}')
        self.compile()
        self.assertEqual(build_results.read(self.project.config)["status"], "complete")

    def test_actual_custom_builder_still_has_no_dependency_proof(self):
        self.compile(kind="custom")
        self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_skipped_actual_dependency_is_unknown_instead_of_silently_omitted(self):
        with mock.patch.object(build_results, "MAX_CANDIDATES", 1):
            self.compile()
        self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_stale_recorder_is_removed_and_never_reused(self):
        self.compile()
        self.compile(recorder=False)
        self.assertEqual(build_results.read(self.project.config)["status"], "unknown")
        self.assertFalse((self.root / "build/main.fls").exists())

    def test_failed_build_preserves_previous_receipt_and_pdf_history(self):
        self.compile()
        old = build_results.read(self.project.config)
        old_path = self.root / ".prism-local/build-results" / (old["id"] + ".json")
        saved = old_path.read_bytes()
        self.compile(exit=1)
        self.assertEqual(build_results.read(self.project.config)["status"], "failed")
        self.assertEqual(old_path.read_bytes(), saved)

    def test_source_change_after_success_does_not_mislabel_pdf(self):
        self.compile()
        (self.root / "main.tex").write_text("Later source")
        self.assertEqual(build_results.read(self.project.config)["status"], "source_changed")
        self.assertTrue((self.root / "build/main.pdf").exists())

    def test_replaced_pdf_is_unknown(self):
        self.compile()
        (self.root / "build/main.pdf").write_bytes(b"%PDF replaced")
        self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_nonobject_receipt_is_unknown_and_read_only(self):
        self.compile()
        path = build_results._latest(self.root, self.project.config.main)
        for raw in ("null", "[]", "true", "42", '"invalid"'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                self.assertEqual(build_results.read(self.project.config)["status"], "unknown")
                self.assertEqual(path.read_text(), raw)

    def test_building_result_never_exposes_previous_success(self):
        self.compile()
        self.assertEqual(build_results.read(self.project.config, building=True)["status"], "building")

    def test_unknown_external_local_input_is_unsupported(self):
        self.compile(extra="INPUT /private/tmp/another-user-file.tex\n")
        self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_system_distribution_inputs_are_historical_environment(self):
        distribution = ("C:/Program Files/MiKTeX/tex/latex/base/article.cls" if sys.platform == "win32"
                        else "/usr/local/texlive/2026/texmf-dist/tex/latex/base/article.cls")
        self.compile(extra=f"INPUT {distribution}\n")
        self.assertEqual(build_results.read(self.project.config)["status"], "complete")

    def test_tectonic_uses_native_rules_and_does_not_assume_fls(self):
        self.compile(kind="tectonic")
        self.assertEqual(build_results.read(self.project.config)["status"], "complete")
        self.assertFalse((self.root / "build/main.fls").exists())
        self.compile(kind="tectonic", recorder=False)
        self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_tectonic_rules_with_ambiguous_output_source_are_unsupported(self):
        self.compile(kind="tectonic", extra=" build/section.tex")
        self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_untracked_source_in_output_is_not_ignored_as_generated_feedback(self):
        self.compile(extra="INPUT build/previous.tex\n")
        self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_interrupted_builder_marks_result_unknown_without_retry(self):
        self.compile()
        builder = mock.Mock()
        builder.return_value.run.side_effect = OSError("interrupted build")
        self.project.builder = builder
        with self.assertRaises(OSError):
            self.project.build("draft")
        self.assertEqual(builder.return_value.run.call_count, 1)
        self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_symlink_inputs_never_become_dependencies(self):
        other = tmpdir() / "other.tex"
        other.write_text("External")
        (self.root / "section.tex").unlink()
        (self.root / "section.tex").symlink_to(other)
        self.compile()
        self.assertEqual(build_results.read(self.project.config)["status"], "unknown")

    def test_native_flags_record_inputs_without_changing_build_mode(self):
        import build
        job = build.Build(self.root, "main.tex", "build", "strict", engine="pdflatex")
        job.engine = "pdflatex"
        self.assertIn("-recorder", job._engine_argv())
        for builder in ("tectonic", "latexmk"):
            job.builder = builder
            job._step = mock.Mock(return_value=(0, ""))
            job._which = lambda name: name
            job._command()
            argv = job._step.call_args.args[1]
            self.assertIn("--makefile-rules" if builder == "tectonic" else "-recorder", argv)


class Rules(unittest.TestCase):
    def test_spaces_and_line_continuations_are_not_split_into_fake_inputs(self):
        self.assertEqual(build_results._make_words("build/a.pdf : a\\ b.tex \\\n c.tex\n"), ["a b.tex", "c.tex"])

    def test_make_variables_and_extra_rules_are_unsupported(self):
        for text in ("out : $(PRIVATE)", "out : a\nmore : b", "out : a#comment"):
            with self.assertRaises(ValueError):
                build_results._make_words(text)


class HTTPResult(unittest.TestCase):
    def test_get_is_read_only_and_exposes_no_current_build(self):
        import server
        from test_lifecycle import request
        from http.server import ThreadingHTTPServer
        from threading import Thread
        root = tmpdir()
        (root / "main.tex").write_text(DOC)
        server.set_root(root)
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{http.server_port}"
        try:
            status, result = request(url, "/api/build/result?main=main.tex")
            self.assertEqual((status, result["status"]), (200, "missing"))
            self.assertFalse((root / ".prism-local").exists())
            self.assertEqual(request(url, "/api/build/result?main=other.tex")[0], 409)
        finally:
            http.shutdown()
            thread.join(5)
            http.server_close()

    def test_conditional_write_has_a_separate_endpoint_and_preserves_conflicting_manual_text(self):
        import hashlib
        import json
        import server
        from test_lifecycle import request
        from http.server import ThreadingHTTPServer
        from threading import Thread
        root = tmpdir()
        (root / "main.tex").write_text("Original source")
        server.set_root(root)
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{http.server_port}"
        headers = {"Content-Type": "application/json", "X-Prism-Local": "1"}
        payload = {"path": "main.tex", "content": "Corrected source",
                   "expected_sha256": hashlib.sha256(b"Original source").hexdigest()}
        try:
            (root / "main.tex").write_text("Manual source")
            status, result = request(url, "/api/file/conditional", json.dumps(payload).encode(), headers)
            self.assertEqual((status, result["conflict"]), (409, True))
            self.assertEqual((root / "main.tex").read_text(), "Manual source")
            payload["expected_sha256"] = hashlib.sha256(b"Manual source").hexdigest()
            status, result = request(url, "/api/file/conditional", json.dumps(payload).encode(), headers)
            self.assertEqual((status, result["ok"]), (200, True))
            self.assertEqual((root / "main.tex").read_text(), "Corrected source")
        finally:
            http.shutdown()
            thread.join(5)
            http.server_close()
