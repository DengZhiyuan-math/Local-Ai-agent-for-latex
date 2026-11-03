"""Research fixture with the real GitSync and a disposable local bare remote."""
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
os.environ.update(PRISM_STATE_DIR=str(work / "state"), PRISM_AUTO_UPDATE="0",
                  PRISM_AGENTS=str(work / "agents.json"))
import httpbase
import registry
from test_gitsync import Clock, git, identity

if "--state-dir" in sys.argv:
    shared_state = Path(sys.argv[sys.argv.index("--state-dir") + 1]).resolve()
    registry.state_dir = lambda: shared_state

root = work / "project"
remote = (Path(sys.argv[sys.argv.index("--received-origin") + 1]).resolve()
          if "--received-origin" in sys.argv else work / "remote.git")
if "--received-origin" in sys.argv and not (root / ".git").exists():
    git(work, "clone", "-q", str(remote), str(root))
    identity(root)

original_listen = httpbase.listen


def listen(*args, **kwargs):
    import gitsync
    import research_provenance
    import server
    from syncschedule import Schedule

    if not (root / ".git").exists():
        remote.mkdir(exist_ok=True)
        git(remote, "init", "-q", "--bare", "-b", "main")
        git(root, "init", "-q", "-b", "main")
        identity(root)
        (root / "research").mkdir(exist_ok=True)
        (root / "research/collaboration.md").write_text(
            "# Existing collaboration record\n\nTentative: compare the two arguments.\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "Initial research fixture source")
        git(root, "remote", "add", "origin", str(remote))
        git(root, "push", "-q", "-u", "origin", "main")
    clock = Clock()
    sync = server.GITSYNC
    sync.schedule = Schedule(clock)
    had_settings = (root / ".git/prism-local.json").exists()
    sync.recheck()
    sync.bind_target()
    if not had_settings:
        sync.set_enabled(False)

    original_checkpoint_replace = research_provenance.replace_file

    def checkpoint_replace(path, raw, **options):
        if Path(path).name == "state.json":
            state = json.loads(raw)
            if state.get("pending") and (work / "fail-provenance-prepare").exists():
                raise OSError("fixture provenance prepare unavailable")
            if (state.get("pending") is None and state.get("committed")
                    and (work / "fail-provenance-commit").exists()):
                raise OSError("fixture provenance commit unavailable")
        return original_checkpoint_replace(path, raw, **options)

    research_provenance.replace_file = checkpoint_replace

    def snapshot():
        files = {}
        for relative in ("main.tex", "research/knowledge.json", "research/collaboration.md"):
            path = root / relative
            if path.exists():
                raw = path.read_bytes()
                files[relative] = {"text": raw.decode("utf-8"), "sha256": hashlib.sha256(raw).hexdigest()}
        return {"receiver_head": git(root, "rev-parse", "HEAD"),
                "remote_head": git(remote, "rev-parse", "main"),
                "sync": sync.status(), "files": files, "now": clock.now,
                "root": str(root), "remote": str(remote)}

    def audit(action, before, after):
        with (work / "transport.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"action": action, "before": before, "after": after}, ensure_ascii=False) + "\n")

    def collaborator(files):
        colleague = work / "colleague"
        if not (colleague / ".git").exists():
            git(work, "clone", "-q", str(remote), str(colleague))
            identity(colleague)
        else:
            git(colleague, "fetch", "-q", "origin")
            git(colleague, "merge", "--ff-only", "origin/main")
        if not isinstance(files, dict) or not files:
            raise ValueError("Choose fixture collaborator files")
        for relative, text in files.items():
            name = Path(relative)
            if name.is_absolute() or ".." in name.parts or ".git" in name.parts:
                raise ValueError("Fixture collaborator files must stay in its project")
            path = colleague / name
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(text), encoding="utf-8")
        git(colleague, "add", "-A")
        git(colleague, "commit", "-qm", "Collaborator fixture change")
        git(colleague, "push", "-q", "origin", "main")

    original_get, original_post = server.Handler._get, server.Handler._post

    def fixture_get(handler, path, query):
        if path != "/__fixture/gitsync":
            return original_get(handler, path, query)
        if handler._foreign_project(query):
            return
        return handler._json(snapshot())

    def fixture_post(handler, path, body):
        if path != "/__fixture/gitsync":
            return original_post(handler, path, body)
        if handler._foreign_project():
            return
        if body.get("project_key") != registry.project_key(root):
            return handler._err(409, "wrong fixture project")
        before = snapshot()
        action = body.get("action")
        result = None
        if action == "tick":
            clock.advance(float(body.get("advance", gitsync.IDLE + 1)))
            sync.tick()
        elif action == "pull":
            outcome = sync.pull(manual=bool(body.get("manual", True)))
            result = {"state": outcome.state, "message": outcome.message}
        elif action == "remote_commit":
            collaborator(body.get("files"))
        elif action == "reject_upload":
            hook = remote / "hooks/pre-receive"
            if body.get("value"):
                hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                hook.chmod(0o755)
            else:
                hook.unlink(missing_ok=True)
        else:
            return handler._err(400, "unknown fixture action")
        after = snapshot()
        audit(action, before, after)
        return handler._json({**after, "outcome": result})

    server.Handler._get, server.Handler._post = fixture_get, fixture_post
    return original_listen(*args, **kwargs)


httpbase.listen = listen
runpy.run_path(str(Path(__file__).with_name("research_fixture.py")), run_name="__main__")
