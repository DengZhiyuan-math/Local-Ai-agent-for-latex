"""Run the full offline fixture suite with private state and a JSON report."""
import argparse
from contextlib import ExitStack
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("claude", "codex", "deepcode", "gh")
TEX = ("pdflatex", "xelatex", "lualatex", "latex", "tectonic", "latexmk",
       "kpsewhich", "biber", "bibtex", "makeindex", "dvipdfmx")
NATIVE_APP = ("osascript", "osacompile", "codesign", "sips")
APP_TESTS = {
    "test_macos_app.MacApp.test_browser_selection_scripts_compile_natively",
    "test_macos_app.MacApp.test_bundle_runs_launcher_with_quoted_paths",
    "test_macos_app.MacApp.test_installer_refuses_an_unrelated_existing_app",
}


def snapshot():
    paths = [p for name in ("prism_local", "tests") for p in (ROOT / name).rglob("*")
             if p.is_file() and "__pycache__" not in p.parts
             and p.suffix not in (".pyc", ".pyo") and p.name != ".DS_Store"
             and not p.name.startswith("._")]
    paths += [ROOT / name for name in ("README.md", "CONTEXT.md", "AGENTS.md")]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file()}


def flatten(suite):
    for case in suite:
        if isinstance(case, unittest.TestSuite):
            yield from flatten(case)
        else:
            yield case


def enter_context(case, context):
    """TestCase.enterContext's cleanup contract on Python 3.9 and 3.10."""
    cls = type(context)
    value = cls.__enter__(context)
    case.addCleanup(cls.__exit__, context, None, None, None)
    return value


