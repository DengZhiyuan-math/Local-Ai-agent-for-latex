"""Offline GitHub-discussion extension of the existing research fixture.

The test routes drive one deterministic worker tick. They are fixture-only and
never configure an alternate product API endpoint or use an account credential.
"""
import copy
import json
import os
from pathlib import Path
import runpy
import sys
import threading


work = Path(sys.argv[1]).resolve()
work.mkdir(parents=True, exist_ok=True)
os.environ.update(PRISM_STATE_DIR=str(work / "state"), PRISM_AUTO_UPDATE="0",
                  PRISM_AGENTS=str(work / "agents.json"))
repo = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(repo / "prism_local"), str(repo / "tests")]
import httpbase

original_listen = httpbase.listen


def listen(*args, **kwargs):
    import gitenv
    import server
    import synctarget

    root = server.PROJECT.root
    if not (root / ".git").exists():
        gitenv.git(root, "init", "-q", "-b", "main", check=True)
        gitenv.git(root, "remote", "add", "origin", "https://github.com/fixture/paper.git", check=True)
    server.GITSYNC.recheck()
    target = synctarget.resolve(server.GITSYNC.repo, fresh=True)
    if not server.GITSYNC.settings.read().get("sync_target"):
        server.GITSYNC.settings.bind(target)
    server.GITSYNC.settings.update(sync=False)

    from fake_github_issues import FakeIssueAdapter
    from github_issues import IssueError
    from research_github import GitHubDiscussions

    from fsutil import replace_file
    from research import ResearchKnowledge
    import research
    import research_github

    remote_file = (Path(sys.argv[sys.argv.index("--remote-file") + 1]).resolve()
                   if "--remote-file" in sys.argv else work / "github-remote.json")

    class Remote(FakeIssueAdapter):
        def __init__(self):
            super().__init__()
            self.lock = threading.RLock()
            self.block_method = None
            self.blocked = threading.Event()
            self.release = threading.Event()
            self._load()

        def _load(self):
            if not remote_file.exists():
                return
            state = json.loads(remote_file.read_text(encoding="utf-8"))
            self.repo = state["repo"]
            self.issue_rows = state["issues"]
            self.comment_rows = {int(key): value for key, value in state["comments"].items()}
            self.calls = state["calls"]
            self.offline = state.get("offline", False)
            self.unknown_after_apply = set(state.get("unknown_after_apply", []))
            self.overrides = state.get("overrides", {})
            self.users = state.get("users", getattr(self, "users", {}))
            self.next_error = {name: IssueError(error["message"], uncertain=error["uncertain"])
                               for name, error in state.get("next_error", {}).items()}

        def _save(self):
            replace_file(remote_file, (json.dumps(self.snapshot(), ensure_ascii=False) + "\n").encode("utf-8"), durable=True)

        def snapshot(self):
            return copy.deepcopy({"repo": self.repo, "issues": self.issue_rows,
                "comments": self.comment_rows, "calls": self.calls, "offline": self.offline,
                "users": getattr(self, "users", {}),
                "unknown_after_apply": sorted(self.unknown_after_apply), "overrides": self.overrides,
                "next_error": {name: {"message": str(error), "uncertain": error.uncertain}
                               for name, error in self.next_error.items()}})

        def invoke(self, name, *values):
            if self.block_method == name:
                self.block_method = None
                self.blocked.set()
                if not self.release.wait(10):
                    raise IssueError("fixture blocked request expired")
            with self.lock:
                self._load()
                try:
                    return getattr(super(), name)(*values)
                finally:
                    self._save()

    for method in ("repository", "user", "list_issues", "get_issue", "list_comments", "create_issue", "create_comment"):
        def invoke(self, *values, _name=method):
            return self.invoke(_name, *values)
        setattr(Remote, method, invoke)

    remote = Remote()
    now = [100.0]
    previous = getattr(server, "DISCUSSIONS", None)
    if previous is not None:
        previous.close()
    server.DISCUSSIONS = GitHubDiscussions(ResearchKnowledge(server.PROJECT), adapter=remote,
        target=server.GITSYNC.status, is_open=lambda: server.PRESENCE.count() > 0,
        clock=lambda: now[0], interval=30)
    ticks = [0]
    tick_condition = threading.Condition()
    original_tick = server.DISCUSSIONS.tick

    def tick():
        try:
            return original_tick()
        finally:
            with tick_condition:
                ticks[0] += 1
                tick_condition.notify_all()

    server.DISCUSSIONS.tick = tick
    automatic = "--automatic" in sys.argv
    if automatic:
        server.DISCUSSIONS.start()

    topic_replace = research_github.replace_file
    research_replace = research.replace_file

    def fail_progress(path, raw, **options):
        if (work / "fail-github-progress").exists():
            raise OSError("fixture issue progress persistence unavailable")
        if (work / "fail-github-reply-ack").exists():
            state = json.loads(raw)
            if any(row.get("proposal_replies") for repository in state.get("repositories", {}).values()
                   for row in repository.get("topics", {}).values()):
                raise OSError("fixture GitHub reply acknowledgement unavailable")
        if (work / "fail-github-progress-after-apply").exists():
            state = json.loads(raw)
            if any(row.get("published_revision") for repository in state.get("repositories", {}).values()
                   for row in repository.get("topics", {}).values()):
                raise OSError("fixture confirmed issue progress persistence unavailable")
        return topic_replace(path, raw, **options)

    def fail_mirror(path, raw, **options):
        if (work / "fail-github-mirror").exists() and Path(path).name == "knowledge.json":
            raise OSError("fixture issue mirror persistence unavailable")
        return research_replace(path, raw, **options)

    research_github.replace_file = fail_progress
    research.replace_file = fail_mirror
    original_post, original_get = server.Handler._post, server.Handler._get

    def fixture_get(handler, path, query):
        if path != "/__fixture/github":
            return original_get(handler, path, query)
        if handler._foreign_project(query):
            return
        with remote.lock:
            remote._load()
            state = remote.snapshot()
        state.update(blocked=remote.blocked.is_set(), pages=server.PRESENCE.count(), now=now[0],
                     ticks=ticks[0], automatic=automatic)
        return handler._json(state)

    def fixture_post(handler, path, body):
        if path != "/__fixture/github":
            return original_post(handler, path, body)
        if handler._foreign_project():
            return
        if body.get("project_key") != server.registry.project_key(root):
            return handler._err(409, "wrong fixture project")
        action = body.get("action")
        if action == "tick":
            now[0] += float(body.get("advance", 31))
            server.DISCUSSIONS.tick()
        elif action == "wake":
            if not automatic:
                return handler._err(400, "automatic worker was not started")
            with tick_condition:
                before = ticks[0]
                now[0] += float(body.get("advance", 31))
                server.DISCUSSIONS.wake()
                if not tick_condition.wait_for(lambda: ticks[0] > before, timeout=3):
                    return handler._err(500, "automatic worker did not tick")
        elif action == "release":
            remote.release.set()
        elif action == "block":
            remote.release.clear(); remote.blocked.clear()
            remote.block_method = str(body["method"])
        elif action == "target":
            gitenv.git(root, "remote", "set-url", "origin", str(body["url"]), check=True)
        else:
            with remote.lock:
                remote._load()
                if action == "offline":
                    remote.offline = bool(body["value"])
                elif action == "unknown":
                    remote.unknown_after_apply.add(str(body["method"]))
                elif action == "error":
                    remote.next_error[str(body["method"])] = IssueError(str(body.get("message", "fixture outage")),
                                                                      uncertain=bool(body.get("uncertain")))
                elif action == "override":
                    remote.overrides[str(body["method"])] = body["value"]
                elif action == "repository":
                    remote.repo.update(body["value"])
                elif action == "user":
                    if not hasattr(remote, "users"):
                        remote.users = {}
                    remote.users[str(body["login"]).casefold()] = body["row"]
                elif action == "comment":
                    number = int(body.get("issue", 1))
                    rows = remote.comment_rows.setdefault(number, [])
                    identity = int(body.get("id", 6000 + len(rows)))
                    full = remote.repo["full_name"]
                    row = {"id": identity, "body": str(body["body"]),
                           "user": body.get("user", {"id": 42, "login": "author", "type": "User"}),
                           "issue_url": f"https://api.github.com/repos/{full}/issues/{number}",
                           "html_url": f"https://github.com/{full}/issues/{number}#issuecomment-{identity}",
                           "created_at": body.get("created_at", "2026-10-29T12:00:00Z"),
                           "updated_at": body.get("updated_at", "2026-10-29T12:00:00Z")}
                    rows[:] = [old for old in rows if old["id"] != identity] + [row]
                else:
                    return handler._err(400, "unknown fixture action")
                remote._save()
        return handler._json({"ok": True})

    server.Handler._get, server.Handler._post = fixture_get, fixture_post
    srv = original_listen(*args, **kwargs)
    original_close = srv.server_close

    def close():
        server.DISCUSSIONS.close()
        return original_close()

    srv.server_close = close
    return srv


httpbase.listen = listen
runpy.run_path(str(Path(__file__).with_name("research_topics_fixture.py")), run_name="__main__")
