"""Codex discovery for Windows GUI launchers, without invoking a model."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import json
import re
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
from backend_codex import Codex, codex_bin, protected_paths, runtime_support
from fsutil import METADATA, is_metadata
from backends import Job
from test_agent import manager


@unittest.skipUnless(os.name == "nt", "Windows desktop installation")
class CodexDiscovery(unittest.TestCase):
    def test_gui_finds_bundled_native_cli_and_follows_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "OpenAI/Codex/bin"
            first = base / "old/codex.exe"
            first.parent.mkdir(parents=True)
            first.touch()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                    mock.patch("backend_codex.find_bin", return_value=None):
                self.assertEqual(codex_bin(), str(first))
                newer = base / "new/codex.exe"
                newer.parent.mkdir()
                newer.touch()
                os.utime(first, ns=(1_000_000_000, 1_000_000_000))
                os.utime(newer, ns=(2_000_000_000, 2_000_000_000))
                self.assertEqual(codex_bin(), str(newer))
                newer.unlink()
                self.assertEqual(codex_bin(), str(first))

    def test_missing_desktop_installation_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                mock.patch("backend_codex.find_bin", return_value=None):
            self.assertIsNone(codex_bin())
            self.assertIn("not found", Codex("codex").unavailable())

    def test_override_and_path_take_precedence(self):
        with mock.patch("backend_codex.find_bin", return_value="explicit-codex"):
            self.assertEqual(codex_bin(), "explicit-codex")
        self.assertEqual(Codex("codex", {"bin": "provider-codex"}).bin(), "provider-codex")

    def test_one_disappearing_bundle_does_not_hide_another_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "OpenAI/Codex/bin"
            good, gone = base / "good/codex.exe", base / "gone/codex.exe"
            for path in (good, gone):
                path.parent.mkdir(parents=True)
                path.touch()
            real_stat = Path.stat
            observed = []

            def stat(path, *args, **kwargs):
                if path == gone:
                    observed.append(path)
                    raise FileNotFoundError(str(path))
                return real_stat(path, *args, **kwargs)

            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                    mock.patch("backend_codex.find_bin", return_value=None), \
                    mock.patch.object(Path, "glob", return_value=iter([gone, good])), \
                    mock.patch.object(Path, "is_file", return_value=True), \
                    mock.patch.object(Path, "stat", stat):
                self.assertEqual(codex_bin(), str(good))
            self.assertIn(gone, observed, "The bundle disappears after discovery, during inspection.")


class CodexCompatibility(unittest.TestCase):
    def setUp(self):
        runtime_support.cache_clear()
        self.addCleanup(runtime_support.cache_clear)

    def check(self, exec_help, resume_help):
        def run(args, **kwargs):
            text = "codex-cli 0.162.0" if args[1:] == ["--version"] else \
                resume_help if "resume" in args else exec_help
            return subprocess.CompletedProcess(args, 0, text, "")
        with mock.patch("backend_codex.run_bounded", side_effect=run):
            return runtime_support("fixture", 1)

    def test_requires_both_isolation_flags_for_new_and_resumed_turns(self):
        flags = "--ignore-user-config --ignore-rules"
        self.assertIsNone(self.check(flags, flags))
        for fresh, resumed in (("--ignore-user-config", flags),
                               (flags, "--ignore-user-config"), (flags, "--ignore-rules")):
            runtime_support.cache_clear()
            with self.subTest(fresh=fresh, resumed=resumed):
                self.assertIn("Update Codex CLI", self.check(fresh, resumed) or "")


class CodexProtectedPaths(unittest.TestCase):
    def policy(self, job):
        table = next(c.split("=", 1)[1] for c in Codex.permission_config(job)
                     if c.startswith("permissions.prism-editor.filesystem="))
        return {json.loads(path): access for path, access in
                re.findall(r'("(?:\\.|[^"\\])*")="(read|write)"', table)}

    def test_scoped_files_cannot_reopen_protected_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in (".codex", ".agents", ".aws", *([".CoDeX"] if os.name == "nt" else [])):
                job = Job(1)
                job.root, job.mode = root, "edit"
                job.scope = [f"nested/{name}/protected"]
                with self.subTest(name=name):
                    policy = self.policy(job)
                    self.assertNotIn(str(root / job.scope[0]),
                                     {p for p, access in policy.items() if access == "write"})

    def test_metadata_names_match_regardless_of_case_on_every_platform(self):
        for name in (".GIT", ".CoDeX", ".Agents", ".AWS"):
            with self.subTest(name=name):
                self.assertTrue(is_metadata(Path("nested") / name / "config"))
        self.assertFalse(is_metadata(Path("nested/git/config")))

    def test_mentioned_metadata_is_refused_before_the_turn_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            backend = Codex("codex", {"bin": "codex"})
            self.assertIsNone(backend.check_scope(root, ["main.tex"]))
            self.assertIn(".codex/x", backend.check_scope(root, ["main.tex", ".codex/x"]))
            with mock.patch.object(backend, "unavailable", return_value=None), \
                    mock.patch.object(backend, "preflight", return_value=None):
                m = manager(root, codex=backend)
                result = m.start("hi", None, "edit", scope=["nested/.Agents/x"])
                self.assertIn("read-only", result.get("error", ""))
                self.assertIsNone(m.active)  # nothing ran
                # Ask grants no writes, so a mention there is only context.
                with mock.patch.object(backend, "run", return_value={}):
                    self.assertNotIn("error", m.start("hi", None, "ask", scope=[".codex/x"]))

    def test_an_unreadable_folder_stops_the_turn_with_a_clear_reason(self):
        def walk(root, followlinks, onerror):
            onerror(PermissionError(13, "Access is denied", str(Path(root) / "locked")))
            yield from ()
        with mock.patch("fsutil.os.walk", walk), \
                self.assertRaisesRegex(OSError, r"locked for repository metadata.*did not start"):
            list(protected_paths(Path("project")))

    def test_a_folder_removed_during_the_check_is_skipped(self):
        def walk(root, followlinks, onerror):
            onerror(FileNotFoundError(2, "gone", str(Path(root) / "build-tmp")))
            yield from ()
        with mock.patch("fsutil.os.walk", walk):
            self.assertEqual(len(list(protected_paths(Path("project")))), len(METADATA))

    def test_existing_nested_metadata_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            names = (".git", ".codex", ".agents", ".aws")
            for name in names:
                (root / "nested" / name).mkdir(parents=True)
            job = Job(1)
            job.root, job.mode = root, "edit"
            policy = self.policy(job)
            for name in names:
                self.assertEqual(policy.get(str(root / "nested" / name)), "read")


class CodexIsolation(unittest.TestCase):
    def test_turn_disables_global_hooks_without_changing_user_settings(self):
        command, _ = Codex("codex", {"bin": "codex"}).command(Job(1))
        self.assertIn("--ignore-user-config", command)
        self.assertIn("features.hooks=false", command)

    def test_windows_turn_enables_native_sandbox_even_with_user_config_ignored(self):
        backend = Codex("codex", {"bin": "codex"})
        for mode in ("ask", "edit"):
            for session in (None, "existing-session"):
                with self.subTest(mode=mode, session=session), \
                        mock.patch("backend_codex.os") as platform:
                    platform.name = "nt"
                    job = Job(1)
                    job.mode, job.session_id = mode, session
                    command, _ = backend.command(job)
                    self.assertIn('windows.sandbox="elevated"', command)
                    self.assertIn("--ignore-user-config", command)
                    self.assertIn("--ignore-rules", command)
                    self.assertIn('approval_policy="never"', command)
                    self.assertIn('default_permissions="prism-editor"', command)
                    self.assertIn("permissions.prism-editor.network.enabled=false", command)
                    self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_other_platforms_do_not_select_a_windows_sandbox(self):
        with mock.patch("backend_codex.os") as platform:
            platform.name = "posix"
            command, _ = Codex("codex", {"bin": "codex"}).command(Job(1))
        self.assertFalse(any(arg.startswith("windows.") for arg in command))


if __name__ == "__main__":
    unittest.main()
