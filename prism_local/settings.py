"""Your settings (Home → Settings): one file for every project, settings.json in the state
folder. The Home page changes them; the editors read them (which Claude account and
profile folder the agent panel may use, the GitHub owner a publish uses, how snippets
expand). Your snippets file is here too: also one for every project.

    settings.load()          # every setting, the defaults for those not set
    settings.save(changes)   # checked, then written; returns the settings now
    settings.snippets()      # {"path", "content" (None: no file yet)}
    settings.save_snippets(text)

A file that cannot be read raises registry.StateUnreadable: it is never taken for the
defaults (and written back so), and a restriction in it (claude_account) never lifted.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import registry
from fsutil import with_line_ends_of, write_bytes

# snippets: expand as you type; snippets_fraction: x/ becomes \frac{x}{} in math;
# snippets_matrix: in align, matrix, cases …, Tab puts & and Enter ends the row with \\.
SNIPPET_SWITCHES = ("snippets", "snippets_fraction", "snippets_matrix")
DEFAULTS = {"default_parent": "", "git_init": True, "github_repo": False,
            "github_owner": "", "claude_account": "", "claude_config_dir": "",
            **{k: True for k in SNIPPET_SWITCHES}}
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")      # a GitHub user or organization


def path() -> Path:
    return registry.state_dir() / "settings.json"


def load() -> dict:
    data = registry.read_state(path())
    return {k: data.get(k, v) for k, v in DEFAULTS.items()}


def save(changes: dict) -> dict:
    """Check `changes` (ValueError says what is wrong), and write them with the others.
    Under a lock: two windows saving at once keep both changes."""
    with registry.FileLock(path().with_suffix(".lock"), timeout=10):
        cur = load()
        if "default_parent" in changes:
            cur["default_parent"] = _folder(changes["default_parent"])
        for k in ("git_init", "github_repo", *SNIPPET_SWITCHES):
            if k in changes:
                cur[k] = bool(changes[k])
        if "claude_account" in changes:
            acct = str(changes["claude_account"] or "").strip()
            if acct and not EMAIL_RE.fullmatch(acct):
                raise ValueError("the Claude account must be an email address")
            cur["claude_account"] = acct
        if "claude_config_dir" in changes:
            cur["claude_config_dir"] = _folder(changes["claude_config_dir"])
        if "github_owner" in changes:
            owner = str(changes["github_owner"] or "").strip()
            if owner and not OWNER_RE.fullmatch(owner):
                raise ValueError("not a GitHub user or organization name")
            cur["github_owner"] = owner
        registry.write_state(path(), cur)
        return cur


# Your snippets (static/snippets.js): JavaScript, run by the editor page, so it lives with
# your settings and not in a project co-authors share.
def snippets_file() -> Path:
    return Path(os.environ.get("PRISM_SNIPPETS") or Path.home() / ".prism-local" / "snippets.js")


def snippets() -> dict:
    f = snippets_file()
    return {"path": str(f), "content": f.read_text(encoding="utf-8") if f.is_file() else None}


def save_snippets(content: str) -> dict:
    """Replace the whole file, with the line ends it had; every editor reads it, so it is
    never seen half-written."""
    f = snippets_file()
    write_bytes(f, with_line_ends_of(str(content or ""), f).encode("utf-8"))
    return {"path": str(f)}


def _folder(value) -> str:
    p = str(value or "").strip().strip('"')
    if p and not Path(os.path.expanduser(p)).is_dir():
        raise ValueError(f"not a folder: {p}")
    return str(Path(os.path.expanduser(p)).resolve()) if p else ""
