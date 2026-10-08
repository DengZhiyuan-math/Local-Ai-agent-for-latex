"""Regression checks for the editor's backend contracts. No external AI calls."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from backend_openai import OpenAICompat, ToolError
from test_agent import job_for
from test_agent import manager, wait_done
from test_agent import FakeAPI
from backends import Backend
from backend_codex import Codex
from backend_deepcode import DeepCode
import json
import subprocess
import os
import server
import mcp_compile
from unittest import mock


class ProjectPaths(unittest.TestCase):
    def test_search_and_catalog_cannot_read_external_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "project"
            root.mkdir()
            (root / "main.tex").write_text("INSIDE\n")
            (base / "outside.tex").write_text("OUTSIDE_SECRET\n")
            (root / "external.tex").symlink_to(base / "outside.tex")
            (root / "internal.tex").symlink_to(root / "main.tex")
            (root / "broken.tex").symlink_to(root / "missing.tex")
            old_root = server.ROOT
            self.addCleanup(server.set_root, old_root)
            server.set_root(root)
            api = OpenAICompat("api", {"base_url": "http://unused"})
            # Check even a stale or independently supplied catalog.
            job = job_for(root=root, files=lambda: ["external.tex", "internal.tex", "broken.tex"])
            self.assertNotIn("OUTSIDE_SECRET", api.tool(job, "search", {"pattern": "."}, []))
            self.assertIn("INSIDE", api.tool(job, "read_file", {"path": "internal.tex"}, []))
            with self.assertRaises(ToolError):
                api.tool(job, "read_file", {"path": "external.tex"}, [])
            self.assertNotIn("external.tex", server.list_files())
            self.assertNotIn("broken.tex", server.list_files())
            self.assertIn("internal.tex", server.list_files())


class ChangeCoverage(unittest.TestCase):
    def test_scope_and_ask_restore_binary_hidden_and_build_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = ["main.tex", "figure.png", "script.py", ".hidden/x", "build/x.tex"]
            class Wild(Backend):
                enforces_scope = False
                read_only_ask = False
                def run(self, job):
                    for rel in paths:
                        (job.root / rel).write_bytes(b"changed")
                    return {"exit": 0}
            m = manager(root, lambda: ["main.tex"], wild=Wild("wild"))
            for mode in ("edit", "ask"):
                for rel in paths:
                    (root / rel).parent.mkdir(parents=True, exist_ok=True)
                    (root / rel).write_bytes(b"original")
                done = wait_done(m.jobs[m.start("fixture", None, mode, scope=["main.tex"])["job"]])
                self.assertEqual(done["reverted"], sorted(paths[1:] if mode == "edit" else paths))
                self.assertEqual([c["path"] for c in done["changed"]], ["main.tex"] if mode == "edit" else [])

    def test_binary_hidden_and_excluded_changes_are_reported_and_undone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            originals = {"main.tex": b"original\r\n", "figure.png": b"\x89PNG\x00\xff",
                         "script.py": b"print(1)\n", ".hidden/x": b"hidden",
                         "build/x.tex": b"build", "node_modules/x": b"dependency"}
            for rel, data in originals.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_bytes(data)

            class Edit(Backend):
                def run(self, job):
                    for rel in originals:
                        (job.root / rel).write_bytes(b"changed")
                    (job.root / "created.png").write_bytes(b"new")
                    return {"exit": 0}

            m = manager(root, lambda: ["main.tex"], edit=Edit("edit"))
            done = wait_done(m.jobs[m.start("edit", None, "edit")["job"]])
            self.assertEqual({c["path"] for c in done["changed"]}, {*originals, "created.png"})
            self.assertEqual(set(m.undo(done["turn"])["restored"]), {*originals, "created.png"})
            self.assertFalse((root / "created.png").exists())
            for rel, data in originals.items():
                self.assertEqual((root / rel).read_bytes(), data)

    def test_undo_restores_a_replaced_link_without_touching_its_external_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "project"
            root.mkdir()
            (root / "main.tex").write_bytes(b"original")
            outside = base / "outside.tex"
            outside.write_bytes(b"private")

            class Replace(Backend):
                def run(self, job):
                    (job.root / "main.tex").unlink()
                    (job.root / "main.tex").symlink_to(outside)
                    return {"exit": 0}

            m = manager(root, replace=Replace("replace"))
            done = wait_done(m.jobs[m.start("replace", None, "edit")["job"]])
            self.assertEqual(m.undo(done["turn"])["restored"], ["main.tex"])
            self.assertEqual((root / "main.tex").read_bytes(), b"original")
            self.assertEqual(outside.read_bytes(), b"private")

    def test_large_file_changes_are_detected_and_marked_as_not_undoable(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("agent.MAX_SNAPSHOT_FILE", 4):
            root = Path(tmp)
            (root / "large.bin").write_bytes(b"old-content")

            class Replace(Backend):
                def run(self, job):
                    (job.root / "large.bin").write_bytes(b"new-content")
                    return {"exit": 0}

            m = manager(root, replace=Replace("replace"))
            done = wait_done(m.jobs[m.start("replace", None, "edit")["job"]])
            self.assertEqual(done["undo_unavailable"], ["large.bin"])
            self.assertEqual(m.undo(done["turn"])["skipped"], ["large.bin"])
            self.assertEqual((root / "large.bin").read_bytes(), b"new-content")


class DefaultModel(unittest.TestCase):
    def test_missing_executable_cannot_start_a_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = Codex("codex", {"bin": str(Path(tmp) / "missing")})
            self.assertFalse(backend.info()["available"])
            m = manager(Path(tmp), codex=backend)
            self.assertIn("not found", m.start("hi", None, "ask")["error"])
            self.assertIsNone(m.active)
    def test_configured_model_is_checked_and_used_unless_overridden(self):
        class Capture(Backend):
            def check(self, model, effort):
                self.checked = model
                return super().check(model, effort)

            def run(self, job):
                self.used = job.model
                return {"exit": 0}

        with tempfile.TemporaryDirectory() as tmp:
            backend = Capture("cli", {"default_model": "configured-model"})
            m = manager(Path(tmp), cli=backend)
            for model, expected in ((None, "configured-model"), ("explicit-model", "explicit-model")):
                wait_done(m.jobs[m.start("hi", None, "ask", model=model)["job"]])
                self.assertEqual(backend.checked, expected)
                self.assertEqual(backend.used, expected)
            backend.default_model = "--invalid"
            self.assertIn("Not a model name", m.start("hi", None, "ask")["error"])


class CodexPermissions(unittest.TestCase):
    def test_command_policy_enforces_scope_with_the_native_sandbox(self):
        codex = Codex("codex")
        if not codex.bin():
            self.skipTest("Codex CLI is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for rel in ("main.tex", "other.tex", "figure.png"):
                (root / rel).write_text("original")
            job = job_for(root=root, mode="edit", scope=["main.tex"])
            cmd, _ = codex.command(job)
            self.assertNotIn("--sandbox", cmd, "legacy sandbox conflicts with named profiles")
            config = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-c"]
            self.assertIn('default_permissions="prism-editor"', config)
            if not os.environ.get("PRISM_NATIVE_CODEX"):
                return  # Native sandbox startup requires host permission; opt in explicitly.
            program = """import json
