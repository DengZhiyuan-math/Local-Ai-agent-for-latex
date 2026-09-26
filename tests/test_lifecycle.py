"""Integration tests: the real server exits after its last page, and the launcher
reuses a running server. Uses short timings via the hidden --idle-timings option."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "prism_local" / "server.py"
LAUNCHER = REPO / "launcher" / "prism_launcher.pyw"
PROJECT = REPO / "examples" / "minimal"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def request(url, path, data=None, headers=None):
    req = urllib.request.Request(url.rstrip("/") + path, data=data, headers=headers or {})
    try:
        with HTTP.open(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


def beat(url, cid):
    return request(url, "/api/presence", json.dumps({"client": cid}).encode(),
                   {"Content-Type": "application/json", "X-Prism-Local": "1"})


def bye(url, cid, origin=None):
    host = url.split("//")[1].rstrip("/")
    return request(url, "/api/bye", cid.encode(),
                   {"Content-Type": "text/plain;charset=UTF-8",
                    "Origin": origin or f"http://{host}"})


def open_stream(url, cid, origin=None):
    """Hold the page presence stream open like a browser's EventSource."""
    host, port = url.split("//")[1].rstrip("/").split(":")
    sock = socket.create_connection((host, int(port)), timeout=5)
    extra = f"Origin: {origin}\r\n" if origin else ""
    sock.sendall(f"GET /api/presence/stream?client={cid} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                 f"Accept: text/event-stream\r\n{extra}\r\n".encode())
    head = sock.recv(200).decode("latin-1")
    return sock, int(head.split()[1])


def stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(10)


def wait_file(path, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.1)
    raise AssertionError(f"{path} did not appear")


class ServerLifecycle(unittest.TestCase):
    def start(self, timings, *extra):
        self.tmp = Path(tempfile.mkdtemp())
        self.ready = self.tmp / "ready.json"
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER), str(PROJECT), "--port", "0", "--no-browser",
             "--exit-when-idle", "--idle-timings", timings, "--ready-file", str(self.ready),
             *extra], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
            env={**os.environ, "PRISM_STATE_DIR": str(self.tmp / "state")})
        self.addCleanup(self.proc.stdout.close)
        self.addCleanup(stop, self.proc)
        return wait_file(self.ready)["url"]

    def assertExitsWithin(self, lo, hi, t0):
        self.proc.wait(hi + 5)
        took = time.monotonic() - t0
        self.assertEqual(self.proc.returncode, 0)
        self.assertGreaterEqual(took, lo)
        self.assertLessEqual(took, hi)
        self.assertFalse(self.ready.exists(), "ready file should be removed on exit")

    def test_exits_when_no_page_ever_connects(self):
        self.start("2,1,5")
        t0 = time.monotonic()
        self.assertExitsWithin(0.5, 4, t0)

    def test_exits_after_last_goodbye(self):
        url = self.start("30,1.5,30")
        self.assertEqual(beat(url, "editor-page-1")[0], 200)
        self.assertEqual(beat(url, "pdf-page-0001")[0], 200)
        self.assertEqual(request(url, "/api/ping")[1]["pages"], 2)
        self.assertEqual(bye(url, "editor-page-1"), (200, {"ok": True}))
        time.sleep(3)               # one page is still open
        self.assertIsNone(self.proc.poll())
        self.assertEqual(bye(url, "pdf-page-0001"), (200, {"ok": True}))
        t0 = time.monotonic()
        self.assertExitsWithin(1, 4, t0)

    def test_reload_does_not_exit(self):
        url = self.start("30,2,30")
        beat(url, "page-before-reload")
        bye(url, "page-before-reload")
        time.sleep(0.5)
        beat(url, "page-after-reload")
        time.sleep(3)
        self.assertIsNone(self.proc.poll())

    def test_silent_page_goes_stale(self):
        url = self.start("30,1,2")
        beat(url, "page-that-crashes")
        t0 = time.monotonic()
        self.assertExitsWithin(2, 6, t0)

    def test_goodbye_is_guarded(self):
        url = self.start("30,1,30")
        beat(url, "real-page-0001")
        self.assertEqual(bye(url, "real-page-0001", origin="https://evil.example")[0], 403)
        self.assertEqual(bye(url, "guessed-id-000"), (200, {"ok": False}))
        status, _ = request(url, "/api/presence", b'{"client": "no-header-page"}',
                            {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertEqual(request(url, "/api/ping")[1]["pages"], 1)

    def test_closing_the_stream_exits_without_goodbye(self):
        url = self.start("30,1.5,120")
        sock, status = open_stream(url, "stream-page-01")
        self.assertEqual(status, 200)
        time.sleep(1)
        self.assertEqual(request(url, "/api/ping")[1]["pages"], 1)
        time.sleep(3)                   # no heartbeats, still open
        self.assertIsNone(self.proc.poll())
        sock.close()                    # the browser closed: no goodbye is sent
        t0 = time.monotonic()
        self.assertExitsWithin(1, 5, t0)

    def test_stream_from_other_sites_is_refused(self):
        url = self.start("30,1,30")
        sock, status = open_stream(url, "evil-page-001", origin="https://evil.example")
        sock.close()
        self.assertEqual(status, 403)
        self.assertEqual(request(url, "/api/ping")[1]["pages"], 0)

    def test_other_sites_cannot_use_the_api(self):
        url = self.start("30,1,30")
        cross = {"Sec-Fetch-Site": "cross-site"}
        # an <img> or <script> on another website: no data, no programs started
        self.assertEqual(request(url, "/api/tree", headers=cross)[0], 403)
        self.assertEqual(request(url, "/api/agent/commands?refresh=1", headers=cross)[0], 403)
        self.assertEqual(request(url, "/api/tree", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(request(url, "/api/tree", headers={"Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(request(url, "/api/ping")[0], 200)          # the launcher, no browser
        # the editor page itself may be opened from anywhere (a link, a bookmark), unframed
        with HTTP.open(urllib.request.Request(url, headers=cross), timeout=5) as r:
            self.assertEqual((r.status, r.headers["X-Frame-Options"]), (200, "DENY"))

    def test_port_in_use_moves_up(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        self.addCleanup(blocker.close)
        port = blocker.getsockname()[1]
        self.start("30,1,30", "--port", str(port), "--port-tries", "5")
        info = wait_file(self.ready)
        self.assertNotEqual(info["port"], port)
        self.assertTrue(port < info["port"] <= port + 4)


class LauncherLifecycle(unittest.TestCase):
    def test_launcher_starts_server_and_exits(self):
        state = Path(tempfile.mkdtemp())
        env = {**os.environ, "PRISM_STATE_DIR": str(state)}
        cmd = [sys.executable, str(LAUNCHER), str(PROJECT), "--browser", "none", "--quiet",
               "--idle-timings", "30,1,30"]
        # The launcher starts the server and exits; it does not stay behind.
        first = subprocess.run(cmd, env=env, timeout=40, creationflags=NO_WINDOW)
        self.assertEqual(first.returncode, 0)
        insts = list((state / "instances").glob("*.json"))
        self.assertEqual(len(insts), 1)
        info = wait_file(insts[0])
        url = info["url"]
        self.addCleanup(lambda: subprocess.run(
            ["taskkill", "/F", "/PID", str(info["pid"])] if os.name == "nt" else
            ["kill", str(info["pid"])], capture_output=True))
        self.assertEqual(beat(url, "launched-page-1")[0], 200)

        # A second click reuses the running server.
        second = subprocess.run(cmd, env=env, timeout=20, creationflags=NO_WINDOW)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(request(url, "/api/ping")[1]["pid"], info["pid"])
        self.assertEqual(len(list((state / "instances").glob("*.json"))), 1)

        # Closing the last page stops the server, which removes its instance file.
        bye(url, "launched-page-1")
        deadline = time.monotonic() + 15
        while insts[0].exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertFalse(insts[0].exists())
        with self.assertRaises(OSError):
            HTTP.open(url + "api/ping", timeout=2)
        self.assertTrue(list((state / "logs").glob("*.log")))


if __name__ == "__main__":
    unittest.main()
