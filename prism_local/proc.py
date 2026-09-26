"""Starting and stopping the programs prism-local runs (TeX tools, git, agent CLIs)."""
from __future__ import annotations

import os
import subprocess

# When the server runs without a console (started by the launcher), every console
# program it starts (git, tectonic, claude) would otherwise flash its own window.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a process and its children. On Windows a CLI may be a .cmd shim around node,
    and MiKTeX's pdflatex.exe runs the real engine as a child process."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, timeout=15, **NO_WINDOW)
    else:
        proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
