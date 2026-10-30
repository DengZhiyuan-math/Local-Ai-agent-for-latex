"""What both servers' requests share (httpbase.py): an error is always an answer."""
import json
import os
import socket
import subprocess
import sys
import threading
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException, HTTPResponse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import errors  # noqa: E402
import httpbase  # noqa: E402
import registry  # noqa: E402
from presence import Presence  # noqa: E402


class Failing(httpbase.Handler):
    presence = Presence()

    def get(self, path, q):
        if path == "/bug":
            raise RuntimeError("something broke")
        if path == "/slow":
            raise subprocess.TimeoutExpired(["git", "status"], 10)
        if path == "/push":
            raise subprocess.TimeoutExpired(["git", "-c", "x=y", "push", "origin", "main"], 60)
        if path == "/half":
            self._send(200, b"{}")
            raise RuntimeError("after the answer")
        if path == "/gone":
            raise errors.NotFound("unknown project")
        if path == "/param":
            return self._json({"path": q["path"]})          # missing: a Refused
        if path == "/keyerror":
            return self._json({}[path])                     # a bug's KeyError
        if path == "/value":
            raise ValueError("not a name")
        if path == "/no-gh":
            raise errors.NotInstalled("gh")
        if path == "/no-file":
            raise FileNotFoundError(2, "No such file or directory")
        if path == "/locked":
            with registry.FileLock(Path(q["lock"]), timeout=0.2):
                return self._json({"ok": True})
        return self._json({"ok": True})

    def post(self, path, body):
        return self._json({"job": int(body["job"])})       # TypeError for None


class Binding(unittest.TestCase):
    def test_loopback_listener_serves_without_reverse_dns(self):
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("DNS is unavailable")) as fqdn:
            srv = httpbase.Server(("127.0.0.1", 0), Failing)
            self.addCleanup(srv.server_close)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.shutdown)
            self.assertEqual((srv.server_name, srv.server_port), srv.server_address)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{srv.server_port}/ok", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {"ok": True})
            fqdn.assert_not_called()


