"""Protocol tests plus opt-in-by-availability native sandbox probes.

Protocol fakes test copying/publication/Stop wiring, not OS isolation. Native tests
run controlled Python commands; they do not contact a model or a remote service.
"""
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build
import guarded_compile as guarded
from project import Project
from tmpdirs import tmpdir


class CompileCase(unittest.TestCase):
    def setUp(self):
        self.base = tmpdir()
        self.root = self.base / "project"
        self.root.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.env = mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.write("main.tex", r"\documentclass{article}\begin{document}$x$\end{document}")
        self.write("build/main.pdf", "original PDF")
        self.project = Project(self.root)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def custom(self, script):
        self.write("prism.json", json.dumps({"build": {"draft": [sys.executable, "-I", "-c", script]}}))


class FakeRunner:
    """No execution: model the isolated driver protocol at its process seam."""
    def __init__(self, action=None, payload=None):
        self.action = action
        self.payload = {"exit": 0, "output": "controlled diagnostic", "diagnostics": [],
                        "cancelled": False, "timed_out": False, "builder": "builtin",
                        **(payload or {})}
        self.stopped = threading.Event()
        self.timed_out = False
        self.argv = self.cwd = self.env = None

    def run(self, argv, cwd, env=None):
        self.argv, self.cwd, self.env = argv, cwd, env
        self.request = json.loads(Path(argv[-2]).read_text())
        self.copy = Path(self.request["root"])
        out = self.copy / self.request["outdir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "main.pdf").write_bytes(b"%PDF controlled")
        (out / "main.log").write_text("controlled log")
        Path(argv[-1]).write_text(json.dumps(self.payload))
        if self.action:
            self.action(self)
        return 0, ""

    def stop(self):
        self.stopped.set()


class Protocol(CompileCase):
    def run_guarded(self, runner=None, **kwargs):
        self.runner = runner or FakeRunner()
        with mock.patch.object(guarded, "availability", return_value={"supported": True, "reason": "protocol fake"}), \
                mock.patch.object(build, "Runner", return_value=self.runner):
            return guarded.guarded_compile(self.project, **kwargs)

    def test_unsupported_fails_closed_without_starting_a_build(self):
        self.custom("raise RuntimeError('must not run')")
        with mock.patch.object(guarded, "availability", return_value={"supported": False, "reason": "no native isolation"}), \
                mock.patch.object(build, "Runner") as runner:
            result = guarded.guarded_compile(self.project)
        runner.assert_not_called()
        self.assertEqual(result["exit"], 127)
        self.assertTrue(result["guard_unavailable"])
        self.assertFalse(result["pdf_updated"])
        self.assertIn("no native isolation", result["output"])
        self.assertFalse(self.project.building)

    def test_other_platforms_are_unsupported(self):
        with mock.patch.object(sys, "platform", "linux"):
            guarded._availability.cache_clear()
            self.assertFalse(guarded.availability()["supported"])
        guarded._availability.cache_clear()

    def test_only_safe_inputs_enter_the_copy_and_env(self):
        self.write(".git/config", "secret")
        self.write(".aws/credentials", "secret")
        self.write("research/knowledge.json", "secret receipt")
        self.write("build/old.tex", "stale generated input")
        self.write("main.aux", "stale auxiliary")
        self.write("figures/plot.pdf", "source figure")
        self.write("style.sty", "source style")
        (self.root / "external.txt").symlink_to(self.state / "secret")
        (self.root / "linked").symlink_to(self.state, target_is_directory=True)
        (self.root / "local-state").mkdir()
        self.write("local-state/secret", "local state")
        with mock.patch.dict(os.environ, {"PRISM_STATE_DIR": str(self.root / "local-state"),
                                           "OPENAI_API_KEY": "do-not-copy", "ANTHROPIC_API_KEY": "do-not-copy",
                                           "PYTHONPATH": "do-not-copy"}):
            def inspect(runner):
                names = {p.relative_to(runner.copy).as_posix() for p in runner.copy.rglob("*") if p.is_file()}
                self.assertEqual(names, {"main.tex", "style.sty", "figures/plot.pdf", "build/main.pdf", "build/main.log"})
                for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PYTHONPATH", "PRISM_STATE_DIR"):
                    self.assertNotIn(key, runner.env)
                self.assertNotEqual(runner.cwd, self.root)
                self.assertIn("-I", runner.argv)
                self.assertIn("-B", runner.argv)
                self.assertEqual(Path(runner.argv[runner.argv.index("-c") + 2]), Path(build.__file__).resolve().parent)
            result = self.run_guarded(FakeRunner(inspect))
        self.assertEqual(result["exit"], 0)
        self.assertFalse(self.runner.copy.exists())

    def test_builtin_publishes_only_selected_pdf_and_log(self):
        def artifacts(runner):
            for name in ("new.tex", "main.sty", "knowledge.json", "main.synctex.gz", "main.fls"):
                (runner.copy / "build" / name).write_text("must not return")
        result = self.run_guarded(FakeRunner(artifacts), clean=True)
        self.assertEqual(result["exit"], 0)
        self.assertTrue(result["pdf_updated"])
        self.assertTrue(self.runner.request["clean"])
        self.assertEqual(result["published"], ["build/main.pdf", "build/main.log"])
        self.assertEqual(sorted(p.name for p in (self.root / "build").iterdir()), ["main.log", "main.pdf"])

    def test_custom_build_returns_diagnostics_without_publishing(self):
        self.custom("pass")
        result = self.run_guarded()
        self.assertEqual(result["exit"], 0)
        self.assertFalse(result["pdf_updated"])
        self.assertEqual((self.root / "build/main.pdf").read_text(), "original PDF")
        self.assertIn("diagnostics only", result["output"])

    def assert_diagnostics_only(self, result, reason):
        self.assertEqual(result["exit"], 0, result["output"])
        self.assertFalse(result["pdf_updated"])
        self.assertEqual(result["published"], [])
        self.assertIn(reason, result["publication_restricted"])
        self.assertIn("diagnostics only", result["output"])
        self.assertEqual((self.root / "build/main.pdf").read_text(), "original PDF")
        self.assertFalse((self.root / "build/main.log").exists())
        self.assertIsNotNone(self.runner.argv)  # diagnostics still run behind the OS boundary

    def test_dynamic_tex_markers_never_publish_even_with_unchanged_inputs(self):
        self.write("prism.json", json.dumps({"engine": "pdflatex"}))
        for code in (r"\csname z\endcsname", r"\char122", r"\directlua{dofile('payload.txt')}",
                     r"\openout1=generated.tex", r"\def\dynamic{z}", r"\let\dynamic=z"):
            with self.subTest(code=code):
                self.write("main.tex", r"\documentclass{article}\begin{document}" + code + r"\end{document}")
                self.write("payload.txt", "tex.sprint(string.char(36,122,36))")
                self.assert_diagnostics_only(self.run_guarded(), "Dynamic TeX marker")

    def test_lua_engine_never_publishes_even_without_dynamic_source_markers(self):
        for engine in ("lualatex", "luatex", "lualatex-dev"):
            with self.subTest(engine=engine):
                self.write("prism.json", json.dumps({"engine": engine}))
                self.assert_diagnostics_only(self.run_guarded(), "LuaLaTeX")
        self.write("prism.json", "{}")
        self.write("main.tex", "% !TEX program = lualatex\n" + (self.root / "main.tex").read_text())
        self.assert_diagnostics_only(self.run_guarded(), "LuaLaTeX")

    def test_shell_escape_never_publishes(self):
        self.write("prism.json", json.dumps({"shell_escape": True}))
        self.assert_diagnostics_only(self.run_guarded(clean=True), "Shell escape")

    def test_script_and_executable_inputs_never_publish(self):
        for name in ("payload.lua", "payload.py", "payload.js", "payload.sh", "payload.ps1"):
            with self.subTest(name=name):
                path = self.write(name, "construct mathematical output")
                self.assert_diagnostics_only(self.run_guarded(), "Script or executable input")
                path.unlink()

    @unittest.skipIf(os.name == "nt", "Windows chmod does not provide Unix executable mode bits.")
    def test_unix_executable_mode_input_never_publishes(self):
        path = self.write("payload.txt", "construct mathematical output")
        path.chmod(0o700)
        self.assert_diagnostics_only(self.run_guarded(), "Script or executable input")

    def test_dynamic_tex_in_misnamed_non_tex_input_never_publishes(self):
        self.write("payload.dat", r"\char122")
        self.assert_diagnostics_only(self.run_guarded(), "payload.dat")

    def test_diagnostic_only_dynamic_build_still_honours_stop(self):
        self.write("prism.json", json.dumps({"shell_escape": True}))
        def stop(runner):
            self.assertTrue(self.project.stop_build())
            self.assertTrue(runner.stopped.is_set())
            raise build.Stopped()
        result = self.run_guarded(FakeRunner(stop))
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["pdf_updated"])
        self.assertIn("Shell escape", result["publication_restricted"])

    def test_copied_source_changes_prevent_publication(self):
        result = self.run_guarded(FakeRunner(lambda runner: (runner.copy / "main.tex").write_text("changed")))
        self.assertEqual(result["exit"], 126)
        self.assertIn("inputs changed", result["output"])
        self.assertFalse(result["pdf_updated"])
        self.assertEqual((self.root / "build/main.pdf").read_text(), "original PDF")

    def test_new_copied_source_prevents_publication(self):
        result = self.run_guarded(FakeRunner(lambda runner: (runner.copy / "new.tex").write_text("changed")))
        self.assertEqual(result["exit"], 126)
        self.assertFalse((self.root / "new.tex").exists())
        self.assertFalse(result["pdf_updated"])

    def test_manual_source_edit_during_build_keeps_old_pdf(self):
        result = self.run_guarded(FakeRunner(lambda runner: self.write("main.tex", "manual source")))
        self.assertEqual(result["exit"], 126)
        self.assertIn("Project inputs changed", result["output"])
        self.assertEqual((self.root / "main.tex").read_text(), "manual source")
        self.assertEqual((self.root / "build/main.pdf").read_text(), "original PDF")

    def test_new_manual_source_during_build_keeps_old_pdf(self):
        result = self.run_guarded(FakeRunner(lambda runner: self.write("new.tex", "manual source")))
        self.assertEqual(result["exit"], 126)
        self.assertFalse(result["pdf_updated"])

    def test_linked_output_is_not_followed_by_host(self):
        secret = self.state / "key"
        secret.write_text("secret-content-must-not-appear")
        def redirect(runner):
            log = runner.copy / "build/main.log"
            log.unlink()
            log.symlink_to(secret)
        result = self.run_guarded(FakeRunner(redirect))
        self.assertEqual(result["exit"], 126)
        self.assertNotIn(secret.read_text(), result["output"])
        self.assertEqual((self.root / "build/main.pdf").read_text(), "original PDF")

    def test_linked_result_is_not_followed_by_host(self):
        secret = self.state / "key"
        secret.write_text("secret-content-must-not-appear")
        def redirect(runner):
            result = Path(runner.argv[-1])
            result.unlink()
            result.symlink_to(secret)
        result = self.run_guarded(FakeRunner(redirect))
        self.assertEqual(result["exit"], 126)
        self.assertNotIn(secret.read_text(), result["output"])

    def test_original_output_symlink_is_not_overwritten(self):
        canary = self.base / "canary"
        canary.write_text("untouched")
        pdf = self.root / "build/main.pdf"
        pdf.unlink()
        pdf.symlink_to(canary)
        result = self.run_guarded()
        self.assertEqual(result["exit"], 126)
        self.assertEqual(canary.read_text(), "untouched")

    def test_metadata_and_research_output_folders_are_refused(self):
        for folder in (".git", ".codex", "research", "other/Research"):
            with self.subTest(folder=folder):
                self.write("prism.json", json.dumps({"outdir": folder}))
                result = self.run_guarded()
                self.assertEqual(result["exit"], 126)
                self.assertIsNone(self.runner.argv)
                self.assertFalse((self.root / folder / "main.pdf").exists())

    def test_directory_swap_cannot_redirect_host_artifact_read(self):
        scratch = self.base / "scratch"
        (scratch / "folder").mkdir(parents=True)
        (scratch / "folder/data").write_text("safe")
        (self.state / "data").write_text("private")
        real_resolve = Path.resolve
        swapped = False
        def swap(path, *args, **kwargs):
            nonlocal swapped
            resolved = real_resolve(path, *args, **kwargs)
            if path == scratch / "folder/data" and not swapped:
                swapped = True
                (scratch / "folder").rename(scratch / "old")
                (scratch / "folder").symlink_to(self.state, target_is_directory=True)
            return resolved
        # Replace the directory after the initial path check. Both native
        # handle readers and POSIX directory-descriptor readers must refuse it.
        with mock.patch.object(Path, "resolve", swap):
            with self.assertRaises((OSError, ValueError)):
                guarded._read_plain(scratch, "folder/data")
        self.assertTrue(swapped)

    def test_publication_holds_existing_project_files_lock(self):
        replace = guarded.replace_file
        def check(path, raw):
            self.assertTrue(self.project.files_lock._is_owned())
            replace(path, raw)
        with mock.patch.object(guarded, "replace_file", side_effect=check):
            self.assertTrue(self.run_guarded()["pdf_updated"])

    def test_failed_build_keeps_original_pdf_and_diagnostics(self):
        result = self.run_guarded(FakeRunner(payload={"exit": 1, "output": "tex failure"}))
        self.assertEqual(result["exit"], 1)
        self.assertIn("tex failure", result["output"])
        self.assertFalse(result["pdf_updated"])

    def test_cancel_before_execution(self):
        cancel = threading.Event()
        cancel.set()
        result = self.run_guarded(cancel=cancel)
        self.assertTrue(result["cancelled"])
        self.assertIsNone(self.runner.argv)

    def test_cancel_stops_outer_runner_and_releases_project_lock(self):
        cancel = threading.Event()
        def wait_for_stop(runner):
            cancel.set()
            self.assertTrue(runner.stopped.wait(2))
            raise build.Stopped()
        result = self.run_guarded(FakeRunner(wait_for_stop), cancel=cancel)
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["pdf_updated"])
        self.assertFalse(self.project.building)
        self.assertIsNone(self.project._running)
        self.assertFalse(self.runner.copy.exists())

    def test_existing_stop_button_controls_outer_runner(self):
        def stop(runner):
            self.assertTrue(self.project.stop_build())
            self.assertTrue(runner.stopped.is_set())
            raise build.Stopped()
        self.assertTrue(self.run_guarded(FakeRunner(stop))["cancelled"])

    def test_other_build_busy_does_not_start(self):
        self.project._build_lock.acquire()
        try:
            self.assertEqual(self.run_guarded(), {"busy": True})
            self.assertIsNone(self.runner.argv)
        finally:
            self.project._build_lock.release()

    def test_profile_has_default_deny_and_explicit_boundaries(self):
        scratch = self.base / "scratch"
        scratch.mkdir()
        profile = guarded._profile(scratch, denied=[self.root])
        self.assertIn("(deny default)", profile)
        self.assertIn("(deny network*)", profile)
        self.assertIn('(allow file-write* (subpath ' + json.dumps(str(scratch), ensure_ascii=False) + ')', profile)
        self.assertIn('(subpath ' + json.dumps(str(self.root), ensure_ascii=False) + ')', profile.split("(deny file-read*")[1])
        self.assertIn('(subpath ' + json.dumps(str(self.state), ensure_ascii=False) + ')', profile.split("(deny file-read*")[1])
        self.assertNotIn("(allow process*)", profile)


