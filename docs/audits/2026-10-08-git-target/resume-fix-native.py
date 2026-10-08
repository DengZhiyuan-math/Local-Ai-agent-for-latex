"""Native Deep Code collision acceptance. Uses a synthetic model on loopback only."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "prism_local"), str(REPO / "tests")]
from backend_deepcode import DeepCode, sessions_dir
from test_agent import FakeAPI, manager, wait_done


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", required=True)
    args = parser.parse_args()
    response = [{"choices": [{"index": 0, "delta": {"content": '{"skillNames":[]}'},
                              "finish_reason": "stop"}]}]
    model = FakeAPI([response for _ in range(30)])
    try:
        with tempfile.TemporaryDirectory(prefix="dc-", dir="/tmp" if Path("/tmp").is_dir() else None) as tmp:
            base = Path(tmp).resolve()
            a, b, home = base / "a-b/c", base / "a/b-c", base / "home"
            for root in (a, b):
                root.mkdir(parents=True)
                (root / "main.tex").write_text("fixture\n")
            settings = home / ".deepcode/settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({"env": {
                "API_KEY": "fixture", "BASE_URL": f"http://127.0.0.1:{model.server_port}"},
                "thinkingEnabled": False}))
            with mock.patch.dict(os.environ, {
                    "HOME": str(home), "USERPROFILE": str(home), "DEEPCODE_API_KEY": "fixture",
                    "DEEPCODE_TELEMETRY_ENABLED": "false", "DEEPCODE_DEBUG_LOG_ENABLED": "false"}):
                backend = DeepCode("deepcode", {"bin": args.bin})
                assert sessions_dir(a) == sessions_dir(b), "requires short native project paths"
                ma = manager(a, lambda: ["main.tex"], deepcode=backend)
                mb = manager(b, lambda: ["main.tex"], deepcode=backend)

                def turn(m, sid=None):
                    started = m.start("Answer this fixture without using tools.", sid, "ask")
                    assert "error" not in started, started
                    done = wait_done(m.jobs[started["job"]], timeout=60)
                    assert done["exit"] == 0 and done.get("session_id"), done
                    return done["session_id"]

                sid_a = turn(ma)
                assert turn(ma, sid_a) == sid_a
                requests = len(model.requests)
                assert "error" in mb.start("Resume the foreign fixture.", sid_a, "ask")
                assert len(model.requests) == requests, "foreign resume reached the model"
                sid_b = turn(mb)
                index = json.loads((sessions_dir(b) / "sessions-index.json").read_text())
                assert index["originalPath"] == str(b)
                assert any(e["id"] == sid_a for e in index["entries"])
                assert backend.check_session(b, sid_b) is None
                # Both ids are in B's index. The original per-session cwd must still reject A.
                requests = len(model.requests)
                assert "error" in mb.start("Resume the foreign fixture.", sid_a, "ask")
                assert len(model.requests) == requests, "foreign resume reached the model"
                assert backend.check_session(a, sid_b)
                assert turn(ma, sid_a) == sid_a
                index = json.loads((sessions_dir(a) / "sessions-index.json").read_text())
                assert index["originalPath"] == str(a)
                assert backend.check_session(b, sid_b) is None
                assert turn(mb, sid_b) == sid_b
                requests = len(model.requests)
                assert "error" in ma.start("Resume the foreign fixture.", sid_b, "ask")
                assert "error" in mb.start("Resume the foreign fixture.", sid_a, "ask")
                assert len(model.requests) == requests, "foreign resume reached the model"
                assert (a / "main.tex").read_text() == (b / "main.tex").read_text() == "fixture\n"
                print(json.dumps({"same_native_directory": True, "native_local_resume": True, "native_a_b_a_b_resume": True,
                                  "native_index_rewritten_to_b": True,
                                  "foreign_resume_blocked_before_model": True,
                                  "per_session_project_checked": True,
                                  "cli": args.bin, "model": "synthetic loopback"}, indent=2))
                print("DEEPCODE_COLLISION_ACCEPTANCE_PASSED")
    finally:
        model.shutdown()
        model.server_close()


if __name__ == "__main__":
    main()
