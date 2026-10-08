"""A stand-in for Deep Code's `deepcode --exec` (tests/test_agent.py).

Like the real one it takes the message on stdin, keeps its session in
~/.deepcode/projects/<project code>/ (an index and one .jsonl per session), and prints
only the final reply. It also notes how it was called in call.json in the project, and
edits main.tex when the message says EDIT.
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from backend_deepcode import sessions_dir  # noqa: E402

args = sys.argv[1:]
message = sys.stdin.read()
root = Path.cwd()
resume = args[args.index("--resume") + 1] if "--resume" in args else None
sid = resume or str(uuid.uuid4())
(Path.home() / "call.json").write_text(json.dumps({
    "args": args, "message": message, "key": os.environ.get("DEEPCODE_API_KEY"),
    "model": os.environ.get("DEEPCODE_MODEL"), "effort": os.environ.get("DEEPCODE_REASONING_EFFORT")}),
    encoding="utf-8")
if "EDIT" in message:
    (root / "main.tex").write_text("edited by deepcode\n", encoding="utf-8")

d = sessions_dir(root)
d.mkdir(parents=True, exist_ok=True)
with open(d / f"{sid}.jsonl", "a", encoding="utf-8") as f:
    if not resume:
        f.write(json.dumps({"id": str(uuid.uuid4()), "sessionId": sid, "role": "system",
                            "content": '# Local Workspace Environment\n\n```json\n' +
                            json.dumps({"root path": str(root), "pwd": str(root)}) + '\n```'}) + "\n")
    for m in ({"role": "user", "content": message},
              {"role": "assistant", "content": "", "tool_calls": [
                  {"id": "call_1", "type": "function",
                   "function": {"name": "write_file", "arguments": json.dumps({"path": "main.tex"})}}]},
              {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
              {"role": "assistant", "content": "Changed main.tex."}):
        f.write(json.dumps(m) + "\n")
index_path = d / "sessions-index.json"
index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"entries": []}
index["originalPath"] = str(root)
index["entries"] = [e for e in index["entries"] if e["id"] != sid] + [{"id": sid, "updateTime": int(time.time() * 1000)}]
index_path.write_text(json.dumps(index), encoding="utf-8")
print("Changed main.tex.")
