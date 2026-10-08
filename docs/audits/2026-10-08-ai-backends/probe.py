"""Offline AI backend contract probes. No AI API or external repository is used.

Run from the checkout: python3 docs/audits/2026-10-08-ai-backends/probe.py
The JSON output records current behavior, including demonstrated defects.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "prism_local"))
sys.path.insert(0, str(REPO / "tests"))

import backends
import server
from backend_claude import ClaudeCode
from backend_codex import Codex
from backend_deepcode import DeepCode
from backend_openai import OpenAICompat, ToolError
from test_agent import job_for, manager, wait_done


def main():
    results = {}
    with tempfile.TemporaryDirectory(prefix="prism-ai-audit-") as tmp:
        base = Path(tmp).resolve()
        root = base / "project"
        root.mkdir()
        (root / "main.tex").write_text("original\n", encoding="utf-8")
        job = job_for(root=root, mode="edit", prompt="Read and edit main.tex",
                      server_url="http://127.0.0.1:9/")
        cc = ClaudeCode("claude", {"bin": "claude", "default_model": "audit-default"})
        cx = Codex("codex", {"bin": "codex", "default_model": "audit-default"})
        dc = DeepCode("deepcode", {"bin": "deepcode", "default_model": "audit-default"})
        with mock.patch("backend_claude.use_config_dir"), \
                mock.patch("backend_claude.shell_rules", return_value=[]), \
                mock.patch("backend_deepcode.user_settings", return_value={}):
            cc_cmd, _ = cc.command(job)
            cx_cmd, cx_prompt = cx.command(job)
            dc_env = dc.env(job)
        results["cli_default_model_applied"] = {
            "claude": "audit-default" in cc_cmd,
            "codex": "audit-default" in cx_cmd,
            "deepcode": dc_env.get("DEEPCODE_MODEL") == "audit-default",
        }
        results["compile_registration"] = {
            "claude": "--mcp-config" in cc_cmd,
            "codex": any(job.server_url in arg for arg in cx_cmd),
        }
        results["codex_native_tool_instructions"] = {
            "allows_read_only_shell": "read-only shell commands" in cx_prompt,
            "uses_patch": "apply_patch" in cx_prompt,
            "claude_shell_ban_present": "not shell commands such as" in cx_prompt,
        }
        results["deepcode_tool_instructions"] = {
            "still_names_claude_tools": "Read, Grep, Glob, Edit," in dc.message(job),
            "refreshes_editor_instructions_on_resume": "[Instructions from the editor]" in
                dc.message(job_for(root=root, session_id="audit-session")),
        }
        job.events.clear()
        cx.handle({"type": "item.completed", "item": {
            "type": "mcp_tool_call", "id": "mcp-failure", "server": "prism",
            "tool": "compile", "status": "completed",
            "result": {"isError": True, "content": []}}}, job, {})
        results["codex_mcp_semantic_error"] = {
            "tool_result_error": job.events[-1]["error"],
            "synthetic_event": True,
        }
        results["missing_bin_reported_available"] = Codex(
            "codex", {"bin": str(base / "does-not-exist")}).info()["available"]

        class CaptureAPI(OpenAICompat):
            def _complete(self, job, model, msgs, tools):
                self.sent = {"model": model, "messages": msgs, "tools": tools}
                return "done", [], "", {}

        api = CaptureAPI("api", {"base_url": "http://127.0.0.1:9/v1",
                                 "default_model": "audit-default"})
        (root / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\x00fixture")
        job.prompt = "[Attached files] Read prism-uploads/figure.png with your file tools."
        api.run(job)
        results["api_request"] = {
            "default_model_applied": api.sent["model"] == "audit-default",
            "compile_tool_registered": any(t["function"]["name"] == "compile"
                                           for t in api.sent["tools"]),
            "all_message_content_is_text": all(isinstance(m.get("content"), str)
                                               for m in api.sent["messages"]),
        }
        job.files = lambda: ["main.tex", "figure.png"]
        binary_text = api.tool(job, "read_file", {"path": "figure.png"}, [])
        results["api_binary_read"] = {
            "returns_text": isinstance(binary_text, str),
            "has_replacement_characters": "\ufffd" in binary_text,
        }
        job.events.clear()
        with mock.patch("backend_openai.mcp_compile.build", return_value={
                "exit": 1, "diagnostics": [{"severity": "error", "file": "main.tex",
                                             "line": 1, "message": "fixture error"}]}):
            reply = api._call(job, {"id": "compile-1", "name": "compile", "arguments": "{}"}, [])
        results["api_failed_build"] = {
            "reply_says_failed": "Build FAILED" in reply,
            "tool_result_error": job.events[-1]["error"],
        }

        for mode in ("edit", "ask"):
            for rel in ("main.tex", "other.tex", "figure.png", "script.py", "build/x.tex"):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original\n", encoding="utf-8")
            server.set_root(root)

            class SimulatedCLI(backends.Backend):
                enforces_scope = False
                read_only_ask = False

                def run(self, job):
                    for rel in ("main.tex", "other.tex", "figure.png", "script.py", "build/x.tex"):
                        (job.root / rel).write_text("changed\n", encoding="utf-8")
                    return {"exit": 0}

            m = manager(root, server.list_files, simulated=SimulatedCLI("simulated"))
            r = m.start("fixture", None, mode, scope=["main.tex"] if mode == "edit" else None)
            done = wait_done(m.jobs[r["job"]])
            results[f"manager_{mode}_rollback"] = {
                "changed": [c["path"] for c in done["changed"]],
                "reverted": done["reverted"],
                "unreported_writes_persist": [rel for rel in ("figure.png", "script.py", "build/x.tex")
                                              if (root / rel).read_text() == "changed\n"],
            }

        outside = base / "outside.tex"
        outside.write_text("AUDIT_OUTSIDE_MARKER\n", encoding="utf-8")
        try:
            (root / "linked.tex").symlink_to(outside)
        except OSError as exc:
            results["api_symlink_search"] = {"skipped": type(exc).__name__}
        else:
            server.set_root(root)
            job.root, job.files = root, server.list_files
            try:
                api.tool(job, "read_file", {"path": "linked.tex"}, [])
                direct_read_denied = False
            except ToolError:
                direct_read_denied = True
            search = api.tool(job, "search", {"pattern": "AUDIT_OUTSIDE_MARKER"}, [])
            results["api_symlink_search"] = {
                "direct_read_denied": direct_read_denied,
                "search_reads_outside_project": "AUDIT_OUTSIDE_MARKER" in search,
            }

        remote = base / "empty-remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        env = backends.agent_env()
        blocked = subprocess.run(["git", "ls-remote", str(remote)], env=env, capture_output=True)
        # Change only a copy of the child environment, never this process's environment.
        overridden = {**env, "GIT_ALLOW_PROTOCOL": "file"}
        allowed = subprocess.run(["git", "ls-remote", str(remote)], env=overridden, capture_output=True)
        results["git_environment_guard"] = {
            "unchanged_environment_blocks_read": blocked.returncode != 0,
            "child_can_override_guard": allowed.returncode == 0,
            "external_network_used": False,
        }

        js = (REPO / "prism_local/static/app.js").read_text(encoding="utf-8")
        start = js.index("function slashPrompt(")
        end = js.index("\n}", start) + 2
        script = ("const catalog={skills:[]};\n" + js[start:end]
                  + '\nprocess.stdout.write(slashPrompt("/review @main.tex:1-2", "REFERENCED_SELECTION"));')
        node = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        results["non_claude_slash_reference"] = {
            "selection_kept": "REFERENCED_SELECTION" in node.stdout,
        }
        if "--native-codex-sandbox" in sys.argv:
            native = base / "native-sandbox"
            native.mkdir()
            for name in ("main.tex", "other.tex", "figure.png"):
                (native / name).write_text("original\n", encoding="utf-8")
            # The command has broad read access but may write only main.tex. No
            # user configuration is edited and no model or external network is used.
            policy = ('permissions.prism-audit.filesystem={"/"="read",'
                      + json.dumps((native / "main.tex").as_posix()) + '="write"}')
            program = """import json
from pathlib import Path
results = {}
for name in ('main.tex', 'other.tex', 'figure.png'):
    try:
        Path(name).write_text('changed\\n')
        results[name] = 'allowed'
    except PermissionError:
        results[name] = 'denied'
print(json.dumps(results))
"""
            process = subprocess.run([Codex("codex").bin(), "sandbox", "-P", "prism-audit",
                                      "-C", str(native), "-c", policy,
                                      "--", sys.executable, "-B", "-c", program],
                                     capture_output=True, text=True, timeout=30)
            results["native_codex_path_permissions"] = {
                "exit": process.returncode, "output": process.stdout.strip(),
                "stderr": process.stderr[-1000:] if process.returncode else "",
            }
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
