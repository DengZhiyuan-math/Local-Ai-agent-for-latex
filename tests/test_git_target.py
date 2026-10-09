"""Sync target and environment regressions. Only local temporary remotes are used."""
import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends
import gitsync
import backend_deepcode
import backend_openai
from gitenv import git_env
from tmpdirs import tmpdir


def git(root, *args):
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return r.stdout.strip()


class Targets(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir()
        self.env = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(self.base / "empty"),
                                              "GIT_CONFIG_NOSYSTEM": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = self.base / "paper"
        self.root.mkdir()
        self.remote = self.base / "paper.git"
        self.tool = self.base / "tool.git"
        for p in (self.remote, self.tool):
            git(self.base, "init", "-q", "--bare", "-b", "main", str(p))
        git(self.root, "init", "-q", "-b", "main")
        for k, v in (("user.name", "Fixture"), ("user.email", "test@example.invalid"),
                     ("commit.gpgsign", "false")):
            git(self.root, "config", k, v)
        (self.root / "main.tex").write_text("base\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        git(self.root, "remote", "add", "origin", str(self.remote))
        self.g = gitsync.GitSync(lambda: self.root, lambda: "build")

    def test_unbound_project_binds_itself_and_pushes(self):
        self.g.push()
        self.assertIsNone(self.g.status()["blocked_reason"])
        self.assertEqual(self.g._settings()["sync_target"]["branch_ref"], "refs/heads/main")
        self.assertEqual(git(self.remote, "rev-parse", "main"), git(self.root, "rev-parse", "HEAD"))

    def test_same_repository_reached_another_way_rebinds_quietly(self):
        self.g.bind_target()
        git(self.root, "config", "branch.main.remote", "origin")
        git(self.root, "config", "branch.main.merge", "refs/heads/main")
        self.assertTrue(self.g.validate_target(), self.g.blocked_reason)
        self.assertEqual(self.g._settings()["sync_target"]["configuration_fingerprint"],
                         self.g.target["configuration_fingerprint"])

    def test_create_rejects_target_changed_before_binding(self):
        import hub
        git(self.root, "remote", "remove", "origin")
        real_run, real_bind = subprocess.run, self.g.bind_target

        def created(cmd, *args, **kwargs):
            if cmd[0] == "/fixture/gh":
                git(self.root, "remote", "add", "origin", "https://github.com/owner/paper.git")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, *args, **kwargs)

        def changed_before_binding(expected=None):
            git(self.root, "remote", "set-url", "origin", "https://github.com/owner/tool.git")
            real_bind(expected)

        with mock.patch.object(hub, "gh_status", return_value={
                "logged_in": True, "gh": "/fixture/gh", "account": "owner"}), \
                mock.patch.object(hub, "project_sync", return_value=self.g), \
                mock.patch.object(hub.subprocess, "run", side_effect=created), \
                mock.patch.object(self.g, "bind_target", side_effect=changed_before_binding), \
                mock.patch.object(self.g, "push") as push, \
                mock.patch.object(hub, "protect_branch") as protect:
            result = hub.create_github_repo(self.root, "owner", "paper")
        self.assertIn("changed since confirmation", result.get("error", ""))
        self.assertEqual(result["url"], "https://github.com/owner/paper")
        push.assert_not_called()
        protect.assert_not_called()
        self.assertNotIn("sync_target", self.g._settings())
        self.assertEqual((self.root / "main.tex").read_text(), "base\n")

    def test_redirects_block_and_keep_local_edits(self):
        self.g.bind_target()
        git(self.root, "remote", "add", "tool", str(self.tool))
        for key, value in (("branch.main.pushRemote", "tool"),
                           ("remote.pushDefault", "tool"),
                           ("remote.origin.pushurl", str(self.tool)),
                           (f"url.{self.tool}.pushInsteadOf", str(self.remote)),
                           ("remote.origin.mirror", "true")):
            with self.subTest(key=key):
                git(self.root, "config", key, value)
                (self.root / "main.tex").write_text(key)
                self.g.now("commit")
                self.assertTrue(self.g.status()["blocked_reason"])
                self.assertEqual(git(self.tool, "for-each-ref"), "")
                self.assertEqual(git(self.root, "show", "HEAD:main.tex"), key)
                git(self.root, "config", "--unset-all", key)

    def test_branch_switch_and_multiple_push_urls(self):
        self.g.bind_target()
        git(self.root, "checkout", "-qb", "other")
        self.g.push()
        self.assertTrue(self.g.status()["blocked_reason"])
        git(self.root, "checkout", "-q", "main")
        git(self.root, "config", "--add", "remote.origin.pushurl", str(self.remote))
        git(self.root, "config", "--add", "remote.origin.pushurl", str(self.tool))
        self.g.push()
        self.assertTrue(self.g.status()["blocked_reason"])
        self.assertEqual(git(self.tool, "for-each-ref"), "")

    def test_binding_preserves_settings_and_upstream_branch(self):
        git(self.root, "config", "branch.main.remote", "origin")
        git(self.root, "config", "branch.main.merge", "refs/heads/paper")
        path = self.root / ".git/prism-local.json"
        path.write_text(json.dumps({"restore_rewritten": False, "sync": False}))
        self.g.bind_target()
        data = json.loads(path.read_text())
        self.assertFalse(data["restore_rewritten"])
        self.assertFalse(data["sync"])
        self.assertEqual(data["sync_target"]["branch_ref"], "refs/heads/paper")

    def test_rename_and_move_keep_bound_remote_and_branch(self):
        self.g.bind_target()
        destinations = [self.base / "renamed", self.base / "papers/moved"]
        for destination in destinations:
            with self.subTest(destination=destination.name):
                destination.parent.mkdir(exist_ok=True)
                self.root.rename(destination)
                self.root = destination
                self.assertTrue(self.g.validate_target(), self.g.blocked_reason)
                reopened = gitsync.GitSync(lambda: self.root, lambda: "build")
                self.assertTrue(reopened.validate_target(), reopened.blocked_reason)
                (self.root / "main.tex").write_text(destination.name)
                reopened.now("commit")
                self.assertEqual(git(self.remote, "show", "main:main.tex"), destination.name)
                git(self.root, "remote", "set-url", "origin", str(self.tool))
                self.assertFalse(reopened.validate_target())
                self.assertEqual(git(self.tool, "for-each-ref"), "")
                git(self.root, "remote", "set-url", "origin", str(self.remote))

    def test_inherited_git_selectors_do_not_select_tool(self):
        other = self.base / "other"
        other.mkdir()
        git(other, "init", "-q")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                                          "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.url",
                                          "GIT_CONFIG_VALUE_0": str(self.tool)}):
            self.assertTrue(self.g.check_repo())
            self.assertEqual(self.g.top, self.root)
            env = backends.agent_env()
            self.assertNotIn("GIT_DIR", env)
            self.assertNotIn("GIT_CONFIG_KEY_0", env)

    def test_conflict_merge_ignores_foreign_git_environment(self):
        (self.root / "fig.png").write_bytes(b"\x89PNG\0base")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "figure")
        self.g.bind_target()
        self.g.push()
        coauthor = self.base / "coauthor"
        git(self.base, "clone", "-qb", "main", str(self.remote), str(coauthor))
        git(coauthor, "config", "user.name", "Coauthor")
        git(coauthor, "config", "user.email", "coauthor@example.invalid")
        git(coauthor, "config", "commit.gpgsign", "false")
        (coauthor / "main.tex").write_text("coauthor's text\n")
        (coauthor / "fig.png").write_bytes(b"\x89PNG\0coauthor")
        git(coauthor, "commit", "-qam", "coauthor")
        git(coauthor, "push", "-q", "origin", "main")
        (self.root / "main.tex").write_text("my text\n")
        (self.root / "fig.png").write_bytes(b"\x89PNG\0mine")
        git(self.root, "commit", "-qam", "mine")

        foreign = self.base / "foreign"
        foreign.mkdir()
        git(foreign, "init", "-q", "-b", "main")
        rows = []
        for path, data in (("main.tex", b"FOREIGN\n"), ("fig.png", b"\x89PNG\0FOREIGN")):
            (foreign / path).write_bytes(data)
            oid = git(foreign, "hash-object", "-w", path)
            rows.extend(f"100644 {oid} {stage}\t{path}\n" for stage in (1, 2, 3))
        subprocess.run(["git", "update-index", "--index-info"], cwd=foreign,
                       input="".join(rows), text=True, capture_output=True, check=True)
        with mock.patch.dict(os.environ, {
                "GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                "GIT_INDEX_FILE": str(foreign / ".git/index"),
                "GIT_OBJECT_DIRECTORY": str(foreign / ".git/objects"),
                "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "remote.origin.url",
                "GIT_CONFIG_VALUE_0": str(self.tool)}):
            self.g.pull()
        self.assertIsNone(self.g.error)
        text = (self.root / "main.tex").read_text()
        self.assertIn("my text", text)
        self.assertIn("coauthor's text", text)
        self.assertNotIn("FOREIGN", text)
        self.assertEqual((self.root / "fig.png").read_bytes(), b"\x89PNG\0mine")
        self.assertEqual((self.root / "fig.from-Coauthor.png").read_bytes(), b"\x89PNG\0coauthor")
        self.assertEqual(git(self.root, "ls-files", "-u"), "")
        self.assertEqual(git(self.tool, "for-each-ref"), "")

    def test_env_keeps_auth_transport_and_platform_variables(self):
        base = {"GIT_SSH_COMMAND": "ssh", "HTTPS_PROXY": "proxy", "PATH": "path",
                "GIT_CONFIG_PARAMETERS": "injection", "git_dir": "bad"}
        self.assertEqual(git_env(base), {"GIT_SSH_COMMAND": "ssh", "HTTPS_PROXY": "proxy",
                                         "PATH": "path", "GIT_TERMINAL_PROMPT": "0"})

    def test_worktree_binding_and_git_hub_target(self):
        import hub
        work = self.base / "worktree"
        git(self.root, "worktree", "add", "-qb", "work", str(work))
        sync = gitsync.GitSync(lambda: work, lambda: "build")
        self.assertTrue(sync.check_repo())
        hook = Path(git(work, "rev-parse", "--git-path", "hooks/pre-push"))
        if not hook.is_absolute():
            hook = work / hook
        self.assertIn(gitsync.GUARD_MARK, hook.read_text())
        sync.bind_target()
        sync.push()
        self.assertEqual(git(self.remote, "rev-parse", "work"), git(work, "rev-parse", "HEAD"))
        git(work, "config", "branch.work.pushRemote", "tool")
        self.assertIsNone(hub.github_of(work))
        self.assertIn("pushRemote", hub.fetch_one(work))

    def test_moved_worktree_keeps_binding(self):
        work = self.base / "worktree"
        git(self.root, "worktree", "add", "-qb", "work", str(work))
        sync = gitsync.GitSync(lambda: work, lambda: "build")
        sync.bind_target()
        moved = self.base / "moved-worktree"
        git(self.root, "worktree", "move", str(work), str(moved))
        work = moved
        self.assertTrue(sync.validate_target(), sync.blocked_reason)
        reopened = gitsync.GitSync(lambda: moved, lambda: "build")
        self.assertTrue(reopened.validate_target(), reopened.blocked_reason)
        (moved / "main.tex").write_text("moved worktree\n")
        reopened.now("commit")
        self.assertEqual(git(self.remote, "show", "work:main.tex"), "moved worktree")
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)"), "refs/heads/work")
        # Moving the owner repository also moves the worktree's actual git_dir.
        owner = self.base / "moved-owner"
        self.root.rename(owner)
        self.root = owner
        git(owner, "worktree", "repair", str(moved))
        self.assertFalse(reopened.validate_target(), "cached repository location still requires reopening")
        repaired = gitsync.GitSync(lambda: moved, lambda: "build")
        self.assertTrue(repaired.validate_target(), repaired.blocked_reason)
        repaired.push()
        self.assertIsNone(repaired.error)

    def test_detached_local_upstream_and_stale_confirmation(self):
        self.g.bind_target()
        expected = self.g.status()["target"]
        git(self.root, "checkout", "-q", "--detach")
        self.assertFalse(self.g.validate_target())
        git(self.root, "checkout", "-q", "main")
        git(self.root, "config", "branch.main.remote", ".")
        self.assertFalse(self.g.validate_target())
        git(self.root, "config", "--unset", "branch.main.remote")
        git(self.root, "checkout", "-qb", "new-branch")
        with self.assertRaisesRegex(ValueError, "changed since confirmation"):
            self.g.bind_target(expected)
        self.assertEqual(self.g.status()["bound_target"]["branch_ref"], "refs/heads/main")

    def test_push_only_bound_branch_even_with_tags_and_custom_refspec(self):
        git(self.root, "config", "remote.origin.push", "refs/heads/main:refs/heads/tool")
        git(self.root, "config", "push.followTags", "true")
        git(self.root, "tag", "-am", "fixture", "tagged")
        self.g.bind_target()
        self.g.push()
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)"), "refs/heads/main")

    def test_github_transport_identity_and_secret_redaction(self):
        expected = "github.com/owner/paper"
        for url in ("git@github.com:Owner/Paper.git", "https://token@github.com/Owner/Paper.git",
                    "ssh://git@github.com:22/Owner/Paper.git"):
            self.assertEqual(gitsync.repository_identity(url, self.root), expected)
        git(self.root, "remote", "set-url", "origin", "https://token@github.com/owner/paper.git")
        git(self.root, "config", "remote.origin.pushurl", "git@github.com:owner/paper.git")
        self.g.bind_target()
        self.assertNotIn("token", json.dumps(self.g.status()))
        self.assertNotIn("token", (self.root / ".git/prism-local.json").read_text())
        self.assertNotIn("token", gitsync.safe_git_message("failed: https://token@github.com/owner/paper.git?secret=yes"))

    def test_fetch_upstream_remote_branch_without_origin_assumption(self):
        git(self.root, "remote", "rename", "origin", "paper")
        git(self.root, "config", "branch.main.remote", "paper")
        git(self.root, "config", "branch.main.merge", "refs/heads/document")
        self.g.bind_target()
        self.g.push()
        clone = self.base / "coauthor"
        git(self.base, "clone", "-qb", "document", str(self.remote), str(clone))
        git(clone, "config", "user.name", "Fixture")
        git(clone, "config", "user.email", "test@example.invalid")
        git(clone, "config", "commit.gpgsign", "false")
        (clone / "main.tex").write_text("from coauthor\n")
        git(clone, "commit", "-qam", "coauthor")
        git(clone, "push", "-q", "origin", "HEAD:refs/heads/document")
        self.g.pull()
        self.assertEqual((self.root / "main.tex").read_text(), "from coauthor\n")


