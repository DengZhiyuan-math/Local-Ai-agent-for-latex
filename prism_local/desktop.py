"""The system's file manager, file clipboard and Recycle Bin (Trash).

reveal(path) shows a file or folder selected in Explorer, Finder or the file manager;
open_folder(path) opens a folder. copy_files/clipboard_files put files on the system
clipboard and read them from it, as Explorer and Finder copy and paste them. trash(paths)
moves files and folders to the Recycle Bin, or the Trash, never deleting them for good.
PRISM_CLIPBOARD_FILE and PRISM_TRASH_DIR (the browser checks set them) keep both away
from the system: the copied paths go into that file, what is trashed into that folder.

What a system cannot do raises Unsupported, with a message for the author.
Windows uses its own API (ctypes); macOS uses osascript (JavaScript for Automation).
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class Unsupported(OSError):
    """This system offers no way to do it; the message says what to do instead."""


def reveal(path: Path) -> None:
    """Show `path` selected in the folder that holds it."""
    path = Path(path)
    if os.name == "nt":
        # explorer parses its own command line: /select,"…" must stay one unquoted token.
        subprocess.Popen(f'explorer /select,"{path}"', **NO_WINDOW)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def open_folder(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def copy_files(paths: list[Path]) -> None:
    """Put these files and folders on the clipboard, to paste in Explorer or Finder."""
    paths = [str(Path(p)) for p in paths]
    if os.environ.get("PRISM_CLIPBOARD_FILE"):
        Path(os.environ["PRISM_CLIPBOARD_FILE"]).write_text(json.dumps(paths), encoding="utf-8")
    elif os.name == "nt":
        _win_set_files(paths)
    elif sys.platform == "darwin":
        _jxa(MAC_SET, paths)
    else:
        raise Unsupported("this system's file clipboard is not supported")


def clipboard_files() -> list[Path]:
    """The files and folders copied in Explorer or Finder (or by copy_files); [] if none."""
    if os.environ.get("PRISM_CLIPBOARD_FILE"):
        f = Path(os.environ["PRISM_CLIPBOARD_FILE"])
        return [Path(p) for p in json.loads(f.read_text(encoding="utf-8"))] if f.exists() else []
    if os.name == "nt":
        return [Path(p) for p in _win_get_files()]
    if sys.platform == "darwin":
        return [Path(p) for p in json.loads(_jxa(MAC_GET, []) or "[]")]
    raise Unsupported("this system's file clipboard is not supported")


def trash(paths: list[Path]) -> None:
    """Move these files and folders to the Recycle Bin or Trash, where they can be restored.
    With PRISM_TRASH_DIR set, they go into that folder instead."""
    paths = [Path(p) for p in paths]
    if os.environ.get("PRISM_TRASH_DIR"):
        _move_into(Path(os.environ["PRISM_TRASH_DIR"]), paths)
    elif os.name == "nt":
        _win_trash(paths)
    elif sys.platform == "darwin":
        _jxa(MAC_TRASH, [str(p) for p in paths])
    elif shutil.which("gio"):
        p = subprocess.run(["gio", "trash", "--", *map(str, paths)], capture_output=True, text=True)
        if p.returncode:
            raise OSError(p.stderr.strip() or "could not move it to the Trash")
    else:
        raise Unsupported("no Trash is available here (gio is missing); delete it in your file manager")


def _move_into(folder: Path, paths: list[Path]) -> None:
    """Move each path into `folder`, as "name (2)" and so on when the name is taken there."""
    folder.mkdir(parents=True, exist_ok=True)
    for p in paths:
        if not p.exists() and not p.is_symlink():
            raise FileNotFoundError(f"not found: {p}")
        dest, n = folder / p.name, 1
        while dest.exists() or dest.is_symlink():
            n += 1
            dest = folder / f"{p.stem} ({n}){p.suffix}"
        shutil.move(str(p), str(dest))


# ---------------------------------------------------------------- Windows

CF_HDROP = 15
GHND = 0x42                     # movable, zero-filled
DRIVE_FIXED = 3
FO_DELETE = 3
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
FOF_WANTNUKEWARNING = 0x4000   # ask before deleting for good (too large for the Recycle Bin)


def _win():
    import ctypes
    from ctypes import wintypes as w
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    for f, args, res in (
            (user32.OpenClipboard, [w.HWND], w.BOOL), (user32.CloseClipboard, [], w.BOOL),
            (user32.EmptyClipboard, [], w.BOOL), (user32.IsClipboardFormatAvailable, [w.UINT], w.BOOL),
            (user32.GetClipboardData, [w.UINT], w.HANDLE),
            (user32.SetClipboardData, [w.UINT, w.HANDLE], w.HANDLE),
            (user32.RegisterClipboardFormatW, [w.LPCWSTR], w.UINT),
            (kernel32.GlobalAlloc, [w.UINT, ctypes.c_size_t], w.HGLOBAL),
            (kernel32.GlobalLock, [w.HGLOBAL], ctypes.c_void_p),
            (kernel32.GlobalUnlock, [w.HGLOBAL], w.BOOL), (kernel32.GlobalFree, [w.HGLOBAL], w.HGLOBAL),
            (kernel32.GetDriveTypeW, [w.LPCWSTR], w.UINT),
            (shell32.DragQueryFileW, [w.HANDLE, w.UINT, w.LPWSTR, w.UINT], w.UINT)):
        f.argtypes, f.restype = args, res
    return ctypes, user32, kernel32, shell32


class _Clipboard:
    """The clipboard, opened (another program may hold it for a moment)."""

    def __init__(self, user32):
        self.user32 = user32

    def __enter__(self):
        for _ in range(20):
            if self.user32.OpenClipboard(None):
                return self
            time.sleep(0.05)
        raise OSError("the clipboard is in use by another program; try again")

    def __exit__(self, *exc):
        self.user32.CloseClipboard()


def _win_global(ctypes, kernel32, data: bytes):
    h = kernel32.GlobalAlloc(GHND, len(data))
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    ctypes.memmove(kernel32.GlobalLock(h), data, len(data))
    kernel32.GlobalUnlock(h)
    return h


def _win_set_files(paths: list[str]) -> None:
    ctypes, user32, kernel32, _ = _win()
    # DROPFILES {pFiles, pt.x, pt.y, fNC, fWide}, then each path and an empty one, in UTF-16.
    drop = struct.pack("<IiiII", 20, 0, 0, 0, 1) + \
        b"".join(p.encode("utf-16-le") + b"\0\0" for p in paths) + b"\0\0"
    effect = user32.RegisterClipboardFormatW("Preferred DropEffect")
    with _Clipboard(user32):
        user32.EmptyClipboard()
        for fmt, data in ((CF_HDROP, drop), (effect, struct.pack("<I", 1))):   # 1: copy
            h = _win_global(ctypes, kernel32, data)
            if not user32.SetClipboardData(fmt, h):         # on success the system owns h
                kernel32.GlobalFree(h)
                raise ctypes.WinError(ctypes.get_last_error())


def _win_get_files() -> list[str]:
    ctypes, user32, _, shell32 = _win()
    if not user32.IsClipboardFormatAvailable(CF_HDROP):
        return []
    with _Clipboard(user32):
        h = user32.GetClipboardData(CF_HDROP)
        if not h:
            return []
        out = []
        for i in range(shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)):
            size = shell32.DragQueryFileW(h, i, None, 0) + 1
            buf = ctypes.create_unicode_buffer(size)
            shell32.DragQueryFileW(h, i, buf, size)
            out.append(buf.value)
        return out


def _win_trash(paths: list[Path]) -> None:
    ctypes, _, kernel32, shell32 = _win()
    from ctypes import wintypes as w
    for p in paths:
        # Only fixed drives have a Recycle Bin: elsewhere "delete" would be for good.
        if kernel32.GetDriveTypeW(p.anchor) != DRIVE_FIXED:
            raise Unsupported(f"{p.anchor} has no Recycle Bin; delete it in Explorer if you mean to")

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", w.HWND), ("wFunc", w.UINT), ("pFrom", w.LPCWSTR), ("pTo", w.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", w.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", w.LPCWSTR)]

    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    op = SHFILEOPSTRUCTW(wFunc=FO_DELETE, pFrom="".join(f"{p}\0" for p in paths) + "\0",
                         fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_WANTNUKEWARNING
                         | FOF_NOERRORUI | FOF_SILENT)
    code = shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted:
        raise OSError(f"could not move it to the Recycle Bin (Windows error {code:#x}); "
                      "a file in it may be open in another program")


# ---------------------------------------------------------------- macOS

MAC_SET = """ObjC.import('AppKit');
function run(argv) {
  const urls = $.NSMutableArray.alloc.init;
  argv.forEach((p) => urls.addObject($.NSURL.fileURLWithPath(p)));
  const pb = $.NSPasteboard.generalPasteboard;
  pb.clearContents;
  if (!pb.writeObjects(urls)) throw new Error('could not write the clipboard');
}"""

MAC_GET = """ObjC.import('AppKit');
function run() {
  const pb = $.NSPasteboard.generalPasteboard;
  const items = pb.readObjectsForClassesOptions($.NSArray.arrayWithObject($.NSURL), $());
  const out = [];
  if (!items.isNil()) for (let i = 0; i < items.count; i++) {
    const u = items.objectAtIndex(i);
    if (u.isFileURL) out.push(ObjC.unwrap(u.path));
  }
  return JSON.stringify(out);
}"""

MAC_TRASH = """ObjC.import('Foundation');
function run(argv) {
  const fm = $.NSFileManager.defaultManager;
  argv.forEach((p) => {
    if (!fm.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(p), null, null))
      throw new Error('could not move ' + p + ' to the Trash');
  });
}"""


def _jxa(script: str, args: list[str]) -> str:
    p = subprocess.run(["osascript", "-l", "JavaScript", "-e", script, *args],
                       capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise OSError(p.stderr.strip() or "osascript failed")
    return p.stdout.strip()
