"""The agent's editor tools: a minimal MCP server (stdio) for CLI backends.

    python mcp_compile.py --url http://127.0.0.1:8765/ --mode edit

CLI backends start it for every turn from the agent panel (when the editor's server is
known). The agent has no shell, so these tools are how it reaches what the editor does:

- `compile` (Edit turns): the editor builds the project, exactly as the Compile button
  does (same engine, settings and build folder; the PDF in the editor reloads), and
  answers with the errors and warnings and their file:line.
- the project's history (versions.py): `versions` lists pinned versions and commits,
  `version_show` shows one (what it changed, its files, or one file as it was),
  `version_diff` compares it with another or with the files now; in Edit turns
  `version_restore` puts files back as they were in one (as the turn's own changes: the
  author's Undo takes them back), and `version_pin` pins the project under a name (v1)
  once the turn's changes are committed.

The protocol is JSON-RPC 2.0, one message per line on stdin/stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
import urllib.error
import urllib.parse
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
REV = {"type": "string", "description": (
    "A version: a pinned name (v1), a commit hash from `versions`, or HEAD / HEAD~N "
    "(N commits before the latest).")}
HISTORY_TOOLS = [
    {"name": "versions",
     "description": (
         "The project's history (its git repository): the versions the author pinned (v1, "
         "submitted, ...) and the recent commits, newest first, with hash, date, author and "
         "message. Autosave and every agent turn make commits, so earlier states of the "
         "files are here. With `path`, only the commits that changed that file. Use this "
         "and the other version tools for anything about earlier versions, not git."),
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "A project file: only its commits."},
         "limit": {"type": "integer", "description": "How many commits (default 40, at most 500)."}},
         "additionalProperties": False}},
    {"name": "version_show",
     "description": (
         "One version of the project. Without `path`: its commit message, the files it "
         "changed, and every file the project had then. With `path`: that file's full text "
         "as it was in that version (to read an old version, or to recover deleted text)."),
     "inputSchema": {"type": "object", "properties": {
         "rev": REV, "path": {"type": "string", "description": "A project file, as it was then."}},
         "required": ["rev"], "additionalProperties": False}},
    {"name": "version_diff",
     "description": (
         "What differs between version `rev` and version `to`, or, without `to`, between "
         "`rev` and the project's files now. Optionally for one `path` only. A unified diff."),
     "inputSchema": {"type": "object", "properties": {
         "rev": REV, "to": {**REV, "description": "The other version (default: the files now)."},
         "path": {"type": "string", "description": "A project file or folder."}},
         "required": ["rev"], "additionalProperties": False}},
    {"name": "version_restore",
     "description": (
         "Put files back as they were in version `rev`: each is written as it was then, and "
         "files committed since that the version did not have are deleted. Without `paths`, "
         "the whole project; with `paths`, only those files or folders. These are your "
         "changes in this turn (the author sees the diff and can undo it); nothing in the "
         "history is lost. Only when the author asks to go back to a version."),
     "inputSchema": {"type": "object", "properties": {
         "rev": REV, "paths": {"type": "array", "items": {"type": "string"},
                               "description": "Project files or folders (default: the whole project)."}},
         "required": ["rev"], "additionalProperties": False}},
    {"name": "version_pin",
     "description": (
         "Pin the project as a named version (v1, submitted, ...) to return to later. It is "
         "pinned when this turn ends, after your changes are committed, so it includes them. "
         "Only when the author asks for it."),
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "1-50 letters, digits, '.', '-', '_' (v1, draft-2)."},
         "note": {"type": "string", "description": "What this version is (one line)."}},
         "required": ["name"], "additionalProperties": False}},
]
for _t in HISTORY_TOOLS:      # looking only: an Ask turn (plan mode) may use them without asking
    if _t["name"] in ("versions", "version_show", "version_diff"):
        _t["annotations"] = {"readOnlyHint": True}
TOOLS = [TOOL, *HISTORY_TOOLS]
EDIT_ONLY = {"compile", "version_restore", "version_pin"}
# How the agent panel names each tool's steps.
LABELS = {"compile": "Compile", "versions": "History", "version_show": "Show version",
          "version_diff": "Compare versions", "version_restore": "Restore version",
          "version_pin": "Pin version"}
MAX_DIAGNOSTICS = 60
WAIT_BUSY = 300          # seconds to wait for a build that is already running


def tools_for(mode: str) -> list[dict]:
    """The tools a turn in `mode` (edit, ask) has: an Ask turn only looks."""
    return [t for t in TOOLS if mode == "edit" or t["name"] not in EDIT_ONLY]


def names_for(mode: str) -> list[str]:
    return [t["name"] for t in tools_for(mode)]


def config(url: str, mode: str = "edit") -> dict:
    return {"command": sys.executable,
            "args": [str(Path(__file__).resolve()), "--url", url, "--mode", mode]}


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


# ---------------------------------------------------------------- the history tools
def request(url: str, route: str, params: dict | None = None, body: dict | None = None) -> dict:
    """The editor server's answer (JSON); its error message as a ValueError."""
    q = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    full = url.rstrip("/") + route + ("?" + urllib.parse.urlencode(q) if q else "")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(full, data=data, method="GET" if data is None else "POST",
                                 headers={"Content-Type": "application/json", "X-Prism-Local": "1"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            said = json.loads(e.read().decode("utf-8", "replace")).get("error")
        except ValueError:
            said = None
        raise ValueError(said or f"HTTP {e.code}") from None


def _when(at: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if at else "?"


def history(url: str, name: str, args: dict) -> str:
    """One history tool's answer, as text for the model. ValueError: what went wrong."""
    if name == "versions":
        r = request(url, "/api/history", {"path": args.get("path"), "limit": args.get("limit")})
        lines = ["Pinned versions:" if r["pins"] else "Pinned versions: none yet."]
        lines += [f"- {p['name']}: {p['hash'][:10]} ({_when(p['at'])})" + (f" - {p['note']}" if p["note"] else "")
                  for p in r["pins"]]
        what = f"Commits that changed {args['path']}" if args.get("path") else "Commits"
        lines += ["", f"{what}, newest first:" if r["commits"] else f"{what}: none."]
        names = {g["hash"]: "/".join(g["names"]) for g in r.get("groups", [])}
        for c in r["commits"]:
            pins = f" [pinned: {', '.join(c['pins'])}]" if c.get("pins") else ""
            part = f" (part of {names[c['version']]})" if c.get("version") in names else \
                (" (since the last pin)" if names else "")
            lines.append(f"- {c['hash'][:10]} {_when(c['at'])} {c['author']}: {c['message']}{pins}{part}")
        if not r.get("sync"):
            lines += ["", "(Automatic commits are off for this project: only the commits made "
                          "while they were on, or by hand, are here.)"]
        return "\n".join(lines)
    if name == "version_show":
        r = request(url, "/api/history/show", {"rev": args.get("rev"), "path": args.get("path")})
        if args.get("path"):
            if r.get("binary"):
                return f"{r['path']} in {args['rev']} ({r['hash'][:10]}): a binary file of {r['size']} bytes."
            tail = "\n[... cut: the file is longer]" if r.get("truncated") else ""
            return f"{r['path']} as it was in {args['rev']} ({r['hash'][:10]}):\n\n{r['content']}{tail}"
        lines = [f"Version {args['rev']}: commit {r['hash']}, {_when(r['at'])}, {r['author']}"
                 + (f" [pinned: {', '.join(r['pins'])}]" if r["pins"] else ""), "", r["message"], "",
                 "Changed in this commit:" if r["changed"] else "Changed in this commit: nothing in this project."]
        lines += [f"  {c['status']} " + (f"{c['from']} -> " if c.get("from") else "") + c["path"]
                  for c in r["changed"]]
        lines += ["", f"The project's files in this version ({r['file_count']}):"]
        lines += [f"  {f}" for f in r["files"]]
        if r["file_count"] > len(r["files"]):
            lines.append(f"  ... and {r['file_count'] - len(r['files'])} more")
        return "\n".join(lines)
    if name == "version_diff":
        r = request(url, "/api/history/diff", {"rev": args.get("rev"), "to": args.get("to"),
                                               "path": args.get("path")})
        to = f"{args['to']} ({r['to'][:10]})" if r.get("to") else "the files now"
        if not r["diff"]:
            return f"No difference between {args['rev']} ({r['from'][:10]}) and {to}."
        tail = "\n[... cut: the diff is longer; narrow it with `path`]" if r.get("truncated") else ""
        return f"From {args['rev']} ({r['from'][:10]}) to {to}:\n{r['stat']}\n{r['diff']}{tail}"
    if name == "version_restore":
        r = request(url, "/api/history/restore",
                    body={"rev": args.get("rev"), "paths": args.get("paths"), "by": "agent"})
        lines = [f"Restored from {args['rev']} ({r['hash'][:10]}): {len(r['written'])} file(s) written, "
                 f"{len(r['deleted'])} deleted, {r['unchanged']} already the same."]
        lines += [f"  wrote {p}" for p in r["written"]] + [f"  deleted {p}" for p in r["deleted"]]
        lines += [f"  skipped {s['path']}: {s['why']}" for s in r["skipped"]]
        return "\n".join(lines)
    if name == "version_pin":
        r = request(url, "/api/history/pin", body={"name": args.get("name"), "note": args.get("note"), "by": "agent"})
        return (f"{r['queued']} will be pinned when this turn ends, after its changes are committed. "
                "The author can return to it from the History panel, or ask you to.")
    raise ValueError(f"unknown tool {name}")


def call(url: str, name: str, args: dict) -> tuple[str, bool]:
    """Any of the tools: its answer as text, and whether it failed."""
    if name == "compile":
        try:
            return report(build(url, bool(args.get("clean"))))
        except (OSError, ValueError) as e:
            return f"Could not reach the editor to compile: {e}", True
    try:
        return history(url, name, args), False
    except (OSError, ValueError, KeyError, TypeError) as e:
        return str(e) or type(e).__name__, True


def answer(msg: dict, url: str, mode: str = "edit") -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:                      # a notification (initialized, cancelled): no answer
        return None
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "prism", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools_for(mode)}
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") not in names_for(mode):
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        text, failed = call(url, params["name"], args)
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
    ap.add_argument("--mode", default="edit", choices=("edit", "ask"))
    parsed = ap.parse_args()
    url, mode = parsed.url, parsed.mode
    # The pipes themselves (fds 0 and 1): under pythonw.exe (an editor started from the
    # desktop shortcut) sys.stdin and sys.stdout can be None.
    stdin = open(0, "r", encoding="utf-8", errors="replace", closefd=False)
    out = open(1, "w", encoding="utf-8", newline="\n", closefd=False)
    for line in stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = answer(msg, url, mode) if isinstance(msg, dict) else None
        if reply is not None:
            out.write(json.dumps(reply) + "\n")
            out.flush()


if __name__ == "__main__":
    main()
