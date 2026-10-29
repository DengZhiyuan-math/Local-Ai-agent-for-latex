"""One project: real API/CLI adapters, GitSync, and an offline GitHub adapter.

The existing fake programs are extracted as data, not imported as servers. The
GitHub fixture retains its lifecycle/failure controls. Only Git transport sees
the existing FakeGitHub local-bare rewrite; target discovery sees its real URL.
"""
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys


work = Path(sys.argv[1]).resolve()
work.mkdir(parents=True, exist_ok=True)
repo = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(repo / "prism_local"), str(repo / "tests")]
from research_integration_helpers import cli_profiles

task_home, profiles, cli_configs = cli_profiles(work)
issue_origin = work / "received-github-remote"
if "--remote-file" in sys.argv:
    issue_origin.write_text(str(Path(sys.argv[sys.argv.index("--remote-file") + 1]).resolve()))
elif issue_origin.exists():
    sys.argv += ["--remote-file", issue_origin.read_text()]
Path.home = classmethod(lambda cls: task_home)
os.environ.update(PRISM_STATE_DIR=str(work / "state"), PRISM_AUTO_UPDATE="0",
                  PRISM_AGENTS=str(work / "agents.json"), DEEPCODE_API_KEY="fixture-only",
                  CLAUDE_CONFIG_DIR=str(profiles["claude"]),
                  PRISM_STARTED_CLAUDE_CONFIG_DIR=str(profiles["claude"]),
                  CODEX_HOME=str(profiles["codex"]), GIT_CONFIG_NOSYSTEM="1",
                  GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")

import gitenv
import github
import httpbase
from fake_github import FakeGitHub
from test_gitsync import Clock, git, identity

root = work / "project"
origin_file = work / "received-origin"
if "--received-origin" in sys.argv:
    remote = Path(sys.argv[sys.argv.index("--received-origin") + 1]).resolve()
    origin_file.write_text(str(remote))
else:
    remote = Path(origin_file.read_text()) if origin_file.exists() else work / "remote.git"
if not remote.exists():
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
if origin_file.exists() and not (root / ".git").exists():
    git(work, "clone", "-q", str(remote), str(root))
    git(root, "remote", "set-url", "origin", "https://github.com/fixture/paper.git")
transport = FakeGitHub(account="author")
transport.repositories["fixture/paper"] = remote


def local_transport(cwd, *args, **kwargs):
    if args and args[0] in ("fetch", "push", "ls-remote", "clone"):
        configured = transport._git(cwd, "remote", "get-url", "origin")
        if configured.returncode or configured.stdout.strip() != "https://github.com/fixture/paper.git":
            raise AssertionError("The integration fixture refuses an unbound external Git transport")
    return transport.git(cwd, *args, **kwargs)


gitenv.git, github.GITHUB = local_transport, transport
saved_settings = root / ".git/prism-local.json"
enabled = bool(json.loads(saved_settings.read_text()).get("sync")) if saved_settings.exists() else False
original_listen = httpbase.listen


def listen(*args, **kwargs):
    import server
    from backend_claude import ClaudeCode
    from backend_codex import Codex
    from backend_deepcode import DeepCode
    from backends import Backend
    from syncschedule import Schedule

    identity(root)
    if transport.git(root, "rev-parse", "--verify", "HEAD").returncode:
        git(root, "add", "-A")
        git(root, "commit", "-qm", "Initial integrated research fixture")
        transport.git(root, "push", "-q", "-u", "origin", "main", check=True)
    sync, clock = server.GITSYNC, Clock()
    sync.schedule = Schedule(clock)
    sync.recheck()
    sync.bind_target()
    sync.set_enabled(enabled)

    class FixtureClaude(ClaudeCode):
        model_catalog = Backend.model_catalog

    for name, cls in (("claude", FixtureClaude), ("codex", Codex), ("deepcode", DeepCode)):
        server.AGENT.backends[name] = cls(name, cli_configs[name])
    config = json.loads((work / "agents.json").read_text())
    config["providers"].update(cli_configs)
    (work / "agents.json").write_text(json.dumps(config))
    legacy_file = work / "legacy-session"
    if not legacy_file.exists():
        legacy_file.write_text("fixture-legacy-integration")
    legacy = legacy_file.read_text()
    native = profiles["codex"] / "sessions" / ("rollout-fixture-" + legacy + ".jsonl")
    if not native.exists():
        native.parent.mkdir(exist_ok=True)
        native.write_text(json.dumps({"type": "session_meta", "payload": {"id": legacy, "cwd": str(root)}}) + "\n")

    def snapshot():
        files = {}
        for relative in ("main.tex", "research/knowledge.json"):
            path = root / relative
            if path.exists():
                raw = path.read_bytes()
                files[relative] = {"text": raw.decode(), "sha256": hashlib.sha256(raw).hexdigest()}
        return {"receiver_head": git(root, "rev-parse", "HEAD"), "remote_head": git(remote, "rev-parse", "main"),
                "sync": sync.status(), "files": files, "profiles": {name: str(path) for name, path in profiles.items()},
                "working_tree": git(root, "status", "--porcelain"),
                "legacy_session": legacy, "root": str(root), "remote": str(remote)}

    def collaborator(files):
        peer = work / "colleague"
        if not (peer / ".git").exists():
            git(work, "clone", "-q", str(remote), str(peer))
            identity(peer)
        else:
            git(peer, "fetch", "-q", "origin")
            git(peer, "merge", "--ff-only", "origin/main")
        if not isinstance(files, dict) or not files:
            raise ValueError("Choose fixture files")
        for relative, content in files.items():
            name = Path(relative)
            if name.is_absolute() or ".git" in name.parts or ".." in name.parts:
                raise ValueError("Fixture files must stay in the peer project")
            path = peer / name
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(content))
        git(peer, "add", "-A")
        git(peer, "commit", "-qm", "Integration collaborator change")
        git(peer, "push", "-q", "origin", "main")

    previous_get, previous_post = server.Handler._get, server.Handler._post

    def get(handler, path, query):
        if path != "/__fixture/integration":
            return previous_get(handler, path, query)
        if handler._foreign_project(query):
            return
        return handler._json(snapshot())

    def post(handler, path, body):
        if path != "/__fixture/integration":
            return previous_post(handler, path, body)
        if handler._foreign_project():
            return
        if body.get("project_key") != server.registry.project_key(root):
            return handler._err(409, "wrong fixture project")
        before = snapshot()
        action = body.get("action")
        outcome = None
        if action == "tick":
            clock.advance(float(body.get("advance", 31)))
            sync.tick()
        elif action == "pull":
            result = sync.pull(manual=True)
            outcome = {"state": result.state, "message": result.message}
        elif action == "remote_commit":
            collaborator(body.get("files"))
        elif action == "reject_upload":
            hook = remote / "hooks/pre-receive"
            if body.get("value"):
                hook.write_text("#!/bin/sh\nexit 1\n")
                hook.chmod(0o755)
            else:
                hook.unlink(missing_ok=True)
        elif action == "script":
            provider = body.get("provider", "fixture")
            path = work / "api-script.json" if provider.startswith("fixture") else profiles[provider] / "cli-script.json"
            path.write_text(json.dumps(body.get("script", [])))
        else:
            return handler._err(400, "unknown integration action")
        after = snapshot()
        with (work / "integration-transport.jsonl").open("a") as stream:
            stream.write(json.dumps({"action": action, "before": before, "after": after}) + "\n")
        return handler._json({**after, "outcome": outcome})

    server.Handler._get, server.Handler._post = get, post
    return original_listen(*args, **kwargs)


httpbase.listen = listen
runpy.run_path(str(Path(__file__).with_name("github_topics_fixture.py")), run_name="__main__")
