"""Your settings (Home → Settings): one file for every project, settings.json in the state
folder. The Home page changes them; the editors read them (which Claude account and
profile folder the agent panel may use, the GitHub owner a publish uses).

    settings.load()          # every setting, the defaults for those not set
    settings.save(changes)   # checked, then written; returns the settings now

A file that cannot be read raises registry.StateUnreadable: it is never taken for the
defaults (and written back so), and a restriction in it (claude_account) never lifted.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import registry

DEFAULTS = {"default_parent": "", "git_init": True, "github_repo": False,
            "github_owner": "", "claude_account": "", "claude_config_dir": ""}
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
        for k in ("git_init", "github_repo"):
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
        registry.write_json(path(), cur)
        return cur


def _folder(value) -> str:
    p = str(value or "").strip().strip('"')
    if p and not Path(os.path.expanduser(p)).is_dir():
        raise ValueError(f"not a folder: {p}")
    return str(Path(os.path.expanduser(p)).resolve()) if p else ""
