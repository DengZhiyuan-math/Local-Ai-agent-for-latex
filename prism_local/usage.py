"""Read-only Claude control queries and account-scoped subscription usage snapshots.

No user message is sent. Unsupported CLI versions return an error, never a fallback
model request. The last successful snapshot is retained only for the same identity.
"""
from __future__ import annotations

import json
import math
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from backends import Job, agent_env, cli_process, kill_tree

USAGE_TTL = 60
CONTROL_TIMEOUT = 15


def control_query(exe: str | None, request: dict | None = None, timeout: float = CONTROL_TIMEOUT,
                  root: Path | None = None) -> dict:
    """Initialize a headless CLI and optionally ask a read-only control question.

    Returns the control response's payload; raises ValueError on unsupported, malformed,
    failed or timed-out queries. `request=None` returns initialization metadata.
    """
    if not exe:
        raise ValueError("Claude Code CLI not found")
    cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
           "--no-session-persistence"]
    job = Job(0)
    job.root = root or Path(tempfile.gettempdir())
    initialize = {"type": "control_request", "request_id": "prism-init",
                  "request": {"subtype": "initialize"}}
    expired = threading.Event()

    def expire():
        expired.set()
        job.cancel.set()
        if job.proc is not None:
            kill_tree(job.proc)

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        with cli_process(job, cmd, json.dumps(initialize) + "\n", agent_env(root=job.root), keep_stdin_open=True) as (proc, lines, errors):
            if proc is None:
                raise ValueError("Claude Code usage query timed out")
            expected = "prism-init"
            for line in lines:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict) or data.get("type") != "control_response":
                    continue
                response = data.get("response")
                if not isinstance(response, dict) or response.get("request_id") != expected:
                    continue
                if response.get("subtype") != "success":
                    raise ValueError(str(response.get("error") or "Claude Code control query failed")[:300])
                payload = response.get("response")
                if not isinstance(payload, dict):
                    raise ValueError("Claude Code returned an invalid control response")
                if expected == "prism-init" and request is not None:
                    expected = "prism-query"
                    proc.stdin.write(json.dumps({"type": "control_request", "request_id": expected,
                                                  "request": request}) + "\n")
                    proc.stdin.flush()
                else:
                    return payload
            if expired.is_set():
                raise ValueError("Claude Code usage query timed out")
            detail = "".join(errors).strip()[-300:]
            raise ValueError("Claude Code did not answer the read-only control query" + (": " + detail if detail else ""))
    except OSError as e:
        raise ValueError(f"Could not query Claude Code: {e}") from e
    finally:
        timer.cancel()


def normalize_usage(payload: dict, at: float) -> dict:
    """Convert native /usage percentages and ISO dates to the existing rate event form."""
    limits = payload.get("rate_limits")
    if payload.get("rate_limits_available") is not True or not isinstance(limits, dict):
        raise ValueError("Claude Code does not expose subscription usage for this login")
    windows = {}
    for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
        window = limits.get(key)
        if not isinstance(window, dict) or window.get("utilization") is None:
            continue
        utilization = window["utilization"]
        if isinstance(utilization, bool) or not isinstance(utilization, (int, float)) \
                or not math.isfinite(utilization) or not 0 <= utilization <= 100:
            raise ValueError("Claude Code returned invalid usage percentages")
        reset = window.get("resets_at")
        try:
            stamp = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp() if reset else None
        except (ValueError, AttributeError, TypeError) as e:
            raise ValueError("Claude Code returned an invalid usage reset time") from e
        windows[key] = {"utilization": utilization / 100, "resetsAt": stamp}
    if not windows:
        raise ValueError("Claude Code returned no subscription usage windows")
    return {"unifiedWindows": windows, "at": at, "source": "claude-control-get_usage"}


def expired(rate: dict | None, now: float) -> bool:
    if not rate or not rate.get("at") or now - rate["at"] >= USAGE_TTL:
        return True
    return any(w.get("resetsAt") and w["resetsAt"] <= now
               for w in rate.get("unifiedWindows", {}).values())


class UsageCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.identity = None
        self.generation = None
        self.rate: dict | None = None
        self.error: str | None = None

    def get(self, exe: str | None, identity: tuple, force: bool = True) -> dict:
        # Forced refreshes queue, then each starts a new process: no silent busy result.
        with self.lock:
            if identity != self.identity:
                self.identity, self.rate, self.error = identity, None, None
                self.generation = uuid.uuid4().hex
            if not force and not self.error and not expired(self.rate, time.time()):
                return self._result(cached=True)
            try:
                payload = control_query(exe, {"subtype": "get_usage", "skip_behaviors": True})
                rate = normalize_usage(payload, time.time())
            except (ValueError, OSError) as e:
                self.error = str(e)
            else:
                self.rate, self.error = rate, None
            return self._result(cached=False)

    def _result(self, cached: bool) -> dict:
        result = {"rate": self.rate, "last_success": self.rate.get("at") if self.rate else None,
                  "stale": bool(self.error) or expired(self.rate, time.time()), "cached": cached,
                  "identity": self.generation}
        if self.error:
            result["error"] = self.error
        return result
