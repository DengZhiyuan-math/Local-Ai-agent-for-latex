"""Agent sessions for prism-local.

A chat turn runs on one of several AI backends (backends.py): the Claude Code CLI,
the Codex CLI, or an OpenAI-compatible API such as DeepSeek. This module does the
part that is the same for all of them: it checks the request, states the turn's
file scope, keeps the project's files before and after the turn (snapshot.py), reports
per-file diffs afterwards and can undo the turn.
"""
from __future__ import annotations

import itertools
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

from backend_claude import claude_bin  # noqa: F401 — re-exported
from backends import NO_WINDOW, SYSTEM_APPEND, Backend, Job, kill_tree, load_backends  # noqa: F401
from fsutil import project_path
import registry
import snapshot

# A model alias or id such as "opus", "sonnet[1m]", "deepseek-chat" or
# "deepseek/deepseek-chat" (OpenRouter). Never a flag.
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,119}")
MAX_TURNS = 50          # turns kept in memory for their events and Undo
UNDO_WAIT = 10          # seconds an Undo waits for git to finish with the files (a slow hook) before saying so


class AgentManager:
    def __init__(self, root_fn: Callable[[], Path], files_fn: Callable[[], list[str]],
                 writable_fn: Callable[[str], object] | None = None,
                 backends: tuple[dict[str, Backend], str, str | None] | None = None):
        self.root_fn = root_fn        # current repository root
        self.files_fn = files_fn      # editable files (repo-relative)
        # Raises ValueError for a path an agent may not write (server.resolve).
        self.writable_fn = writable_fn
        self.backends, self.default, self.config_error = backends or load_backends()
        self.jobs: dict[int, Job] = {}
        self.turns: dict[int, Job] = {}
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.active: Job | None = None
        self.server_url: str | None = None    # the editor server, for the compile tool
        # Called before a turn's snapshot, and with the files a turn (or its Undo) changed:
        # the server commits them to the project's repository (gitsync.py).
        self.on_start: Callable[[], None] = lambda: None
        self.on_changed: Callable[[list[str], str], None] = lambda paths, why: None
        # Held while Undo writes the files: the editor's save lock (repository.FilesLock), which a
        # sync's commit and merge hold too, so a merge never undoes the Undo.
        self.writes = threading.RLock()

    def backend(self, provider: str | None) -> Backend | None:
        return self.backends.get(provider or self.default)

    def info(self) -> dict:
        root = self.root_fn().resolve()
        return {"default": self.default, "config_error": self.config_error,
                "project_key": registry.project_key(root), "root": str(root),
                "providers": [b.info() for b in self.backends.values()]}

    def _snapshot(self, job: Job) -> dict:
        """The project's files now (snapshot.take); what could not be looked at is noted."""
        files, issues = snapshot.take(job.root)
        job.snapshot_issues |= issues
        return files

    def _writable(self, job: Job, rel: str) -> bool:
        if job.mode != "edit":
            return False
        if job.scope:
            return rel in job.scope
        if self.writable_fn is None:
            return True
        try:
            self.writable_fn(rel)
            return True
        except ValueError:
            return False

    # ------------------------------------------------------------ run
    def start(self, prompt: str, session_id: str | None, mode: str,
              model: str | None = None, effort: str | None = None,
              scope: list[str] | None = None, provider: str | None = None,
              attachments: list[str] | None = None) -> dict:
        """Run one turn. `scope` (project-relative files) limits which files the agent may
        change in edit mode; None lets it change any file and create new ones."""
        backend = self.backend(provider)
        if backend is None:
            return {"error": f"Unknown provider: {provider}"}
        why = backend.unavailable()
        if why:
            return {"error": why}
        model = model or backend.default_model
        if model and not MODEL_RE.fullmatch(model):
            return {"error": f"Not a model name: {model}"}
        root = self.root_fn().resolve()
        bad = backend.check(model, effort) or backend.check_attachments(root, attachments or []) \
            or backend.preflight(root) or (backend.check_session(root, session_id) if session_id else None)
        if bad:
            return {"error": bad}
        if scope:
            try:
                scope = sorted({project_path(root, rel).relative_to(root).as_posix() for rel in scope})
            except (ValueError, OSError, RuntimeError) as e:
                return {"error": f"Invalid scope: {e}"}
        with self.lock:
            if self.active and not self.active.done:
                return {"error": "The agent is still working on the previous message."}
            job = Job(next(self.ids))
            # Set before the turn counts as running: editing() and server_wrote() read them.
            job.mode, job.root = mode if mode in ("edit", "ask") else "ask", root
            self.jobs[job.id] = job
            self.active = job
        job.scope = scope if mode == "edit" and scope else None
        if job.scope:
            note = ("You may change ONLY these files in this turn: " + ", ".join(job.scope)
                    + ". Do not edit or create any other file; if the request needs that, "
                    "say so instead.")
        elif mode == "edit":
            note = ("You may change any file in the project and create new files in this "
                    "turn. File restrictions from earlier turns no longer apply.")
        else:
            note = "Ask mode: do not create, change or delete files, and do not compile. Earlier Edit permissions no longer apply."
        # Each turn states its own scope at the end of the message. A resumed conversation
        # remembers earlier turns, and a note there outweighs one in the system prompt.
        # (Appended, not prepended: a /command must stay first.)
        if note:
            prompt = f"{prompt}\n\n[Scope for this turn] {note}"
        job.provider, job.prompt, job.session_id = backend.id, prompt, session_id
        job.mode, job.model, job.effort = mode if mode in ("edit", "ask") else "ask", model, effort
        job.root, job.files = self.root_fn().resolve(), self.files_fn
        job.server_url = self.server_url
        job.attachments = attachments or []
        print(json.dumps({"event": "agent_start", "job": job.id, "provider": job.provider,
                          "project_key": registry.project_key(root), "cwd": str(root),
                          "session_id": session_id}), flush=True)
        job.writable = lambda rel: self._writable(job, rel)
        try:
            self.on_start()                      # your edits so far, committed apart
        except Exception:  # noqa: BLE001 — a failed commit must not stop the turn
            pass
        try:
            job.before = self._snapshot(job)
            threading.Thread(target=self._run, args=(job, backend), daemon=True).start()
        except Exception as e:  # noqa: BLE001 — a turn that cannot start must not stay "running"
            with job.cond:
                job.done = True
                job.cond.notify_all()
            return {"error": f"The turn could not start: {type(e).__name__}: {e}"}
        return {"job": job.id, "provider": backend.id, "model": job.model}

    def _run(self, job: Job, backend: Backend) -> None:
        t0, res = time.time(), {}
        try:
            res = backend.run(job) or {}
        except Exception as e:  # noqa: BLE001 — report anything to the UI
            job.emit({"t": "error", "message": f"{type(e).__name__}: {e}"})
            res = {"is_error": True}
        try:
            self._finish(job, backend, res, t0)
        except Exception as e:  # noqa: BLE001 — the turn ends whatever happens
            job.emit({"t": "error", "message": f"{type(e).__name__}: {e}"})
            job.emit({"t": "done", "turn": job.id, "provider": job.provider, "changed": [],
                      "is_error": True, "session_id": res.get("session_id") or job.session_id})
        finally:
            with job.cond:
                job.done = True
                job.cond.notify_all()

    def _finish(self, job: Job, backend: Backend, res: dict, t0: float) -> None:
        """After the backend: what the turn changed, reverted where it may not, reported."""
        time.sleep(0.2)
        # What the editor wrote meanwhile (your saves, uploads, builds) is not the turn's:
        # taken as it was then. The files are held while looked at and reverted (a sync's
        # merge holds them too); a hold that cannot be had in time does not stop the turn.
        held = self.writes.acquire(timeout=UNDO_WAIT)
        try:
            job.after = self._snapshot(job)
            with self.lock:
                job.sealed = True
                snapshot.rebase(job.before, job.rebased, job.rebased_folders)
            # Ask is read-only: a backend without a read-only mode may change nothing.
            read_only = job.mode == "ask" and not backend.read_only_ask
            out_of_scope = [rel for rel in snapshot.changed_paths(job.before, job.after)
                            if read_only or (job.scope and rel not in job.scope)]
            reverted = []
            if out_of_scope and (read_only or not backend.enforces_scope):
                # This backend cannot be kept to the @-mentioned files, so undo what it
                # wrote outside them (only where nothing else changed the file since).
                reverted = snapshot.restore(job.root, job.before, job.after, out_of_scope)
                job.after = self._snapshot(job)
        finally:
            if held:
                self.writes.release()
        changed = snapshot.changes(job.before, job.after)
        # Undo needs only the files this turn changed: keep just those in memory.
        keep = [c["path"] for c in changed]
        job.before = {r: job.before.get(r) for r in keep}
        job.after = {r: job.after.get(r) for r in keep}
        self.turns[job.id] = job
        with self.lock:
            while len(self.turns) > MAX_TURNS:
                self.turns.pop(next(iter(self.turns)))
            while len(self.jobs) > MAX_TURNS and next(iter(self.jobs)) != job.id:
                self.jobs.pop(next(iter(self.jobs)))
        ev = {"t": "done", "turn": job.id, "provider": job.provider, "changed": changed,
              "exit": res.get("exit"), "scope": job.scope,
              "snapshot_issues": sorted(job.snapshot_issues),
              "undo_unavailable": [c["path"] for c in changed if not c["undoable"]],
              "out_of_scope": [r for r in out_of_scope if r not in reverted],
              "reverted": reverted,
              "session_id": res.get("session_id") or job.session_id,
              "duration": res.get("duration") or int((time.time() - t0) * 1000)}
        for k in ("cost", "billing", "usage", "context", "is_error", "subtype", "denials", "denied", "stderr"):
            if res.get(k) is not None:
                ev[k] = res[k]
        if changed:
            try:
                self.on_changed([c["path"] for c in changed], job.prompt)
            except Exception:  # noqa: BLE001
                pass
        job.emit(ev)

    # ------------------------------------------------------------ backend extras
    def commands(self, provider: str | None, refresh: bool = False) -> dict:
        backend = self.backend(provider)
        if backend is None:
            return {"error": f"Unknown provider: {provider}"}
        return backend.commands(self.root_fn(), refresh)

    def probe_rate(self, provider: str | None = None) -> dict:
        backend = self.backend(provider)
        if backend is None or not backend.usage_limits:
            return {"error": "This provider reports no usage limits."}
        # The probe is a (tiny) model call, so it passes the same check as a turn.
        bad = backend.preflight(self.root_fn())
        if bad:
            return {"error": bad, "rate": backend.rate}
        return backend.probe_rate()

    def account(self, provider: str | None, root: Path, fresh: bool = False) -> dict:
        """Which account the provider would use, where it has one (Claude); else none."""
        backend = self.backend(provider)
        return (backend.account(root, fresh=fresh) if backend else None) or {"account": None}

    def events(self, jid: int, after: int, timeout: float) -> tuple[list[dict], bool] | None:
        """A turn's events from number `after` on, waiting up to `timeout` seconds for new
        ones, and whether it is done; None for a turn not known (an old one, a restart)."""
        job = self.jobs.get(jid)
        return job.wait_events(after, timeout) if job else None

    def stop(self, jid: int) -> dict:
        job = self.jobs.get(jid)
        if job and not job.done:
            backend = self.backends.get(job.provider)
            if backend:
                backend.stop(job)
            return {"ok": True}
        return {"ok": False}

    def built(self, result: dict) -> None:
        """A build the agent ran with its compile tool: the panel shows it as the editor's."""
        job = self.active
        if job and not job.done:
            job.emit({"t": "build", "result": result})

    def busy(self) -> bool:
        job = self.active
        return bool(job and not job.done)

    def editing(self) -> bool:
        """An Edit turn runs: the agent writes the files, and the editor must not (its turn's
        diff and Undo would take your text for the agent's). Saves wait for its end."""
        job = self.active
        return bool(job and not job.done and job.mode == "edit")

    def server_wrote(self, paths: list[str]) -> None:
        """The editor wrote these project paths (a save, an upload, a build's output folder),
        not the agent: a running turn takes them as they are now, so it neither reports them
        as its changes, nor reverts them, nor lets its Undo put them back."""
        with self.lock:
            job = self.active
            if not job or job.done or job.sealed:
                return
        states, folders = {}, []
        for rel in paths:
            p = job.root / rel
            if p.is_dir() and not p.is_symlink():
                folders.append(rel.rstrip("/") + "/")
                for f in p.rglob("*"):
                    if f.is_file() or f.is_symlink():
                        states[f.relative_to(job.root).as_posix()] = snapshot.state_of(f)
            else:
                states[rel] = snapshot.state_of(p)
        with self.lock:
            if job.sealed:
                return
            job.rebased.update(states)
            job.rebased_folders.update(folders)

    def shutdown(self) -> None:
        """Stop a turn that is still running when the server exits."""
        job = self.active
        if job and not job.done:
            self.stop(job.id)

    def undo(self, turn: int) -> dict:
        """Restore files changed in `turn`, only where they still match the turn's result."""
        job = self.turns.get(turn)
        if not job:
            return {"error": "unknown turn"}
        rels = snapshot.changed_paths(job.before, job.after)
        if not self.writes.acquire(timeout=UNDO_WAIT):
            return {"busy": True, "error": "git is busy with the files; try Undo again in a moment"}
        try:
            restored = snapshot.restore(job.root, job.before, job.after, rels)
        finally:
            self.writes.release()
        if restored:
            try:
                self.on_changed(restored, "Undo of an agent turn")
            except Exception:  # noqa: BLE001
                pass
        return {"restored": restored, "skipped": [r for r in rels if r not in restored]}