from pathlib import Path
result = {}
for name in ('main.tex', 'other.tex', 'figure.png'):
    try:
        Path(name).write_text('changed')
        result[name] = 'allowed'
    except PermissionError:
        result[name] = 'denied'
print(json.dumps(result))
"""
            sandbox = [codex.bin(), "sandbox", "-P", "prism-editor", "-C", str(root)]
            for value in config:
                if value.startswith("permissions."):
                    sandbox += ["-c", value]
            p = subprocess.run(sandbox + ["--", sys.executable, "-B", "-c", program],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout), {"main.tex": "allowed", "other.tex": "denied",
                                                   "figure.png": "denied"})


class CompileContract(unittest.TestCase):
    def test_cli_compile_is_only_registered_for_edit_and_errors_are_preserved(self):
        codex = Codex("codex", {"bin": "codex"})
        for mode in ("edit", "ask"):
            cmd, _ = codex.command(job_for(mode=mode, server_url="http://127.0.0.1:9/"))
            self.assertEqual(any("mcp_compile.py" in arg for arg in cmd), mode == "edit")
        api = OpenAICompat("api", {"base_url": "http://unused"})
        job = job_for(mode="edit", server_url="http://127.0.0.1:9/")
        with mock.patch("mcp_compile.build", return_value={"exit": 1, "diagnostics": []}):
            reply = api._call(job, {"id": "c", "name": "compile", "arguments": "{}"}, [])
        self.assertIn("Build FAILED", reply)
        self.assertTrue(job.events[-1]["error"])
        codex.handle({"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "c",
                     "status": "completed", "result": {"isError": True, "content": []}}}, job, {})
        self.assertTrue(job.events[-1]["error"])


class DeepCodeContract(unittest.TestCase):
    def test_resumed_turn_refreshes_native_tools_and_project_key_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".deepcode").mkdir()
            (root / ".deepcode/settings.json").write_text(json.dumps({"env": {"API_KEY": "fixture"}}))
            backend = DeepCode("deepcode", {"bin": sys.executable, "default_model": "default-model"})
            with mock.patch.dict(os.environ, {"DEEPCODE_API_KEY": "", "DEEPSEEK_API_KEY": ""}), \
                    mock.patch("backend_deepcode.user_settings", return_value={}):
                self.assertIsNone(backend.preflight(root))
            job = job_for(root=root, session_id="old", mode="ask")
            prompt = backend.message(job)
            self.assertIn("[Instructions from the editor]", prompt)
            self.assertNotIn("Read, Grep, Glob, Edit", prompt)
            self.assertIn("snippet", prompt)
            self.assertIn("default-model", backend.env(job).values())


class VisualAttachments(unittest.TestCase):
    def test_api_receives_image_content_and_unsupported_inputs_are_rejected(self):
        api = FakeAPI([[{"choices": [{"delta": {"content": "image received"}}]}]])
        self.addCleanup(api.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = b"\x89PNG\r\n\x1a\n\x00fixture"
            (root / "figure.png").write_bytes(image)
            backend = OpenAICompat("api", {"base_url": f"http://127.0.0.1:{api.server_port}",
                "default_model": "vision-fixture", "input_types": ["text", "image"]})
            m = manager(root, api=backend)
            done = wait_done(m.jobs[m.start("describe", None, "ask", attachments=["figure.png"])["job"]])
            self.assertEqual(done["exit"], 0)
            content = api.requests[0]["body"]["messages"][-1]["content"]
            self.assertEqual(content[0]["type"], "text")
            self.assertTrue(content[0]["text"].startswith("describe"))
            self.assertIn("figure.png", content[1]["text"])
            self.assertTrue(content[2]["image_url"]["url"].startswith("data:image/png;base64,"))
            backend.input_types = ("text",)
            self.assertIn("does not support image", m.start("describe", None, "ask",
                                                          attachments=["figure.png"])["error"])


class FrontendReferences(unittest.TestCase):
    def test_unknown_slash_command_keeps_selection_text(self):
        import shutil
        if not shutil.which("node"):
            self.skipTest("Node is not installed")
        source = (Path(__file__).resolve().parents[1] / "prism_local/static/app.js").read_text()
        start = source.index("function slashPrompt(")
        end = source.index("\n}", start) + 2
        script = "const catalog={skills:[]};\n" + source[start:end] \
            + '\nprocess.stdout.write(slashPrompt("/review @main.tex:1-2", "SELECTED_TEXT"));'
        p = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(p.stdout, "/review @main.tex:1-2\n\nSELECTED_TEXT")


if __name__ == "__main__":
    unittest.main()
