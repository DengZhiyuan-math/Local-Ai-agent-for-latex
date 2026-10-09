"""The agent's compile tool: a minimal MCP server (stdio) for CLI backends.

    python mcp_compile.py --url http://127.0.0.1:8765/

CLI backends start it for Edit turns from the agent panel and offer
its one tool, `compile`. The tool asks the editor server to build the project, exactly
as the Compile button does (same engine, settings and build folder; the PDF in the
editor reloads), and answers with the errors and warnings and their file:line. So the
agent can compile and fix what fails without any shell.

The protocol is JSON-RPC 2.0, one message per line on stdin/stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
import urllib.error
import urllib.request

TOOL = {
    "name": "compile",
    "description": (
        "Compile the LaTeX project with the editor's own build (the author's Compile "
        "button: same engine, bibliography and settings). It builds the document you "
        "edited since its PDF was made, a standalone .tex with its own \\documentclass as "
        "well as the project's main file; else the document shown. The PDF in the editor "
        "reloads. Returns which document it built, whether it built, and the errors and "
        "warnings with file:line. Use it to check your changes; do not run pdflatex or "
        "latexmk yourself."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "clean": {"type": "boolean", "description": (
                "Delete what earlier builds left (.aux, .bbl, ...) first. Only when a "
                "build keeps failing for no visible reason.")},
        },
        "additionalProperties": False,
    },
}
MAX_DIAGNOSTICS = 60
WAIT_BUSY = 300          # seconds to wait for a build that is already running


def config(url: str) -> dict:
    return {"command": sys.executable,
            "args": [str(Path(__file__).resolve()), "--url", url]}


def build(url: str, clean: bool) -> dict:
    body = json.dumps({"mode": "draft", "clean": clean, "by": "agent"}).encode()
    deadline = time.time() + WAIT_BUSY
    while True:
        req = urllib.request.Request(url.rstrip("/") + "/api/build", data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-Prism-Local": "1"})
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 409 and time.time() < deadline:    # another build is running
                time.sleep(1)
                continue
            raise


def report(r: dict) -> tuple[str, bool]:
    """The build result as text for the model, and whether it failed."""
    if r.get("busy"):
        return "Another build is still running; try again in a moment.", True
    diags = r.get("diagnostics") or []
    errors = [d for d in diags if d.get("severity") == "error"]
    ok = r.get("exit") == 0 and not errors and not r.get("cancelled") and not r.get("timed_out")
    if r.get("cancelled"):
        head = "The build was stopped by the author."
    elif r.get("timed_out"):
        head = "The build took too long and was stopped."
    elif ok:
        head = "Build OK."
    elif r.get("pdf_updated"):
        head = "The PDF was built, but with errors."
    else:
        head = f"Build FAILED (exit {r.get('exit')})."
    how = " · ".join(str(x) for x in (r.get("engine"), r.get("seconds") and f"{r['seconds']}s") if x)
    lines = [head + (f" ({how})" if how else "")]
    if r.get("main"):
        lines.append(f"Document: {r['main']}.")
    lines += [
             f"{len(errors)} error(s), {len(diags) - len(errors)} warning(s)."]
    for d in diags[:MAX_DIAGNOSTICS]:
        where = f"{d.get('file')}:{d.get('line')}" if d.get("line") else (d.get("file") or "?")
        lines.append(f"- {d.get('severity')}: {where}: {d.get('message')}")
    if len(diags) > MAX_DIAGNOSTICS:
        lines.append(f"... and {len(diags) - MAX_DIAGNOSTICS} more.")
    if not ok and not diags:
        lines += ["", "End of the build output:", (r.get("output") or "")[-3000:]]
    return "\n".join(lines), not ok


def answer(msg: dict, url: str) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:                      # a notification (initialized, cancelled): no answer
        return None
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "prism", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != "compile":
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        try:
            text, failed = report(build(url, bool((params.get("arguments") or {}).get("clean"))))
        except (OSError, ValueError) as e:
            text, failed = f"Could not reach the editor to compile: {e}", True
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="the editor server, http://127.0.0.1:PORT/")
    url = ap.parse_args().url
    # The pipes themselves (fds 0 and 1): under pythonw.exe (an editor started from the
    # desktop shortcut) sys.stdin and sys.stdout can be None.
    stdin = open(0, "r", encoding="utf-8", errors="replace", closefd=False)
    out = open(1, "w", encoding="utf-8", newline="\n", closefd=False)
    for line in stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = answer(msg, url) if isinstance(msg, dict) else None
        if reply is not None:
            out.write(json.dumps(reply) + "\n")
            out.flush()


if __name__ == "__main__":
    main()
