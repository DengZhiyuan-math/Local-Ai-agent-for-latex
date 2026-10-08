"""Git target diagnostics using temporary repositories and dry-run pushes only.

No AI, external network, user Git configuration, or remote ref update is used.
Run: python3 docs/audits/2026-10-08-git-target/probe.py
Use --assert-display-matches-push to fail on a demonstrated UI/target mismatch.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "prism_local"))
import backends
import gitsync


def main():
    results = {}
    with tempfile.TemporaryDirectory(prefix="prism-git-target-") as tmp:
        base = Path(tmp).resolve()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(base / "empty-config"),
                   GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0")
        with mock.patch.dict(os.environ, env, clear=True):
            def git(root, *args, env_override=None):
                p = subprocess.run(["git", *args], cwd=root, env=env_override,
                                   capture_output=True, text=True, timeout=20)
                if p.returncode:
                    raise RuntimeError(p.stderr)
                return p.stdout.strip()

            tool_remote = base / "latex-ai-agent.git"
            paper_remote = base / "paper.git"
            for remote in (tool_remote, paper_remote):
                git(base, "init", "-q", "--bare", str(remote))
            tool = base / "latex-ai-agent"
            tool.mkdir()
            git(tool, "init", "-q", "-b", "main")
            git(tool, "remote", "add", "origin", str(tool_remote))
            nested = tool / "papers" / "paper"
            nested.mkdir(parents=True)
            sync = gitsync.GitSync(lambda: nested, lambda: "build")
            results["nested_paper_without_own_git"] = {
                "git_top_is_tool": Path(git(nested, "rev-parse", "--show-toplevel")) == tool,
                "agent_sees_tool_origin": git(nested, "remote", "get-url", "origin",
                                               env_override=backends.agent_env()) == str(tool_remote),
                "editor_auto_sync_enabled": sync.check_repo(),
            }

            paper = base / "paper"
            paper.mkdir()
            git(paper, "init", "-q", "-b", "main")
            for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid"),
                               ("commit.gpgsign", "false"), ("push.default", "current")):
                git(paper, "config", key, value)
            (paper / "main.tex").write_text("fixture\n")
            git(paper, "add", "main.tex")
            git(paper, "commit", "-q", "-m", "fixture base")
            git(paper, "remote", "add", "origin", str(paper_remote))
            git(paper, "remote", "add", "tool", str(tool_remote))
            git(paper, "config", "branch.main.remote", "origin")
            git(paper, "config", "branch.main.merge", "refs/heads/main")
            git(paper, "update-ref", "refs/remotes/origin/main", "HEAD")
            (paper / "main.tex").write_text("fixture changed\n")
            git(paper, "commit", "-q", "-am", "fixture paper edit")

            class DryRunSync(gitsync.GitSync):
                def git(self, *args, **kwargs):
                    if args[0] == "push":
                        self.requested_push = list(args)
                        args = ("push", "--dry-run", "--porcelain",
                                *(a for a in args[1:] if a != "-q"))
                        p = super().git(*args, **kwargs)
                        self.push_output = p.stdout + p.stderr
                        return p
                    return super().git(*args, **kwargs)

            for setting in ("branch.main.pushRemote", "remote.origin.pushurl"):
                git(paper, "config", setting, "tool" if setting.endswith("pushRemote")
                    else str(tool_remote))
                sync = DryRunSync(lambda: paper, lambda: "build")
                assert sync.check_repo()
                sync.push(tries=1)
                results[setting] = {
                    "origin_url_is_paper": git(paper, "remote", "get-url", "origin") == str(paper_remote),
                    "upstream": sync.upstream,
                    "production_push_argv": sync.requested_push,
                    "dry_run_output": sync.push_output.replace(str(base), "<fixture>"),
                    "dry_run_selects_tool": f"To {tool_remote}" in sync.push_output,
                    "reported_error": sync.error,
                }
                git(paper, "config", "--unset", setting)

            redirected = backends.agent_env({**env, "GIT_DIR": str(tool / ".git"),
                                               "GIT_WORK_TREE": str(tool)})
            results["inherited_git_environment"] = {
                "cwd_is_paper": True,
                "agent_env_preserves_git_dir": "GIT_DIR" in redirected,
                "agent_sees_tool_origin": git(paper, "remote", "get-url", "origin",
                                               env_override=redirected) == str(tool_remote),
            }
            results["remote_refs_unchanged"] = all(not git(r, "for-each-ref")
                                                   for r in (paper_remote, tool_remote))
    print(json.dumps(results, indent=2))
    if "--assert-display-matches-push" in sys.argv:
        for setting in ("branch.main.pushRemote", "remote.origin.pushurl"):
            assert not results[setting]["dry_run_selects_tool"], (
                f"{setting}: editor origin is paper, but GitSync push selects tool")


if __name__ == "__main__":
    main()
