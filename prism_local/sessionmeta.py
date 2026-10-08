"""Read native CLI session ownership without loading conversation text."""
import json
import re
from pathlib import Path
import registry


def valid_id(session_id: str) -> bool:
    return isinstance(session_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id))


def matches(path: Path, root: Path, session_id: str) -> bool:
    try:
        with path.open(encoding="utf-8") as stream:
            for _ in range(30):
                line = stream.readline(1024 * 1024)
                if not line:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                data = record.get("payload", {}) if record.get("type") == "session_meta" else record
                if not isinstance(data, dict):
                    continue
                sid, cwd = data.get("id", data.get("sessionId")), data.get("cwd")
                # Deep Code 0.4 stores the original project in a native system message.
                # The shared index's originalPath is rewritten by each project save.
                if record.get("role") == "system" and record.get("sessionId") == session_id:
                    content = record.get("content")
                    if not isinstance(content, str):
                        continue
                    _, marker, context = content.partition("# Local Workspace Environment\n\n```json\n")
                    if not marker:
                        continue
                    workspace = json.loads(context.removesuffix("\n```"))
                    if not isinstance(workspace, dict) or workspace.get("root path") != workspace.get("pwd"):
                        return False
                    sid, cwd = session_id, workspace.get("root path")
                if sid == session_id and isinstance(cwd, str) and Path(cwd).is_absolute():
                    return registry.norm(Path(cwd).resolve()) == registry.norm(root.resolve())
    except (OSError, ValueError, TypeError):
        pass
    return False
