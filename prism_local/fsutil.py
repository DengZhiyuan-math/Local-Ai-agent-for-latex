"""Project files: which ones the editor shows, and writing them safely on every system."""
from __future__ import annotations

import os
import re
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from errors import Conflict, Refused

EDITABLE_SUFFIXES = {".tex", ".bib", ".md", ".sty", ".cls", ".bbx", ".cbx", ".txt", ".tikz"}
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv"}
TEMP_SUFFIX = ".prism-tmp"      # a save half-way (replace_file): never a project's file
METADATA = {".git", ".codex", ".agents", ".aws"}   # repository and agent metadata
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
                    *(f"LPT{i}" for i in range(10))}


def _check_windows_name(api, handle, expected=None):
    """Capture or check a normalized physical NT name, including exact case."""
    import ctypes

    # VOLUME_NAME_NT avoids drive letters and mounted-folder DOS aliases.
    needed = api.GetFinalPathNameByHandleW(handle, None, 0, 0x2)
    if not needed:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(needed)
    length = api.GetFinalPathNameByHandleW(handle, buffer, needed, 0x2)
    if not length:
        raise ctypes.WinError(ctypes.get_last_error())
    if length >= needed:
        raise Conflict("The checked file name changed while it was opened.")

    # Each expected name derives from the already opened parent, never a late
    # path resolve which could adopt a junction's target. Case folding could
    # alias distinct names in a case-sensitive directory.
    if expected is not None and buffer.value != expected:
        raise Refused("The opened file does not have its expected unlinked name.")
    return buffer.value


