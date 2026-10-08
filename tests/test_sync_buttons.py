"""The GitHub button's actions, end to end: a real editor server (server.py) on a clone of a
repository standing in for GitHub, driven only through the HTTP requests the page sends
(Save to GitHub now, Get changes from GitHub, the switch, History), with a co-author's
clone pushing alongside."""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tmpdirs import tmpdir  # noqa: E402
from test_lifecycle import NO_WINDOW, stop, wait_file  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "prism_local"))
import gitsync
SERVER = REPO / "prism_local" / "server.py"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HEADERS = {"Content-Type": "application/json", "X-Prism-Local": "1"}


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def identity(cwd, name):
    for k, v in (("user.email", f"{name}@example.com"), ("user.name", name), ("commit.gpgsign", "false")):
        git(cwd, "config", k, v)


@unittest.skipUnless(shutil.which("git"), "needs git")
class SyncButtons(unittest.TestCase):
    def setUp(self):
        tmp = tmpdir()
        self.remote = tmp / "paper.git"
        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        seed = tmp / "seed"
        shutil.copytree(REPO / "examples" / "minimal", seed)
        git(seed, "init", "-q", "-b", "main")
        identity(seed, "seed")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "start")
        git(seed, "remote", "add", "origin", str(self.remote))
        git(seed, "push", "-q", "-u", "origin", "main")
        self.mine = tmp / "my paper"
        git(tmp, "clone", "-q", str(self.remote), str(self.mine))
        identity(self.mine, "Me")
        gitsync.GitSync(lambda: self.mine, lambda: "build").bind_target()
        self.other = tmp / "coauthor"
        git(tmp, "clone", "-q", str(self.remote), str(self.other))
        identity(self.other, "Alice")
        self.ready = tmp / "ready.json"
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER), str(self.mine), "--port", "0", "--no-browser",
             "--ready-file", str(self.ready)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW, env={**os.environ, "PRISM_STATE_DIR": str(tmp / "state")})
        self.addCleanup(stop, self.proc)
        self.url = wait_file(self.ready)["url"].rstrip("/")
        self.wait_idle()

    # ------------------------------------------------------------ the page's requests
    def req(self, path, body=None, headers=HEADERS):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.url + path, data=data, headers=headers if body is not None else {})
        try:
            with HTTP.open(r, timeout=120) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def save_file(self, rel, text):
        """What the editor does as you type: save the buffer."""
        status, r = self.req("/api/file", {"path": rel, "content": text})
        self.assertEqual(status, 200, r)

    def button(self, action):
        status, r = self.req("/api/git/sync", {"action": action})
        self.assertEqual(status, 200, r)
        return r

    def status(self):
        return self.req("/api/tree")[1]["sync"]

    def wait_idle(self):
        for _ in range(100):
            if self.status()["state"] == "idle":
                return
            time.sleep(0.1)

    def test_target_change_keeps_file_and_pauses_upload_and_close_sync(self):
        tool = self.remote.parent / "tool.git"
        git(self.remote.parent, "init", "-q", "--bare", str(tool))
        git(self.mine, "config", "remote.origin.pushurl", str(tool))
        self.save_file("main.tex", "local edit\n")
        self.assertTrue(self.button("commit")["blocked_reason"])
        self.assertEqual(self.read("main.tex"), "local edit\n")
        import base64
        code, upload = self.req("/api/upload", {"name": "fixture.png", "data": base64.b64encode(b"PNG").decode()})
        self.assertEqual(code, 200, upload)
        stop(self.proc)
        self.assertEqual(git(tool, "for-each-ref"), "")
        self.assertEqual((self.mine / "main.tex").read_text(), "local edit\n")

    def test_agent_rejects_wrong_or_missing_project_key_before_start(self):
        code, info = self.req("/api/agent/info")
        self.assertEqual(code, 200)
        self.assertEqual(info["project_key"], self.req("/api/ping")[1]["project_key"])
        for key in (None, "another-project"):
            code, result = self.req("/api/agent", {"project_key": key, "prompt": "fixture", "provider": "unavailable"})
            self.assertEqual(code, 409)
            self.assertIn("another project", result["error"])

    def read(self, rel):
        return self.req(f"/api/file?path={rel}")[1]["content"]

    def coauthor_pushes(self, rel, old, new):
        f = self.other / rel
        git(self.other, "pull", "-q")
        f.write_text(f.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        git(self.other, "commit", "-qam", f"Alice: {new}")
        git(self.other, "push", "-q")

    def on_remote(self, rel):
        return git(self.remote, "show", f"main:{rel}")

    def assertClean(self):
        git_dir = self.mine / ".git"
        self.assertFalse((git_dir / "MERGE_HEAD").exists(), "left in a merge")
        self.assertFalse((git_dir / "index.lock").exists(), "left a lock")

    # ------------------------------------------------------------ Save to GitHub now
    def test_save_now(self):
        text = self.read("sections/intro.tex")
        self.save_file("sections/intro.tex", text.replace("convex", "strictly convex", 1))
        st = self.button("commit")
        self.assertIsNone(st["error"])
        self.assertEqual(st["ahead"], 0, "pushed")
        self.assertTrue(st["last_commit"]["message"].startswith("Saved: "))
        self.assertIn("strictly convex", self.on_remote("sections/intro.tex"))
        before = git(self.mine, "rev-parse", "HEAD")
        st = self.button("commit")                     # nothing new: nothing happens
        self.assertIsNone(st["error"])
        self.assertEqual(git(self.mine, "rev-parse", "HEAD"), before)
        self.assertClean()

    def test_save_now_clicked_twice_at_once(self):
        text = self.read("main.tex")
        self.save_file("main.tex", text + "\n% one more line\n")
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.button("commit"))) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([r["error"] for r in results], [None] * 3)
        self.assertEqual(git(self.mine, "log", "--format=%s", "-n", "1"), "Saved: main.tex")
        self.assertEqual(git(self.mine, "log", "--format=%s", "-n", "2").splitlines()[1], "start",
                         "one commit, not three")
        self.assertIn("% one more line", self.on_remote("main.tex"))
        self.assertClean()

    def test_save_now_after_a_coauthor_pushed(self):
        self.coauthor_pushes("main.tex", "\\maketitle", "\\maketitle % Alice was here")
        text = self.read("sections/intro.tex")
        self.save_file("sections/intro.tex", text.replace("convex", "uniformly convex", 1))
        st = self.button("commit")
        self.assertIsNone(st["error"])
        self.assertEqual(st["ahead"], 0)
        self.assertIn("Alice was here", self.on_remote("main.tex"))
        self.assertIn("uniformly convex", self.on_remote("sections/intro.tex"))
        self.assertIn("Alice was here", self.read("main.tex"), "the editor sees Alice's change")
        self.assertClean()

    def test_save_now_same_lines_keeps_both(self):
        self.coauthor_pushes("sections/intro.tex", "convex", "strictly convex")
        text = self.read("sections/intro.tex")
        self.save_file("sections/intro.tex", text.replace("convex", "uniformly convex", 1))
        st = self.button("commit")
        self.assertIsNone(st["error"])
        merged = self.on_remote("sections/intro.tex")
        self.assertIn("strictly convex", merged)
        self.assertIn("uniformly convex", merged)
        self.assertIn("% [prism-local] Alice's version (from GitHub):", merged)
        self.assertTrue(st["kept"], "the menu lists the place")
        self.assertEqual(st["kept"][0]["path"], "sections/intro.tex")
        self.assertClean()

    def test_unreachable_github_then_back(self):
        git(self.mine, "remote", "set-url", "origin", str(self.remote.parent / "gone.git"))
        text = self.read("main.tex")
        self.save_file("main.tex", text + "\n% offline line\n")
        st = self.button("commit")
        self.assertTrue(st["blocked_reason"], "it says so")
        self.assertEqual(git(self.mine, "log", "-1", "--format=%s"), "Saved: main.tex", "committed, kept")
        st = self.button("pull")
        self.assertIn("target changed", st["blocked_reason"])
        git(self.mine, "remote", "set-url", "origin", str(self.remote))
        st = self.button("commit")
        self.assertIsNone(st["error"])
        self.assertIn("% offline line", self.on_remote("main.tex"))
        self.assertClean()

    # ------------------------------------------------------------ Get changes from GitHub
    def test_get_changes(self):
        self.coauthor_pushes("main.tex", "\\maketitle", "\\maketitle % from Alice")
        st = self.button("pull")
        self.assertIsNone(st["error"])
        self.assertIn("Pulled 1 commit", st["notice"] or "")
        self.assertIn("from Alice", self.read("main.tex"))
        st = self.button("pull")                       # nothing new
        self.assertIsNone(st["error"])
        self.assertClean()

    def test_get_changes_with_my_edits_saved_not_committed(self):
        self.coauthor_pushes("main.tex", "\\maketitle", "\\maketitle % from Alice")
        text = self.read("sections/intro.tex")
        self.save_file("sections/intro.tex", text.replace("convex", "mine-convex", 1))
        st = self.button("pull")
        self.assertIsNone(st["error"])
        self.assertIn("from Alice", self.read("main.tex"))
        self.assertIn("mine-convex", self.read("sections/intro.tex"), "my edit is kept")
        self.assertIn("mine-convex", git(self.mine, "show", "HEAD:sections/intro.tex"), "and committed")
        self.assertClean()

    # ------------------------------------------------------------ the switch
    def test_switch_off_and_on(self):
        settings = self.mine / ".git" / "prism-local.json"
        before_settings = json.loads(settings.read_text())
        settings.write_text(json.dumps({**before_settings, "restore_rewritten": False}), encoding="utf-8")
        st = self.button("off")
        self.assertFalse(st["enabled"])
        self.assertEqual(json.loads(settings.read_text(encoding="utf-8")),
                         {**before_settings, "restore_rewritten": False, "sync": False}, "other settings stay")
        text = self.read("main.tex")
        self.save_file("main.tex", text + "\n% while off\n")
        st = self.button("commit")                     # by hand it still saves
        self.assertIsNone(st["error"])
        self.assertIn("% while off", self.on_remote("main.tex"))
        st = self.button("on")
        self.assertTrue(st["enabled"])

    # ------------------------------------------------------------ History
    def test_history_after_syncing(self):
        self.coauthor_pushes("main.tex", "\\maketitle", "\\maketitle % from Alice")
        text = self.read("main.tex")
        self.save_file("main.tex", text + "\n% mine\n")
        self.button("commit")
        status, r = self.req("/api/git/log?path=main.tex")
        self.assertEqual(status, 200)
        messages = [c["message"] for c in r["commits"]]
        self.assertIn("start", messages)
        self.assertTrue(any("from Alice" in m for m in messages), messages)
        first = r["commits"][-1]["hash"]
        status, old = self.req(f"/api/git/show?rev={first}&path=main.tex")
        self.assertEqual(status, 200)
        self.assertNotIn("from Alice", old["content"])

    def test_buttons_while_typing_and_a_coauthor_pushes(self):
        """Typing (the page saving), clicking Save now and Get changes, a co-author pushing,
        and the editor's own timer, all at once: every line reaches GitHub, nothing is left
        half-done."""
        stop_typing = threading.Event()
        typed: list[str] = []
        errors: list[str] = []

        def typing():
            i = 0
            while not stop_typing.is_set():
                text = self.read("main.tex")
                line = f"% typed {i}"
                self.save_file("main.tex", text.replace("\\end{document}", f"{line}\n\\end{{document}}"))
                typed.append(line)
                i += 1
                time.sleep(0.15)

        def coauthor():
            for i in range(6):
                f = self.other / "refs.bib"
                git(self.other, "pull", "-q", "--no-rebase")
                f.write_text(f.read_text(encoding="utf-8") + f"% Alice {i}\n", encoding="utf-8")
                git(self.other, "commit", "-qam", f"Alice {i}")
                try:
                    git(self.other, "push", "-q")
                except AssertionError:
                    pass                    # behind for a moment: her next round pushes it
                time.sleep(0.4)

        t1, t2 = threading.Thread(target=typing), threading.Thread(target=coauthor)
        t1.start(), t2.start()
        for i in range(10):
            st = self.button("commit" if i % 3 else "pull")
            if st["error"]:
                errors.append(st["error"])
            time.sleep(0.2)
        stop_typing.set()
        t1.join(), t2.join()
        git(self.other, "pull", "-q", "--no-rebase")
        git(self.other, "push", "-q")
        st = self.button("commit")
        self.assertIsNone(st["error"], st["error"])
        self.assertEqual(st["ahead"], 0)
        remote_main = self.on_remote("main.tex")
        for line in typed:
            self.assertIn(line, remote_main)
        for i in range(6):
            self.assertIn(f"% Alice {i}", self.on_remote("refs.bib"))
        self.assertEqual(errors, [], "no click failed")
        self.assertNotIn("prism-local]", remote_main, "a line added at the end never clashes")
        self.assertClean()

    # ------------------------------------------------------------ checks
    def test_requests_from_elsewhere_are_refused(self):
        status, _ = self.req("/api/git/sync", {"action": "commit"}, headers={"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        status, _ = self.req("/api/git/sync", {"action": "reset --hard"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
