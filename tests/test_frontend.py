"""The pages' plain modules (prism_local/static/*.js with module.exports), checked under
Node by tests/frontend_check.js; and the routes client.js calls are the server's."""
import ast
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "prism_local" / "static"


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class Frontend(unittest.TestCase):
    def test_received_notation_confirmation(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "received_confirmation_check.js")],
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_github_human_approval_workflow(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "github_approvals_check.js")],
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_github_discussion_settings_and_publication(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "github_discussion_check.js")],
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_research_topic_records(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "topic_check.js")],
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_manual_input_evidence(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "manual_input_check.js")],
                           capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_usage_snapshot_ordering(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "usage_state_check.js")],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_tree_and_save_reconciliation(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "watch_check.js")], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_turn_polling_and_results(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "turn_poll_check.js")], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_notation_proposal_display(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "proposal_check.js")], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_storage_recovery(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "storage_check.js")], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_render_costs(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "performance_check.js")], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_plain_modules(self):
        r = subprocess.run([shutil.which("node"), str(HERE / "async_check.js")], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)
        r = subprocess.run([shutil.which("node"), str(HERE / "frontend_check.js")], capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_client_routes_are_the_servers(self):
        """Each request client.js makes is one server.py answers, with that method; and the
        editor's pages make theirs only through it (the route names are in one place)."""
        r = subprocess.run([shutil.which("node"), str(HERE / "client_routes.js")], capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = json.loads(r.stdout)
        routes = server_routes()
        for c in calls:
            with self.subTest(c["path"]):
                if c["method"] == "POST" and c["path"].startswith("/api/files/"):
                    self.assertIn(c["path"], routes["_files"])
                else:
                    self.assertIn(c["path"], routes["_get" if c["method"] == "GET" else "_post"])
        by = {c["path"]: c for c in calls if c["method"] == "GET"}
        self.assertEqual(by["/api/file"]["query"], "path=sec%2Fa%20b.tex")
        self.assertEqual(by["/api/upload/sync"]["query"], "paths=a.png%0Ab.png")
        self.assertEqual(by["/api/synctex/inverse"]["query"], "page=1&x=10.50&y=20.25")
        self.assertEqual(by["/api/agent/commands"]["query"], "provider=claude&refresh=1")
        for page in ("app.js", "viewer.js"):
            text = (STATIC / page).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"""\bapi\(\s*["'`]/api/""", page)


def server_routes() -> dict[str, set[str]]:
    """The paths server.py's _get, _post and _files compare the request's path with."""
    tree = ast.parse((STATIC.parent / "server.py").read_text(encoding="utf-8"))
    out = {"_get": set(), "_post": set(), "_files": set()}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name in out:
            for node in ast.walk(fn):
                if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "path"
                        and isinstance(node.ops[0], ast.Eq) and isinstance(node.comparators[0], ast.Constant)):
                    out[fn.name].add(node.comparators[0].value)
    return out


if __name__ == "__main__":
    unittest.main()