@contextmanager
def _windows_file(path: Path):
    """Open a regular leaf with pinned names and without write/delete sharing."""
    import ctypes
    from ctypes import wintypes as w
    import msvcrt

    # Attributes are queried on the opened object, never through a second path
    # lookup. OPEN_REPARSE_POINT opens a link itself so it can be rejected.
    class Attributes(ctypes.Structure):
        _fields_ = [("attributes", ctypes.c_uint32), ("tag", ctypes.c_uint32)]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                               w.DWORD, w.DWORD, w.HANDLE]
    api.CreateFileW.restype = w.HANDLE
    api.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    api.GetFileInformationByHandleEx.restype = w.BOOL
    api.GetFileType.argtypes = [w.HANDLE]
    api.GetFileType.restype = w.DWORD
    api.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    api.GetFinalPathNameByHandleW.restype = w.DWORD
    api.CloseHandle.argtypes = [w.HANDLE]
    api.CloseHandle.restype = w.BOOL
    invalid = ctypes.c_void_p(-1).value
    open_reparse, backup_semantics = 0x00200000, 0x02000000
    handles = []
    leaf = None
    parent_name = None
    try:
        for directory in reversed(path.parents):
            # FILE_READ_ATTRIBUTES; share read/write, but not delete. Holding
            # each ancestor blocks directory replacement throughout the read.
            # The final-handle name check also rejects in-place reparse tagging.
            handle = api.CreateFileW(str(directory), 0x80, 0x3, None, 3,
                                     open_reparse | backup_semantics, None)
            if handle == invalid:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            expected = parent_name.rstrip("\\") + "\\" + directory.name if parent_name else None
            parent_name = _check_windows_name(api, handle, expected)
            attrs = Attributes()
            if not api.GetFileInformationByHandleEx(handle, 9, ctypes.byref(attrs), ctypes.sizeof(attrs)):
                raise ctypes.WinError(ctypes.get_last_error())
            if attrs.attributes & 0x400 or not attrs.attributes & 0x10:
                raise Refused("A checked file cannot have linked directory ancestors.")
        # GENERIC_READ; FILE_SHARE_READ only. An existing writer prevents this
        # open, and writes/deletes cannot start until the descriptor is closed.
        leaf = api.CreateFileW(str(path), 0x80000000, 0x1, None, 3, open_reparse, None)
        if leaf == invalid:
            leaf = None
            raise ctypes.WinError(ctypes.get_last_error())
        _check_windows_name(api, leaf, parent_name.rstrip("\\") + "\\" + path.name)
        attrs = Attributes()
        if not api.GetFileInformationByHandleEx(leaf, 9, ctypes.byref(attrs), ctypes.sizeof(attrs)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attrs.attributes & (0x400 | 0x10) or api.GetFileType(leaf) != 1:
            raise Refused("A checked file must be a regular disk file without links.")
        fd = msvcrt.open_osfhandle(leaf, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        leaf = None                 # ownership transferred to the CRT descriptor
        try:
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            yield stream
    finally:
        if leaf is not None:
            api.CloseHandle(leaf)
        for handle in reversed(handles):
            api.CloseHandle(handle)


def read_windows_file(path: Path, limit: int) -> tuple[bytes, tuple]:
    """Read a bounded, unchanged Windows file and return its handle metadata.

    Windows path stat and fstat can give ctime different meanings. Pinned native
    handles protect the path; both version observations use fstat on that handle.
    """
    path = Path(path)
    if (path.drive and not path.is_absolute() or ".." in path.parts
            or str(path).casefold().startswith(("\\\\.\\", "\\\\?\\globalroot\\"))):
        raise Refused("A checked file needs an ordinary filesystem path.")
    path = path.absolute()
    if not path.name or any(":" in part for part in path.parts[1:]):
        raise Refused("A checked file needs an ordinary filesystem path.")
    fields = lambda item: (item.st_dev, item.st_ino, item.st_size,
                           item.st_mtime_ns, item.st_ctime_ns)
    with _windows_file(path) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= limit:
            raise Refused("A checked file must be a bounded regular file.")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if len(raw) > limit or fields(before) != fields(after):
            raise Conflict("The checked file changed while it was read.")
        return raw, fields(after)


def bad_name(name: str) -> str | None:
    """Why `name` cannot name a project's file or folder on every system, or None."""
    if not name.strip() or name in (".", ".."):
        return "give a name"
    if re.search(r'[\x00-\x1f<>:"/\\|?*]', name):
        return 'a name cannot contain / \\ : * ? " < > |'
    if name != name.strip() or name.endswith("."):
        return "a name cannot start or end with a space, or end with a dot"
    if name.startswith("."):
        return "a name starting with a dot would be hidden in the editor"
    if name.split(".")[0].upper() in WINDOWS_RESERVED:
        return f"{name} is a reserved name on Windows"
    if len(name) > 200:
        return "the name is too long"
    return None


def is_metadata(path: Path) -> bool:
    # Case-insensitive everywhere: Windows and macOS file systems usually are.
    return any(part.casefold() in METADATA for part in path.parts)


def metadata_paths(root: Path, onerror=None):
    """The metadata folders and files at any depth below the resolved `root`, not entered,
    without following folder links or Windows junctions. A folder removed meanwhile is
    skipped; `onerror(error)` gets any other folder that could not be looked at."""
    def unreadable(error):
        if onerror and not isinstance(error, FileNotFoundError):
            onerror(error)

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        parent = Path(directory)
        for name in sorted(dirs + files):
            if is_metadata(Path(name)):
                yield parent / name
        # resolve also detects Windows junctions on Python versions before is_junction.
        dirs[:] = [name for name in dirs if not is_metadata(Path(name))
                   and (parent / name).resolve() == parent / name]


def project_path(root: Path, rel: str) -> Path:
    """Resolve an in-project path. Internal links are allowed; .git is off limits."""
    rel = str(rel).replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"[A-Za-z]:", rel):
        raise Refused("give a path relative to the project root")
    root = root.resolve()
    p = (root / rel).resolve()
    if root not in p.parents:
        raise Refused("the path is outside the project")
    # Case-insensitive: on macOS (as on Windows) .GIT names the same directory.
    if any(part.casefold() == ".git" for part in (*Path(rel).parts, *p.relative_to(root).parts)):
        raise Refused("the .git directory is off limits")
    return p


def uses_crlf(data: bytes) -> bool:
    """Whether most line ends in `data` are Windows ones (\\r\\n)."""
    crlf = data.count(b"\r\n")
    return crlf > 0 and crlf * 2 >= data.count(b"\n")


def with_line_ends_of(text: str, path: Path) -> str:
    """`text` (with \\n line ends, as an editor or a model writes it) with the line ends the
    file at `path` already uses, so a save never turns a whole file into a diff."""
    text = text.replace("\r\n", "\n")
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return text
    return text.replace("\n", "\r\n") if uses_crlf(head) else text


def replace_file(path: Path, data: bytes, tries: int = 40, durable: bool = False) -> None:
    """Write a file so that a reader never sees half of it: the data goes to a temporary
    file beside it, which then replaces `path`. Windows refuses the replace while another
    process has the file open (a server reading it, a TeX engine during a build), so it is
    tried every 0.05 seconds, `tries` times in all, before PermissionError is raised.
    `durable`: the data is on the disk before it replaces the file (and, outside Windows,
    the replacement too), so that a power cut leaves the old file or the new one, never an
    empty one; for the state files, whose loss loses the project list or the settings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}{TEMP_SUFFIX}")
    try:
        if durable:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        else:
            tmp.write_bytes(data)
        try:
            shutil.copymode(path, tmp)      # the new file keeps the old one's permissions
        except OSError:
            pass                            # a new file: the default permissions
        for attempt in range(tries):
            try:
                os.replace(tmp, path)
                if durable and os.name != "nt":
                    _sync_folder(path.parent)
                return
            except PermissionError:
                if attempt == tries - 1:
                    raise
                time.sleep(0.05)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _sync_folder(folder: Path) -> None:
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_bytes(path: Path, data: bytes) -> None:
    """Write a project file so that a reader never sees half of it, where the system allows.

    On Windows replace_file fails while another program has the file open, for instance a
    TeX engine reading it during a build; after retrying for about a second the file is
    written in place, which such programs allow. Should that write fail half-way (a full
    disk), the old contents are put back and the error is raised: a file is never left cut
    short without a word."""
    try:
        replace_file(path, data, tries=20)
        return
    except PermissionError:
        pass
    try:
        old = path.read_bytes()
    except FileNotFoundError:
        old = None
    with open(path, "wb" if old is None else "r+b") as f:
        try:
            f.write(data)
            f.truncate()
            f.flush()
        except OSError as e:
            try:
                f.seek(0)
                f.write(old or b"")
                f.truncate()
                f.flush()
            except OSError:
                raise OSError(f"{path.name} was written only in part ({e}); its earlier "
                              "version could not be put back") from e
            raise