class Answers(unittest.TestCase):
    def setUp(self):
        self.srv = httpbase.Server(("127.0.0.1", 0), Failing)
        self.srv.handle_error = lambda *a: None                # "/half" re-raises; keep the log quiet
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def ask(self, path, body=None):
        req = urllib.request.Request(self.url + path, None if body is None else json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "X-Prism-Local": "1"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_an_unexpected_error_is_a_json_answer(self):
        self.assertEqual(self.ask("/bug"), (500, {"error": "RuntimeError: something broke"}))
        self.assertEqual(self.ask("/api/x", {"job": None})[0], 500)
        self.assertEqual(self.ask("/ok"), (200, {"ok": True}), "the server goes on")

    def test_a_command_that_timed_out_says_so(self):
        status, r = self.ask("/slow")
        self.assertEqual(status, 504)
        self.assertIn("git did not finish within 10 seconds; try again", r["error"])
        status, r = self.ask("/push")             # a Push or Pull from the menu: as the sync says it
        self.assertEqual(status, 504)
        self.assertIn("GitHub did not answer within 60 seconds", r["error"])
        self.assertNotIn("origin", r["error"])

    def test_a_lock_another_window_holds_is_a_409_that_says_so(self):
        """Not a dropped connection, which the page reads as "server not reachable"."""
        lock = Path(tempfile.mkdtemp()) / "projects.lock"
        with registry.FileLock(lock, timeout=1):
            status, r = self.ask("/locked?lock=" + urllib.parse.quote(str(lock)))
        self.assertEqual(status, 409)
        self.assertIn("busy with projects.lock; try again", r["error"])
        self.assertEqual(self.ask("/locked?lock=" + urllib.parse.quote(str(lock))), (200, {"ok": True}))

    def test_an_answer_already_sent_is_not_followed_by_another(self):
        self.assertEqual(self.ask("/half"), (200, {}))

    def test_errors_become_statuses_in_one_place(self):
        """A Refused says its status; a missing parameter is a 400 that names it; a
        KeyError otherwise is a bug: a 500, with its traceback in the log."""
        with mock.patch.object(httpbase, "log") as log:
            self.assertEqual(self.ask("/gone"), (404, {"error": "unknown project"}))
            self.assertEqual(self.ask("/param"), (400, {"error": "missing path"}))
            self.assertEqual(self.ask("/param?path=a.tex"), (200, {"path": "a.tex"}))
            log.assert_not_called()
            self.assertEqual(self.ask("/keyerror")[0], 500)
            self.assertIn("Traceback", log.call_args.args[0])
            self.assertEqual(self.ask("/value"), (400, {"error": "not a name"}))
            self.assertIn("refused GET /value", log.call_args.args[0])

    def test_a_program_not_installed_says_so_not_file_not_found(self):
        """NotInstalled is a FileNotFoundError (callers catch OSError), but the page gets
        its sentence: which program, and where to install it."""
        with mock.patch.object(httpbase, "log") as log:
            status, r = self.ask("/no-gh")
            self.assertEqual(status, 424)
            self.assertEqual(r["error"], "The GitHub CLI is not installed on this computer. "
                                         "Install it from the Home page.")
            self.assertEqual(self.ask("/no-file"), (404, {"error": "file not found"}))
            log.assert_not_called()


class StaticAssets(unittest.TestCase):
    def test_a_browser_asset_burst_survives_an_accept_pause(self):
        # Browsers open several asset connections at once. Hold accept until every
        # client has submitted its request, without relying on thread scheduling.
        srv = httpbase.Server(("127.0.0.1", 0), httpbase.Handler)
        self.addCleanup(srv.server_close)
        address = srv.server_address
        clients = 8                 # some kernels admit backlog + 1 connections
        submitted = threading.Condition()
        count = 0
        accepting = threading.Event()
        request = (f"GET /static/tabs.js HTTP/1.0\r\n"
                   f"Host: 127.0.0.1:{address[1]}\r\n\r\n").encode()

        def fetch():
            nonlocal count
            sock, error = None, None
            try:
                try:
                    sock = socket.create_connection(address, timeout=2)
                    sock.sendall(request)
                except OSError as exc:
                    error = exc
                finally:
                    with submitted:
                        count += 1
                        submitted.notify_all()
                if error:
                    return error
                accepting.wait(10)
                try:
                    with HTTPResponse(sock) as response:
                        response.begin()
                        return response.status, response.read()
                except (OSError, HTTPException) as exc:
                    return exc
            finally:
                if sock is not None:
                    sock.close()

        with ThreadPoolExecutor(max_workers=clients) as pool:
            pending = [pool.submit(fetch) for _ in range(clients)]
            try:
                with submitted:
                    self.assertTrue(submitted.wait_for(lambda: count == clients, timeout=10))
            finally:
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                self.addCleanup(srv.shutdown)
                accepting.set()
            results = [future.result(timeout=10) for future in pending]
        errors = [str(result) for result in results if isinstance(result, Exception)]
        self.assertEqual(errors, [], "static asset connections were dropped before accept")
        expected = (httpbase.STATIC / "tabs.js").read_bytes()
        for result in results:
            self.assertEqual(result, (200, expected))


class CodeStamp(unittest.TestCase):
    def test_the_code_is_looked_at_again_only_every_few_seconds(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(httpbase, "CODE_DIR", Path(tmp)), \
                mock.patch.dict(httpbase._CODE_STAMP, {"at": None, "stamp": 0.0}):
            (Path(tmp) / "server.py").write_text("")
            os.utime(Path(tmp) / "server.py", (1000, 1000))
            self.assertEqual(httpbase.code_stamp(), 1000)
            os.utime(Path(tmp) / "server.py", (2000, 2000))
            self.assertEqual(httpbase.code_stamp(), 1000, "asked again at once: as before")
            httpbase._CODE_STAMP["at"] -= httpbase.CODE_STAMP_EVERY
            self.assertEqual(httpbase.code_stamp(), 2000, "a few seconds later: the update is seen")


class App(unittest.TestCase):
    """One server's life, as the editor's and the Home page's share it."""

    def test_both_servers_take_the_same_options_and_say_who_they_are(self):
        import argparse
        import hub
        import server
        for app, port in ((server.APP, 8765), (hub.APP, hub.instances.HOME_PORT)):
            ap = argparse.ArgumentParser()
            httpbase.App.arguments(ap, port, "page")
            a = ap.parse_args(["--port", "0", "--exit-when-idle", "--idle-timings", "1,2,3"])
            self.assertEqual((a.port, a.port_tries, a.no_browser, a.exit_when_idle, a.ready_file),
                             (0, 1, False, True, None))
            ping = app.ping()
            self.assertEqual(set(ping) >= {"app", "pid", "pages", "token"}, True)
            self.assertEqual(ping["app"], {server.APP: "prism-local", hub.APP: "prism-home"}[app])

    def test_a_restart_before_serving_is_refused_and_after_it_is_the_same_command_line(self):
        app = httpbase.App("prism-test", Presence())
        with self.assertRaises(errors.Conflict):
            app.restart()
        app.args = mock.Mock(exit_when_idle=True, idle_timings="1,2,3")
        app.server = mock.Mock(server_address=("127.0.0.1", 8999))
        argv = app.successor_argv(Path("server.py"), "my paper")
        self.assertEqual(argv[argv.index("server.py"):],
                         ["server.py", "my paper", "--port", "8999", "--port-tries", "1", "--no-browser",
                          "--exit-when-idle", "--idle-timings", "1,2,3"])


if __name__ == "__main__":
    unittest.main()
