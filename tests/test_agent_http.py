"""Real HTTP -> manager -> CLI -> approval -> continuation; no external services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request


@unittest.skipIf(os.name == "nt", "CLI test double has a POSIX shebang")
class AgentHTTP(unittest.TestCase):
    def test_configuration_permission_usage_and_source_navigation(self):
        fixture = Path(__file__).parent / "browser" / "agent_improvements_fixture.py"
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            process = subprocess.Popen([sys.executable, str(fixture), directory],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 8
                while not (work / "ready.json").exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((work / "ready.json").exists())
                url = json.loads((work / "ready.json").read_text())["url"]
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                def request(path, body=None):
                    headers = {"Content-Type": "application/json", "X-Prism-Local": "1"}
                    req = urllib.request.Request(url.rstrip("/") + path,
                        data=json.dumps(body).encode() if body is not None else None, headers=headers)
                    try:
                        with opener.open(req, timeout=5) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        with error:
                            return error.code, json.load(error)
                info = request("/api/agent/info")[1]
                key = info["project_key"]
                models = request("/api/agent/models?provider=claude")[1]
                self.assertEqual(next(m["efforts"] for m in models["models"] if m["id"] == "deep"), ["low", "high"])
                self.assertEqual(request("/api/agent/config", {"project_key": key, "provider": "claude", "model": "deep", "effort": "high"})[0], 200)
                self.assertEqual(request("/api/agent/config", {"project_key": key, "provider": "claude", "model": "fast", "effort": "high"})[0], 400)
                job = request("/api/agent", {"project_key": key, "prompt": "Research this fixture", "mode": "edit", "provider": "claude", "model": "deep", "effort": "high"})[1]["job"]
                events, after = [], 0
                def read_events():
                    nonlocal after
                    status, result = request(f"/api/agent/events?project_key={key}&job={job}&after={after}&wait=0")
                    self.assertEqual(status, 200)
                    events.extend(result["events"])
                    after += len(result["events"])
                    return result["done"]
                deadline = time.monotonic() + 5
                while not any(e["t"] == "permission" for e in events) and time.monotonic() < deadline:
                    read_events(); time.sleep(.02)
                permission = next(e for e in events if e["t"] == "permission")
                reply = {"project_key": key, "job": job, "request_id": permission["request_id"], "decision": "allow", "scope": "session"}
                self.assertEqual(request("/api/agent/permission", {**reply, "project_key": "foreign"})[0], 409)
                self.assertEqual(request("/api/agent/permission", {**reply, "request_id": "missing"})[0], 409)
                self.assertEqual(request("/api/agent/permission", reply)[0], 200)
                deadline = time.monotonic() + 5
                while not read_events() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(any(e.get("text", "").startswith("Continued: allow") for e in events))
                self.assertTrue(any(e["t"] == "rate" and e.get("last_success") for e in events))
                self.assertEqual(request("/api/agent/permission", reply)[0], 409)
                self.assertEqual(request("/api/agent/info")[1]["defaults"]["providers"]["claude"], {"model": "deep", "effort": "high"})
                index = request("/api/labels")[1]
                source = work / "project/sections/intro.tex"
                source.write_text("% new line\n" + source.read_text())
                resolved = request("/api/labels/resolve?" + urllib.parse.urlencode({"label": "eq:one", "version": index["version"]}))[1]
                self.assertEqual((resolved["status"], resolved["file"], resolved["line"], resolved["stale"]), ("ok", "sections/intro.tex", 3, True))
                first = request("/api/agent/usage", {"project_key": key, "provider": "claude", "force": True})[1]
                second = request("/api/agent/usage", {"project_key": key, "provider": "claude", "force": True})[1]
                self.assertGreater(second["last_success"], first["last_success"])
                (work / "usage-fail").touch()
                failed = request("/api/agent/usage", {"project_key": key, "provider": "claude", "force": True})[1]
                self.assertEqual(failed["last_success"], second["last_success"])
                self.assertTrue(failed["stale"])
                self.assertIn("offline", failed["error"])
            finally:
                process.terminate()
                process.wait(timeout=5)
