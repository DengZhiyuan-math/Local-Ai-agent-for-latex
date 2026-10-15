"""Opt-in real Codex tool execution against a local scripted Responses server.

PRISM_NATIVE_CODEX=1 enables host sandbox startup. No login or AI API is used.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from backend_codex import Codex
from backends import Job


@unittest.skipUnless(os.name == "nt" and os.environ.get("PRISM_NATIVE_CODEX"),
                     "opt-in Windows sandbox startup")
class CodexRuntime(unittest.TestCase):
    def probe(self, root, mode, scope, *, omit_windows=False, patch_path=None,
              script=None, session_id=None, home=None, persist=False, server_url=None,
              tool_call=None):
        outputs, requests = [], []
        call_id = "probe_" + uuid.uuid4().hex
        script = script or """$result = @{ read = (Get-Content -Raw main.tex).Trim() }
foreach ($name in @('main.tex', 'other.tex', '.git/protected')) {
    try {
        Set-Content -LiteralPath $name -Value 'changed' -ErrorAction Stop
        $result[$name] = 'allowed'
    } catch { $result[$name] = 'denied' }
}
$result | ConvertTo-Json -Compress
"""

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(data)
                outputs.extend(i["output"] for i in data.get("input", [])
                               if i.get("type") in ("function_call_output", "custom_tool_call_output"))
                if len(requests) == 1:
                    if isinstance(tool_call, dict):
                        item = {"id": "fc_" + call_id, "call_id": call_id, **tool_call}
                    elif tool_call:
                        item = {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id,
                                "name": tool_call[0], "arguments": json.dumps(tool_call[1])}
                    elif patch_path:
                        item = {"type": "custom_tool_call", "id": "fc_" + call_id, "call_id": call_id,
                                "name": "apply_patch", "input": "*** Begin Patch\n"
                                f"*** Update File: {patch_path}\n@@\n-original\n+changed\n*** End Patch"}
                    else:
                        item = {"type": "function_call", "id": "fc_" + call_id, "call_id": call_id,
                                "name": "exec_command", "arguments": json.dumps({
                                    "cmd": script, "max_output_tokens": 1000})}
                else:
                    item = {"type": "message", "id": "msg_probe", "role": "assistant",
                            "content": [{"type": "output_text", "text": "Probe complete."}]}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = [
                    {"type": "response.created", "response": {"id": "resp_probe",
                                                                "status": "in_progress"}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {"id": "resp_probe",
                        "status": "completed", "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
                ]
                for event in events:
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

        backend = Codex("codex")
        if not backend.bin():
            self.skipTest("Codex CLI is not installed")
        job = Job(1)
        job.root, job.mode, job.scope = root, mode, scope
        job.session_id, job.server_url = session_id, server_url
        command, _ = backend.command(job)
        if omit_windows:
            index = command.index('windows.sandbox="elevated"')
            del command[index - 1:index + 1]
        # Keep native helper caches and sessions away from the user's active CLI/app.
        with tempfile.TemporaryDirectory(prefix="prism-codex-home-") as probe_home, \
                ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            # Use known metadata so Codex registers the native apply_patch tool.
            # The model response still comes entirely from the local fixture.
            # Resume has a positional id/prompt: put all overrides before its subcommand.
            index = command.index("resume") if session_id else len(command) - 1
            command[index:index] = ([] if persist else ["--ephemeral"]) + ["-m", "gpt-6.1-sol", "-c",
                'model_provider="probe"', "-c", 'features.code_mode=false', "-c",
                'model_providers.probe={name="Probe",'
                f'base_url="http://127.0.0.1:{server.server_port}/v1",'
                'wire_api="responses",requires_openai_auth=false}']
            try:
                env = dict(os.environ)
                env["CODEX_HOME"] = str(home or probe_home)
                result = subprocess.run(command, cwd=root, input="Run the fixture tool.",
                    capture_output=True, text=True, encoding="utf-8", timeout=45, env=env)
            finally:
                server.shutdown()
                thread.join(5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(outputs, result.stdout + result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
        self.last_probe = {"requests": requests, "stderr": result.stderr,
                           "session_id": next((e["thread_id"] for e in events
                                                if e.get("type") == "thread.started"), None)}
        return "\n".join(o if isinstance(o, str) else json.dumps(o) for o in outputs)

    def fixture(self, root):
        for name in ("main.tex", "other.tex", ".git/protected"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("original", encoding="utf-8")

    def test_missing_windows_setting_reproduces_policy_rejection(self):
        with tempfile.TemporaryDirectory(prefix="prism-codex-probe-") as tmp:
            root = Path(tmp)
            self.fixture(root)
            output = self.probe(root, "ask", None, omit_windows=True)
            self.assertIn("blocked by policy", output)
            self.assertEqual((root / "main.tex").read_text(), "original")

    def test_reads_work_and_native_writes_follow_each_turn_scope(self):
        cases = [("ask", None, "denied", "denied"),
                 ("edit", ["main.tex"], "allowed", "denied"),
                 ("edit", None, "allowed", "allowed")]
        for mode, scope, main, other in cases:
            with self.subTest(mode=mode, scope=scope), \
                    tempfile.TemporaryDirectory(prefix="prism-codex-probe-") as tmp:
                root = Path(tmp)
                self.fixture(root)
                output = self.probe(root, mode, scope)
                # exec_command wraps the command's stdout in its tool result.
                record = next((json.loads(line) for line in output.splitlines()
                               if line.startswith('{"')), None)
                self.assertIsNotNone(record, output)
                self.assertEqual(record, {"read": "original", "main.tex": main,
                                          "other.tex": other, ".git/protected": "denied"})
                for name, access in (("main.tex", main), ("other.tex", other),
                                     (".git/protected", "denied")):
                    self.assertEqual((root / name).read_text().strip(),
                                     "changed" if access == "allowed" else "original")

    def test_native_apply_patch_edits_scoped_file_and_denies_other_files(self):
        for name, expected in (("main.tex", "changed"), ("other.tex", "original")):
            with self.subTest(path=name), \
                    tempfile.TemporaryDirectory(prefix="prism-codex-probe-") as tmp:
                root = Path(tmp)
                self.fixture(root)
                output = self.probe(root, "edit", ["main.tex"], patch_path=name)
                self.assertEqual((root / name).read_text().strip(), expected, output)
                self.assertEqual((root / ".git/protected").read_text(), "original")
                if expected == "changed":
                    self.assertIn("Success", output)
                else:
                    self.assertIn("rejected", output.lower())

    def test_existing_nested_metadata_and_explicit_metadata_scopes_stay_read_only(self):
        for name, scope in (("nested/.git/protected", None),
                            ("nested/.agents/protected", None),
                            (".codex/protected", [".codex/protected"]),
                            ("nested/.aws/protected", ["nested/.aws/protected"])):
            with self.subTest(path=name, scope=scope), \
                    tempfile.TemporaryDirectory(prefix="prism-codex-probe-") as tmp:
                root = Path(tmp)
                self.fixture(root)
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original", encoding="utf-8")
                output = self.probe(root, "edit", scope, patch_path=name)
                self.assertEqual(path.read_text(), "original", output)
                self.assertIn("rejected", output.lower())

    def test_resuming_replaces_earlier_full_edit_permissions_with_this_turn_scope(self):
        with tempfile.TemporaryDirectory(prefix="prism-codex-probe-") as tmp:
            base = Path(tmp)
            root, home = base / "paper with spaces", base / "codex-home"
            root.mkdir()
            home.mkdir()
            sid = None
            for mode, scope, expected in (("edit", None, "changed"),
                                           ("ask", None, "original"),
                                           ("edit", ["other.tex"], "original")):
                self.fixture(root)
                output = self.probe(root, mode, scope, patch_path="main.tex", home=home,
                                    persist=True, session_id=sid)
                self.assertEqual((root / "main.tex").read_text().strip(), expected, output)
                sid = self.last_probe["session_id"]
                self.assertTrue(sid)
                self.assertIsNone(self.session_check(home, root, sid))

    def session_check(self, home, root, sid):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            return Codex("codex").check_session(root, sid)

    def test_full_project_edit_cannot_patch_a_sibling_outside_the_project(self):
        with tempfile.TemporaryDirectory(prefix="prism-codex-probe-") as tmp:
            base = Path(tmp)
            root, outside = base / "paper with spaces", base / "outside.tex"
            root.mkdir()
            self.fixture(root)
            outside.write_text("original", encoding="utf-8")
            output = self.probe(root, "edit", None, patch_path=str(outside))
            self.assertEqual(outside.read_text(), "original", output)
            self.assertIn("rejected", output.lower())

    def test_edit_compile_mcp_works_with_native_command_network_disabled(self):
        builds = []

        class BuildHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                builds.append((self.path, json.loads(self.rfile.read(
                    int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"exit": 0, "diagnostics": []}).encode())

        with tempfile.TemporaryDirectory(prefix="prism-codex-probe-") as tmp, \
                ThreadingHTTPServer(("127.0.0.1", 0), BuildHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                root = Path(tmp)
                self.fixture(root)
                output = self.probe(root, "edit", ["main.tex"],
                    server_url=f"http://127.0.0.1:{server.server_port}/",
                    tool_call={"type": "custom_tool_call", "name": "exec", "namespace": "functions",
                               "input": "const result = await tools.mcp__prism__compile({}); text(result);"})
            finally:
                server.shutdown()
                thread.join(5)
            self.assertEqual(builds, [("/api/build", {"mode": "draft", "clean": False, "by": "agent"})], output)
            self.assertIn("Build OK", output)

    def test_user_mcp_configuration_is_ignored_without_modifying_it(self):
        with tempfile.TemporaryDirectory(prefix="prism-codex-probe-") as tmp:
            base = Path(tmp)
            root, home, marker = base / "project", base / "codex-home", base / "unwanted-started"
            root.mkdir()
            home.mkdir()
            self.fixture(root)
            config = ("[features]\nhooks=true\n[mcp_servers.unwanted]\ncommand=" +
                      json.dumps(sys.executable) + "\nargs=" + json.dumps(["-c",
                      f"from pathlib import Path; Path({str(marker)!r}).touch()"])
                      + "\nrequired=true\n")
            path = home / "config.toml"
            path.write_text(config, encoding="utf-8")
            output = self.probe(root, "ask", None, home=home, tool_call={
                "type": "custom_tool_call", "name": "exec", "namespace": "functions",
                "input": 'text(ALL_TOOLS.filter(t => t.name.startsWith("mcp__")).map(t => t.name));'})
            self.assertIn("Script completed", output)
            self.assertNotIn("unwanted", output)
            self.assertFalse(marker.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), config)


if __name__ == "__main__":
    unittest.main()