class SessionOwnership(unittest.TestCase):
    def test_deepcode_collision_allows_a_b_a_and_rejects_foreign_sessions(self):
        with tempfile.TemporaryDirectory(prefix="dc-", dir="/tmp" if Path("/tmp").is_dir() else None) as tmp:
            base = Path(tmp).resolve()
            a, b = base / "a-b/c", base / "a/b-c"
            a.mkdir(parents=True)
            b.mkdir(parents=True)
            if len(str(b)) > 64:
                self.skipTest("requires a short native project path")
            sid = "session-123"
            backend = backend_deepcode.DeepCode("deepcode")
            with mock.patch.object(Path, "home", return_value=base / "home"):
                directory = backend_deepcode.sessions_dir(a)
                self.assertEqual(directory, backend_deepcode.sessions_dir(b))
                directory.mkdir(parents=True)
                index = directory / "sessions-index.json"
                entries = [{"id": sid, "updateTime": 1000}]
                index.write_text(json.dumps({"originalPath": str(a), "entries": entries}))
                (directory / f"{sid}.jsonl").write_text(json.dumps({
                    "id": "native-message-id", "sessionId": sid, "role": "system", "content":
                    '# Local Workspace Environment\n\n```json\n' +
                    json.dumps({"root path": str(a), "pwd": str(a)}) + '\n```'}))
                self.assertIsNone(backend.check_session(a, sid))
                self.assertEqual(backend_deepcode.latest_session(a, 0), sid)
                self.assertTrue(backend.check_session(b, sid))
                self.assertIsNone(backend_deepcode.latest_session(b, 0))
                # The native CLI rewrites originalPath when a colliding project saves.
                sid_b = "session-b"
                entries.append({"id": sid_b, "updateTime": 2000})
                (directory / f"{sid_b}.jsonl").write_text(json.dumps({
                    "id": "native-message-b", "sessionId": sid_b, "role": "system", "content":
                    '# Local Workspace Environment\n\n```json\n' +
                    json.dumps({"root path": str(b), "pwd": str(b)}) + '\n```'}))
                index.write_text(json.dumps({"originalPath": str(b), "entries": entries}))
                self.assertIsNone(backend.check_session(b, sid_b))
                self.assertTrue(backend.check_session(b, sid))
                self.assertTrue(backend.check_session(a, sid_b))
                self.assertIsNone(backend.check_session(a, sid))
                # Returning to A rewrites the shared index again; B remains resumable.
                entries[0]["updateTime"] = 3000
                index.write_text(json.dumps({"originalPath": str(a), "entries": entries}))
                self.assertIsNone(backend.check_session(a, sid))
                self.assertIsNone(backend.check_session(b, sid_b))
                self.assertTrue(backend.check_session(b, sid))
                self.assertTrue(backend.check_session(a, sid_b))

    def test_native_cli_metadata_rejects_foreign_project(self):
        import backend_codex
        import backend_claude
        root, other, home = tmpdir(), tmpdir(), tmpdir()
        sid = "session-123"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            path = home / "sessions/2026/10/08" / f"rollout-fixture-{sid}.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(root)}}))
            backend = backend_codex.Codex("codex")
            self.assertIsNone(backend.check_session(root, sid))
            self.assertTrue(backend.check_session(other, sid))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), mock.patch.object(backend_claude, "use_config_dir"):
            code = __import__('re').sub(r"[^A-Za-z0-9]", "-", str(root))
            path = home / "projects" / code / f"{sid}.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"sessionId": sid, "cwd": str(other)}))
            backend = backend_claude.ClaudeCode("claude")
            self.assertTrue(backend.check_session(root, sid))
            path.write_text(json.dumps({"sessionId": sid, "cwd": str(root)}))
            self.assertIsNone(backend.check_session(root, sid))
    def test_deepcode_requires_local_index_and_file(self):
        root, other, home = tmpdir(), tmpdir(), tmpdir()
        sid = "session-123"
        backend = backend_deepcode.DeepCode("deepcode")
        with mock.patch.object(Path, "home", return_value=home):
            directory = backend_deepcode.sessions_dir(root)
            directory.mkdir(parents=True)
            (directory / "sessions-index.json").write_text(json.dumps({"entries": [{"id": sid}]}))
            self.assertTrue(backend.check_session(root, sid))
            (directory / f"{sid}.jsonl").write_text('{}\n')
            self.assertTrue(backend.check_session(root, sid))
            (directory / "sessions-index.json").write_text(json.dumps({
                "originalPath": str(root), "entries": [{"id": sid}]}))
            self.assertTrue(backend.check_session(root, sid))
            (directory / f"{sid}.jsonl").write_text(json.dumps({
                "id": "native-message-id", "sessionId": sid, "role": "system", "content":
                '# Local Workspace Environment\n\n```json\n' +
                json.dumps({"root path": str(root), "pwd": str(root)}) + '\n```'}))
            self.assertIsNone(backend.check_session(root, sid))
            self.assertTrue(backend.check_session(other, sid))
            self.assertTrue(backend.check_session(root, "../escape"))

    def test_deepcode_settings_cannot_reinject_git_environment(self):
        root, home = tmpdir(), tmpdir()
        backend = backend_deepcode.DeepCode("deepcode")
        with mock.patch.object(Path, "home", return_value=home), mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fixture"}):
            for source in (home / ".deepcode/settings.json", root / ".deepcode/settings.json"):
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(json.dumps({"env": {"GIT_DIR": "secret-path"}}))
                reason = backend.preflight(root)
                self.assertIn("GIT_DIR", reason)
                self.assertIn(str(source), reason)
                self.assertNotIn("secret-path", reason)
                source.unlink()

    def test_api_session_is_bound_to_project(self):
        backend = backend_openai.OpenAICompat("fixture", {"base_url": "http://localhost:9", "model": "test", "api_key_env": ""})
        root, other = tmpdir(), tmpdir()
        backend.sessions["sid"] = []
        backend.session_projects["sid"] = __import__('registry').project_key(root)
        self.assertIsNone(backend.check_session(root, "sid"))
        self.assertTrue(backend.check_session(other, "sid"))


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class Frontend(unittest.TestCase):
    def test_conversations_are_isolated_by_project_and_provider(self):
        source = (Path(__file__).resolve().parents[1] / "prism_local/static/app.js").read_text(encoding="utf-8")
        start = source.index("const conversationKey =")
        end = source.index("// The account", start)
        script = '''const assert = require('node:assert/strict');
const P = { projectKey: 'paper' }, C = { provider: 'deepcode' };
const values = new Map([['chat.session.deepcode', 'legacy-tool-session']]);
const store = { get: (k,d) => values.has(k) ? values.get(k) : d, set: (k,v) => values.set(k,v) };
''' + source[start:end] + '''
assert.equal(provGet('session'), null);
provSet('session', 'paper-session'); provSet('log', 'paper-log'); provSet('context', {used: 12});
P.projectKey = 'tool'; assert.equal(provGet('session'), null); assert.equal(provGet('log'), null);
provSet('session', 'tool-session'); P.projectKey = 'paper'; assert.equal(provGet('session'), 'paper-session');
C.provider = 'codex'; assert.equal(provGet('context'), null); assert.equal(provGet('session'), null);
C.provider = 'deepcode'; assert.equal(provGet('log'), 'paper-log');
'''
        subprocess.run([shutil.which("node"), "-e", script], check=True, capture_output=True, text=True)

    def test_sync_menu_clears_stale_github_link_and_shows_binding(self):
        source = (Path(__file__).resolve().parents[1] / "prism_local/static/app.js").read_text(encoding="utf-8")
        start = source.index("function renderSync(st)")
        end = source.index("function syncMenu", start)
        script = '''const assert = require('node:assert/strict');
const elements = new Map(), S = { githubUrl: 'https://github.com/owner/tool' };
const $ = k => { if (!elements.has(k)) elements.set(k, {removeAttribute(name) { delete this[name]; }}); return elements.get(k); };
const document = {querySelectorAll: () => []}, esc = s => s, ago = () => 'now';
$('#sync-github').href = S.githubUrl;
''' + source[start:end] + '''
const bound = {repository_identity:'github.com/owner/paper',branch_ref:'refs/heads/main'};
renderSync({own:true,enabled:true,remote:true,state:'idle',target:null,bound_target:bound,blocked_reason:'Fetch and push differ'});
assert.equal(S.githubUrl, ''); assert.equal($('#sync-github').hidden, true);
assert.equal($('#sync-github').href, undefined); assert.equal($('#sync-label').textContent,'Sync paused');
assert.ok($('#sync-detail').innerHTML.includes('github.com/owner/paper'));
'''
        subprocess.run([shutil.which("node"), "-e", script], check=True, capture_output=True, text=True)