class NativeSandbox(CompileCase):
    def setUp(self):
        super().setUp()
        support = guarded.availability()
        if not support["supported"]:
            self.skipTest(support["reason"])

    def test_native_write_read_network_and_generated_source_boundaries(self):
        secret = self.state / "research-edit-key"
        secret.write_text("private-research-key")
        targets = [str(self.root / "main.tex"), str(self.root / ".git/config"),
                   str(self.root / "research/knowledge.json"), str(secret)]
        script = r"""
import errno, os, pathlib, socket
targets = TARGETS
for name in targets:
    try:
        pathlib.Path(name).write_text('unapproved')
    except OSError as error:
        assert error.errno in (errno.EPERM, errno.EACCES), error
    else:
        raise AssertionError('original write succeeded')
try:
    pathlib.Path(SECRET).read_bytes()
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES), error
else:
    raise AssertionError('private read succeeded')
try:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES), error
else:
    raise AssertionError('network allowed')
assert 'OPENAI_API_KEY' not in os.environ
pathlib.Path('main.tex').write_text('copied input may change')
pathlib.Path('fake.tex').write_text('unapproved generated source')
pathlib.Path('build/main.pdf').write_text('%PDF generated')
pathlib.Path('build/main.log').symlink_to(SECRET)
print('native boundaries held')
""".replace("TARGETS", repr(targets)).replace("SECRET", repr(str(secret)))
        self.write(".git/config", "original metadata")
        self.write("research/knowledge.json", "original knowledge")
        original = (self.root / "main.tex").read_bytes()
        self.custom(script)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "private-key"}):
            result = guarded.guarded_compile(self.project)
        self.assertEqual(result["exit"], 0, result["output"])
        self.assertIn("native boundaries held", result["output"])
        self.assertEqual((self.root / "main.tex").read_bytes(), original)
        self.assertEqual((self.root / ".git/config").read_text(), "original metadata")
        self.assertEqual((self.root / "research/knowledge.json").read_text(), "original knowledge")
        self.assertEqual(secret.read_text(), "private-research-key")
        self.assertFalse((self.root / "fake.tex").exists())
        self.assertFalse(result["pdf_updated"])

    def test_builtin_script_engine_runs_in_copy_but_does_not_publish_artifacts(self):
        # This is a local Python stand-in for a TeX engine, not a real TeX acceptance.
        executable = self.write("tools/pdflatex", "#!" + sys.executable + "\n" + r"""
import pathlib,sys
out = pathlib.Path(next(arg.split('=',1)[1] for arg in sys.argv if arg.startswith('-output-directory=')))
out.mkdir(exist_ok=True)
assert pathlib.Path('main.tex').is_file()
(out / 'main.pdf').write_bytes(b'%PDF controlled engine')
(out / 'main.log').write_text('controlled engine log')
(out / 'fake.tex').write_text('generated source cannot return')
""")
        executable.chmod(0o700)
        environment = guarded._environment
        def with_engine(path):
            env = environment(path)
            env["PATH"] = str(path / "project/tools") + os.pathsep + env["PATH"]
            return env
        with mock.patch.object(guarded, "_environment", side_effect=with_engine):
            result = guarded.guarded_compile(self.project)
        self.assertEqual(result["exit"], 0, result["output"])
        self.assertFalse(result["pdf_updated"])
        self.assertIn("Script or executable input", result["publication_restricted"])
        self.assertEqual((self.root / "build/main.pdf").read_text(), "original PDF")
        self.assertFalse((self.root / "build/main.log").exists())
        self.assertFalse((self.root / "build/fake.tex").exists())
        self.assertEqual((self.root / "tools/pdflatex").stat().st_mode & 0o777, 0o700)

    def test_stop_ends_driver_compiler_and_ordinary_descendant(self):
        self.custom("import json,os,pathlib,subprocess,sys,time; "
                    "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                    "pathlib.Path('pids.json').write_text(json.dumps([os.getpid(),child.pid])); time.sleep(60)")
        cancel = threading.Event()
        scratch = []
        environment = guarded._environment
        def capture(path):
            scratch.append(path)
            return environment(path)
        results = []
        with mock.patch.object(guarded, "_environment", side_effect=capture):
            worker = threading.Thread(target=lambda: results.append(guarded.guarded_compile(self.project, cancel=cancel)))
            worker.start()
            try:
                deadline = time.monotonic() + 10
                record = None
                while time.monotonic() < deadline:
                    if scratch:
                        record = scratch[0] / "project/pids.json"
                        if record.exists():
                            break
                    time.sleep(0.05)
                self.assertIsNotNone(record)
                self.assertTrue(record.exists())
                pids = json.loads(record.read_text())
            finally:
                cancel.set()
                worker.join(12)
            self.assertFalse(worker.is_alive())
        self.assertTrue(results[0]["cancelled"])
        self.assertFalse(self.project.building)
        self.assertFalse(scratch[0].exists())
        for pid in pids:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"build descendant {pid} survived Stop")


if __name__ == "__main__":
    unittest.main()
