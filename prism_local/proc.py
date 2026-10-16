"""Starting and stopping the programs prism-local runs (TeX tools, git, agent CLIs)."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import weakref

# When the server runs without a console (started by the launcher), every console
# program it starts (git, tectonic, claude) would otherwise flash its own window.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

# For a program that may start programs of its own (latexmk runs pdflatex, an agent CLI
# runs shell commands): on Linux and macOS it leads a process group of its own (start).
TREE = NO_WINDOW if os.name == "nt" else {"start_new_session": True}


def start(cmd, **popen) -> subprocess.Popen:
    """Start a program that may start programs of its own, so that kill_tree and end_group
    stop everything it started, also what it left running when it exited.

    On Linux and macOS it leads a process group of its own. On Windows it starts suspended
    in a job object of its own and then runs: whatever it starts is in that job too, even
    once its parent is gone (taskkill /T finds children only through a running parent).
    The job ends with its last handle, so if this server dies they end with it. A program
    that asks to leave the job (CREATE_BREAKAWAY_FROM_JOB) may. Where no job can be made,
    taskkill /T is all there is, as before."""
    if os.name != "nt":
        return subprocess.Popen(cmd, **TREE, **popen)
    job = _win_job()
    flags = popen.pop("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, creationflags=flags | (CREATE_SUSPENDED if job else 0), **popen)
    if job:
        k32, ntdll = _win()
        joined = k32.AssignProcessToJobObject(job, int(proc._handle))
        if ntdll.NtResumeProcess(int(proc._handle)) != 0:
            k32.CloseHandle(job)
            proc.kill()
            raise OSError(f"could not start {cmd[0]}: it did not resume")
        if joined:
            holder = [job]
            proc._prism_job = holder
            weakref.finalize(proc, _close_job, holder)
        else:
            k32.CloseHandle(job)
    return proc


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a process and its children. On Windows a CLI may be a .cmd shim around node,
    and MiKTeX's pdflatex.exe runs the real engine as a child process.
    Never raises: taskkill failing or hanging leaves at least the program itself killed."""
    if _end_job(proc):                  # Windows, started by start(): the whole job
        _reap(proc)
        return
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15, **NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            _signal_group(proc, signal.SIGKILL)
        _reap(proc)


def end_group(proc: subprocess.Popen) -> None:
    """After `proc` (started by start()) has exited: end the commands it left running, its
    process group on Linux and macOS, its job on Windows."""
    if proc.poll() is None:
        return
    if os.name == "nt":
        _end_job(proc)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
        proc.wait(5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the process group `proc` leads (started by start()), else only `proc`."""
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


# ---------------------------------------------------------------- Windows job objects

CREATE_SUSPENDED = 0x4
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x800
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_jobs = threading.Lock()                # a job's handle is closed once, by one thread
_api = None


def _win():
    global _api
    if _api is None:
        import ctypes
        from ctypes import wintypes as w
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        for f, args, res in ((k32.CreateJobObjectW, [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
                             (k32.SetInformationJobObject, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
                             (k32.AssignProcessToJobObject, [w.HANDLE, w.HANDLE], w.BOOL),
                             (k32.TerminateJobObject, [w.HANDLE, w.UINT], w.BOOL),
                             (k32.CloseHandle, [w.HANDLE], w.BOOL),
                             (ntdll.NtResumeProcess, [w.HANDLE], ctypes.c_long)):
            f.argtypes, f.restype = args, res
        _api = (k32, ntdll)
    return _api


def _win_job():
    """A new job whose processes end when its last handle closes; None if none can be made."""
    import ctypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    try:
        k32, _ = _win()
        job = k32.CreateJobObjectW(None, None)
    except (OSError, AttributeError):
        return None
    if not job:
        return None
    info = Extended()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
    if not k32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                       ctypes.byref(info), ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return None
    return job


def _end_job(proc: subprocess.Popen) -> bool:
    """End every process in `proc`'s job; False when it has none (or it was ended)."""
    holder = getattr(proc, "_prism_job", None)
    with _jobs:
        job = holder[0] if holder else None
        if not job:
            return False
        holder[0] = None
    k32, _ = _win()
    k32.TerminateJobObject(job, 1)
    k32.CloseHandle(job)
    return True


def _close_job(holder: list) -> None:
    """The Popen is gone: its job's handle closes, which ends what is still in it."""
    with _jobs:
        job, holder[0] = holder[0], None
    if job:
        _win()[0].CloseHandle(job)