class CIResult(unittest.TextTestResult):
    """Keep a failing case's trace even if a later case hits the job deadline."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = 0

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed += 1

    def addError(self, test, error):
        super().addError(test, error)
        self.stream.writeln(self.errors[-1][1])

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.stream.writeln(self.failures[-1][1])

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error is not None:
            self.stream.writeln(self._exc_info_to_string(error, subtest))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".ci-results")
    parser.add_argument("--modules", nargs="+", help="Run selected unittest names for diagnosis.")
    parser.add_argument("--shard", help="Run one module shard, for example 1/2.")
    parser.add_argument("--native-app-fixtures", action="store_true",
                        help="Allow temporary macOS app compile/sign/AppleScript fixtures.")
    args = parser.parse_args()
    shard_index, shard_count = 1, 1
    if args.shard:
        try:
            shard_index, shard_count = map(int, args.shard.split("/"))
            if not 1 <= shard_index <= shard_count:
                raise ValueError
        except ValueError:
            parser.error("--shard must be INDEX/COUNT with 1 <= INDEX <= COUNT")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Keep native fixture/cleanup stalls diagnosable before the job deadline.
        faulthandler.dump_traceback_later(600, repeat=True)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    before = snapshot()
    started = time.monotonic()
    original_which = shutil.which
    names = PROVIDERS + TEX + ("open",)
    if not args.native_app_fixtures:
        names += NATIVE_APP
    restricted = {os.path.normcase(str(Path(found).resolve()))
                  for name in names if (found := original_which(name))}
    blocked_processes, blocked_connections = [], []

    def safe_which(name, *positional, **keywords):
        found = original_which(name, *positional, **keywords)
        if found and os.path.normcase(str(Path(found).resolve())) in restricted:
            return None
        return found

    def audit(event, values):
        if event == "socket.connect":
            address = values[1]
            if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "localhost", "::1"):
                blocked_connections.append(str(address[0]))
                raise AssertionError("External connections are forbidden in fixture tests.")
        elif event == "subprocess.Popen":
            executable, command, cwd, environment = values
            environment = environment or os.environ
            if isinstance(command, (list, tuple)):
                parts = list(command)
            elif isinstance(command, str):
                try:
                    parts = shlex.split(command, posix=False)
                except ValueError:
                    parts = [command]
            else:
                parts = [command]
            for item in [executable, *parts]:
                if not isinstance(item, (str, bytes, os.PathLike)):
                    continue
                item = os.fsdecode(item).strip('"')
                if len(item) > 4096 or "\n" in item:
                    continue
                try:
                    found = original_which(item, path=environment.get("PATH"))
                    resolved = os.path.normcase(str(Path(found).resolve())) if found else ""
                except (OSError, ValueError):
                    continue
                if resolved in restricted:
                    blocked_processes.append(Path(item).name)
                    raise AssertionError("Real provider/TeX/application command forbidden: " + Path(item).name)

    sys.addaudithook(audit)
    if not hasattr(unittest.TestCase, "enterContext"):
        unittest.TestCase.enterContext = enter_context
    sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "prism_local")]
    with tempfile.TemporaryDirectory(prefix="prism-ci-") as directory:
        work = Path(directory).resolve()
        for name in ("state", "config", "data", "xdg-state", "cache", "home", "trash"):
            (work / name).mkdir()
        (work / "agents.json").write_text("{}\n", encoding="utf-8")
        (work / "gitconfig").write_text("", encoding="utf-8")
        clean = dict(os.environ)
        for name in list(clean):
            if (name.startswith(("OPENAI_", "ANTHROPIC_", "DEEPCODE_", "DEEPSEEK_", "CODEX_", "CLAUDE_",
                                 "AZURE_OPENAI_", "GH_", "GITHUB_", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
                    or name in ("PRISM_NATIVE_CODEX", "PYTHONHOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                "http_proxy", "https_proxy", "all_proxy", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_SSH_COMMAND",
                                "GIT_CONFIG_PARAMETERS")):
                clean.pop(name, None)
        clean.update(PRISM_STATE_DIR=str(work / "state"), PRISM_AGENTS=str(work / "agents.json"),
                     PRISM_SNIPPETS=str(work / "snippets.js"), PRISM_TRASH_DIR=str(work / "trash"),
                     PRISM_CLIPBOARD_FILE=str(work / "clipboard.json"), PRISM_AUTO_UPDATE="0", PRISM_SKIP_TEX="1",
                     XDG_CONFIG_HOME=str(work / "config"), XDG_DATA_HOME=str(work / "data"),
                     XDG_STATE_HOME=str(work / "xdg-state"), XDG_CACHE_HOME=str(work / "cache"),
                     GIT_CONFIG_GLOBAL=str(work / "gitconfig"), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_COUNT="0",
                     GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file", PYTHONDONTWRITEBYTECODE="1",
                     PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
                     NO_PROXY="127.0.0.1,localhost,::1", no_proxy="127.0.0.1,localhost,::1")
        original_home, original_expanduser = Path.home, os.path.expanduser
        homes = {name: os.environ.get(name) for name in ("HOME", "USERPROFILE")}

        def changed_home():
            return any(os.environ.get(name) and os.environ.get(name) != value for name, value in homes.items())

        def isolated_home(cls):
            return original_home() if changed_home() else work / "home"

        def isolated_expanduser(path):
            if isinstance(path, str) and (path == "~" or path.startswith("~/")) and not changed_home():
                return str(work / "home") + path[1:]
            return original_expanduser(path)

        launch = subprocess.Popen

        def local_git_process(command, *positional, **keywords):
            parts = list(command) if isinstance(command, (list, tuple)) else []
            if parts and Path(str(parts[0])).stem == "git" and any(str(p).startswith("http://127.0.0.1:") for p in parts):
                keywords["env"] = {**(keywords.get("env") or os.environ), "GIT_ALLOW_PROTOCOL": "file:http"}
            return launch(command, *positional, **keywords)

        with ExitStack() as patches:
            for patch in (mock.patch.dict(os.environ, clean, clear=True),
                          mock.patch.object(Path, "home", classmethod(isolated_home)),
                          mock.patch.object(os.path, "expanduser", side_effect=isolated_expanduser),
                          mock.patch.object(shutil, "which", side_effect=safe_which),
                          mock.patch.object(subprocess, "Popen", side_effect=local_git_process)):
                patches.enter_context(patch)
            import github
            patches.enter_context(mock.patch.object(github.GITHUB, "status", return_value={
                "gh": None, "logged_in": False, "account": None, "error": "Offline CI fixture."}))
            loader = unittest.TestLoader()
            suite = (loader.loadTestsFromNames(args.modules) if args.modules
                     else loader.discover(str(ROOT / "tests"), pattern="test*.py"))
            cases = list(flatten(suite))
            discovered_count = len(cases)
            if shard_count > 1:
                modules = sorted({type(case).__module__ for case in cases})
                selected = set(modules[shard_index - 1::shard_count])
                cases = [case for case in cases if type(case).__module__ in selected]
                if not cases:
                    parser.error("--shard selected no tests")
                suite = unittest.TestSuite(cases)
            if not args.native_app_fixtures:
                for case in cases:
                    if case.id() in APP_TESTS:
                        method = getattr(type(case), case._testMethodName)
                        setattr(type(case), case._testMethodName,
                                unittest.skip("Temporary native app fixtures require --native-app-fixtures.")(method))
            result = unittest.TextTestRunner(verbosity=2, resultclass=CIResult).run(suite)
    after = snapshot()
    drift = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    report = {"tests": result.testsRun, "passed": result.passed,
              "seconds": round(time.monotonic() - started, 3), "python": platform.python_version(), "platform": sys.platform,
              "failures": [{"test": case.id(), "traceback": detail} for case, detail in result.failures],
              "errors": [{"test": case.id(), "traceback": detail} for case, detail in result.errors],
              "skipped": [{"test": case.id(), "reason": reason} for case, reason in result.skipped],
              "discovered_tests": [case.id() for case in cases], "source_drift": drift,
              "shard": f"{shard_index}/{shard_count}", "total_discovered_tests": discovered_count,
              "blocked_processes": blocked_processes, "blocked_connections": blocked_connections,
              "native_app_fixtures": args.native_app_fixtures,
              "isolation": "Private PRISM/XDG/home defaults; local-only Git/HTTP fixtures; Python audit hooks. Not an OS sandbox for native descendants."}
    success = result.wasSuccessful() and not drift and not blocked_processes and not blocked_connections
    report["successful"] = success
    (output / "results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("tests", "passed", "seconds", "python", "platform", "successful")}))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
