"""Windows/macOS directory notifications. Workers wait in the OS, never poll the disk.

ReadDirectoryChangesW is armed before returning and rearmed before delivery, so a
change during reconciliation is retained for the next request. Overflow invalidates
the snapshot too. Unsupported file systems use missing-path recovery in diskprojects.
"""
from __future__ import annotations

import ctypes
import os
import struct
import threading
import sys
from pathlib import Path

from fsutil import SKIP_DIRS


def relevant(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    if any(p.startswith(".") or p in SKIP_DIRS | {"build", "conversations", "prism-uploads"}
           for p in parts):
        return False
    # Directory names (including old names from rename notifications) can contain
    # dots, so only known generated files are filtered rather than all suffixes.
    suffix = Path(parts[-1]).suffix.lower()
    return suffix not in {".aux", ".log", ".out", ".toc", ".gz", ".fls", ".blg",
                          ".bbl", ".fdb_latexmk", ".pdf", ".json"} or parts[-1] == "prism.json"


def registry_event(name):
    name = name.replace("\\", "/")
    return name in {"projects.json", "settings.json"} or (name.startswith("instances/") and name.endswith(".json"))


def _api():
    from ctypes import wintypes as w
    api = ctypes.WinDLL("kernel32", use_last_error=True)

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

    signatures = {
        "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
        "CreateEventW": ([ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR], w.HANDLE),
        "ReadDirectoryChangesW": ([w.HANDLE, ctypes.c_void_p, w.DWORD, w.BOOL, w.DWORD,
                                  ctypes.POINTER(w.DWORD), ctypes.POINTER(Overlapped), ctypes.c_void_p], w.BOOL),
        "WaitForMultipleObjects": ([w.DWORD, ctypes.POINTER(w.HANDLE), w.BOOL, w.DWORD], w.DWORD),
        "GetOverlappedResult": ([w.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(w.DWORD), w.BOOL], w.BOOL),
        "CancelIoEx": ([w.HANDLE, ctypes.POINTER(Overlapped)], w.BOOL),
        "SetEvent": ([w.HANDLE], w.BOOL), "ResetEvent": ([w.HANDLE], w.BOOL),
        "CloseHandle": ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        getattr(api, name).argtypes = args
        getattr(api, name).restype = result
    return api, Overlapped, w


class DirectoryWatch:
    def __init__(self, root: Path, changed, excluded=(), accept=relevant):
        self.api, overlapped, self.w = _api()
        self.changed = changed
        self.root, self.excluded = root, excluded
        self.accept = accept
        self.handle = self.api.CreateFileW(str(root), 1, 7, None, 3, 0x42000000, None)
        if self.handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.stop = self.api.CreateEventW(None, True, False, None)
        self.over = overlapped()
        self.over.hEvent = self.api.CreateEventW(None, True, False, None)
        self.buffer = ctypes.create_string_buffer(32768)
        try:
            if not self.stop or not self.over.hEvent:
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            self._close_handles()
            raise
        self.ready, self.error = threading.Event(), None
        self.thread = threading.Thread(target=self._run, daemon=True, name="directory-notifications")
        self.thread.start()
        if not self.ready.wait(2):
            self.close()
            raise OSError('Directory watcher did not start')
        if self.error:
            raise self.error

    def _arm(self):
        self.api.ResetEvent(self.over.hEvent)
        if not self.api.ReadDirectoryChangesW(self.handle, self.buffer, len(self.buffer),
                                              True, 1 | 2 | 16, None, ctypes.byref(self.over), None):
            if ctypes.get_last_error() != 997:  # ERROR_IO_PENDING
                raise ctypes.WinError(ctypes.get_last_error())

    def _run(self):
        handles = (self.w.HANDLE * 2)(self.stop, self.over.hEvent)
        try:
            # Windows cancels a thread's outstanding I/O when that thread exits.
            # Arm here, never in the short-lived HTTP thread that constructs us.
            self._arm()
            self.ready.set()
            while self.api.WaitForMultipleObjects(2, handles, False, 0xffffffff) == 1:
                count = self.w.DWORD()
                ok = self.api.GetOverlappedResult(self.handle, ctypes.byref(self.over), ctypes.byref(count), False)
                if not ok and ctypes.get_last_error() != 1022:  # ERROR_NOTIFY_ENUM_DIR
                    self.changed()
                    return
                raw = self.buffer.raw[:count.value]
                self._arm()
                names, offset = [], 0
                while offset + 12 <= len(raw):
                    next_offset, action, length = struct.unpack_from("<III", raw, offset)
                    names.append(raw[offset + 12:offset + 12 + length].decode("utf-16-le", "replace"))
                    if not next_offset:
                        break
                    offset += next_offset
                if not raw or any(self.accept(name) and not any(
                        (self.root / name).is_relative_to(p) for p in self.excluded) for name in names):
                    self.changed()
        except OSError as exc:
            self.error = exc
            self.changed()
        finally:
            self.ready.set()
            self.api.CancelIoEx(self.handle, ctypes.byref(self.over))
            count = self.w.DWORD()
            self.api.GetOverlappedResult(self.handle, ctypes.byref(self.over), ctypes.byref(count), True)
            self._close_handles()

    def _close_handles(self):
        for handle in (self.over.hEvent, self.stop, self.handle):
            if handle:
                self.api.CloseHandle(handle)

    def close(self):
        if self.thread.is_alive():
            self.api.SetEvent(self.stop)
            self.thread.join(timeout=2)

    def is_alive(self):
        return self.thread.is_alive()


class MacDirectoryWatch:
    """FSEvents on a serial dispatch queue; no polling thread or extra dependency."""
    def __init__(self, root: Path, changed, excluded=(), accept=relevant):
        from macdiskwatch import Watch
        self.watch = Watch(root, changed, excluded, accept)

    def is_alive(self):
        return self.watch.is_alive()

    def close(self):
        self.watch.close()


class Changes:
    def __init__(self, excluded=(), state_root=None):
        self.generation = 0
        self.lock = threading.Condition()
        self.watches = {}
        self.excluded = excluded
        self.state_root = state_root

    def changed(self):
        with self.lock:
            self.generation += 1
            self.lock.notify_all()

    def wait(self, after, timeout=15):
        with self.lock:
            self.lock.wait_for(lambda: self.generation != after, timeout)
            return self.generation

    def stamp(self):
        with self.lock:
            return self.generation

    def configure(self, roots):
        wanted = {str(p): p for p in roots}
        if self.state_root:
            wanted["registry:" + str(self.state_root)] = self.state_root
        for key in list(self.watches):
            if key not in wanted:
                self.watches.pop(key).close()
        factory = DirectoryWatch if os.name == "nt" else MacDirectoryWatch if sys.platform == "darwin" else None
        if factory is None:
            return
        for key, root in wanted.items():
            watch = self.watches.get(key)
            if watch is not None and not watch.is_alive():
                self.watches.pop(key).close()
            if key not in self.watches:
                try:
                    if key.startswith("registry:"):
                        self.watches[key] = factory(root, self.changed, (),
                                                    registry_event)
                    else:
                        self.watches[key] = factory(root, self.changed, self.excluded)
                except OSError:
                    pass  # an unavailable drive: recover when a path is requested

    def close(self):
        for watch in self.watches.values():
            watch.close()
        self.watches.clear()
