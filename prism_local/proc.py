"""Starting and stopping the programs prism-local runs (TeX tools, git, agent CLIs)."""
from __future__ import annotations

import os
import signal
import subprocess

# When the server runs without a console (started by the launcher), every console
# program it starts (git, tectonic, claude) would otherwise flash its own window.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

# For a program that may start programs of its own (latexmk runs pdflatex, an agent CLI
# runs shell commands): on Linux and macOS it leads a process group of its own, so that
# kill_tree can stop the whole group. On Windows taskkill /T finds the tree anyway.
TREE = NO_WINDOW if os.name == "nt" else {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a process and its children. On Windows a CLI may be a .cmd shim around node,
    and MiKTeX's pdflatex.exe runs the real engine as a child process."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, timeout=15, **NO_WINDOW)
    else:
        _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            _signal_group(proc, signal.SIGKILL)
        proc.kill()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the process group `proc` leads (started with TREE), else only `proc`."""
    try:
        if os.getpgid(proc.pid) == proc.pid:
            os.killpg(proc.pid, sig)
            return
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        pass
