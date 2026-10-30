"""Writing files so that no reader sees half of one (fsutil.py)."""
import os
import stat
import subprocess
import sys
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsutil  # noqa: E402
from errors import Conflict, Refused  # noqa: E402
from tmpdirs import tmpdir  # noqa: E402

REFUSED = PermissionError(13, "The process cannot access the file because it is being used")


class HalfWritten:
    """A file whose first `fails` writes stop half-way, as on a full disk."""

    def __init__(self, f, fails):
        self.f, self.fails = f, fails

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.f.close()

    def write(self, data):
        if self.fails:
            self.fails -= 1
            self.f.write(data[:len(data) // 2])
            self.f.flush()
            raise OSError(28, "No space left on device")
        return self.f.write(data)

    def __getattr__(self, name):
        return getattr(self.f, name)


class WindowsReadContract(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.path = self.dir / "record.json"
        self.raw = b'{"record": "unchanged"}\r\n'
        self.path.write_bytes(self.raw)
        opened = self.path.stat()
        self.metadata = {name: getattr(opened, name) for name in
                         ("st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")}

    def read(self, before=None, after=None, limit=1000):
        with mock.patch.object(fsutil, "_windows_file", side_effect=lambda p: p.open("rb")), \
                mock.patch.object(fsutil.os, "fstat", side_effect=[
                    SimpleNamespace(**(before or self.metadata)),
                    SimpleNamespace(**(after or self.metadata))]):
            return fsutil.read_windows_file(self.path, limit)

    def test_bounded_handle_read_preserves_binary_bytes_and_version(self):
        raw, version = self.read()
        self.assertEqual(raw, self.raw)
        self.assertEqual(version, tuple(self.metadata[name] for name in
                         ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")))

    def test_every_handle_version_field_is_still_checked(self):
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
            with self.subTest(field=field):
                changed = {**self.metadata, field: self.metadata[field] + 1}
                with self.assertRaises(Conflict):
                    self.read(after=changed)

    def test_nonregular_and_oversized_files_are_refused(self):
        for metadata in ({**self.metadata, "st_mode": stat.S_IFIFO},
                         {**self.metadata, "st_size": 1001}):
            with self.subTest(metadata=metadata), self.assertRaises(Refused):
                self.read(before=metadata)

    def test_growth_past_the_read_limit_is_refused_even_with_unchanged_reported_size(self):
        small = {**self.metadata, "st_size": 1}
        with self.assertRaises(Conflict):
            self.read(before=small, after=small, limit=4)

    def test_device_and_alternate_stream_paths_never_reach_the_native_opener(self):
        with mock.patch.object(fsutil, "_windows_file") as opened:
            for path in (self.dir / "record.json:stream", Path(r"\\.\pipe\record"),
                         Path(r"\\?\GLOBALROOT\Device\record"), Path("record.json:stream")):
                with self.subTest(path=path), self.assertRaises(Refused):
                    fsutil.read_windows_file(path, 1000)
            opened.assert_not_called()

    def test_final_handle_name_rejects_redirects_instead_of_adopting_their_target(self):
        expected = r"\Device\HarddiskVolume2\project\chapter\record.json"

        def api_for(name):
            def final(handle, buffer, length, flags):
                self.assertEqual(flags, 0x2)
                if buffer is None:
                    return len(name) + 1
                buffer.value = name
                return len(name)
            return SimpleNamespace(GetFinalPathNameByHandleW=final)

        self.assertEqual(fsutil._check_windows_name(api_for(expected), 1, expected), expected)
        for actual in (r"\Device\HarddiskVolume2\outside\chapter\record.json",
                       r"\Device\HarddiskVolume9\project\chapter\record.json",
                       r"\Device\HarddiskVolume2\PROJECT\chapter\record.json"):
            with self.subTest(actual=actual), self.assertRaises(Refused):
                fsutil._check_windows_name(api_for(actual), 1, expected)
        anchor = r"\Device\Mup\server\share"
        self.assertEqual(fsutil._check_windows_name(api_for(anchor), 1), anchor)


@unittest.skipUnless(os.name == "nt", "Native Windows handle sharing and reparse points.")
class NativeWindowsReads(unittest.TestCase):
    def test_read_holds_parent_and_leaf_against_replace_and_write(self):
        directory = tmpdir() / "parent"
        directory.mkdir()
        path = directory / "record.json"
        path.write_bytes(b"unchanged")
        with fsutil._windows_file(path) as stream:
            with self.assertRaises(OSError):
                directory.rename(directory.with_name("moved"))
            with self.assertRaises(OSError):
                path.write_bytes(b"changed")
            self.assertEqual(stream.read(), b"unchanged")
        directory.rename(directory.with_name("moved"))

    def test_regular_read_and_linked_leaf_refusal(self):
        root = tmpdir()
        target = root / "target.json"
        target.write_bytes(b'{"valid": true}\r\n')
        self.assertEqual(fsutil.read_windows_file(target, 1000)[0], target.read_bytes())
        link = root / "linked.json"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("File symlink creation is unavailable.")
        with self.assertRaises(Refused):
            fsutil.read_windows_file(link, 1000)

    def test_junction_ancestor_is_refused(self):
        root = tmpdir()
        outside = root / "outside"
        outside.mkdir()
        (outside / "record.json").write_bytes(b"external")
        alias = root / "junction"
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(alias), str(outside)],
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            self.skipTest("Directory junction creation is unavailable.")
        self.addCleanup(os.rmdir, alias)
        with self.assertRaises(Refused):
            fsutil.read_windows_file(alias / "record.json", 1000)


class Writes(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.path = self.dir / "main.tex"
        self.path.write_bytes(b"the old version, which is rather long\n")
        sleep = mock.patch.object(fsutil.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def files(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_a_refused_replace_is_tried_again(self):
        real, calls = os.replace, []

        def replace(src, dst):              # another process reads the file for a moment
            calls.append(dst)
            if len(calls) < 3:
                raise REFUSED
            real(src, dst)

        with mock.patch.object(fsutil.os, "replace", replace):
            fsutil.replace_file(self.path, b"new\n")
        self.assertEqual(self.path.read_bytes(), b"new\n")
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.files(), ["main.tex"], "no temporary file is left")

    def test_replace_file_gives_up_with_an_error_and_keeps_the_file(self):
        with mock.patch.object(fsutil.os, "replace", side_effect=REFUSED):
            with self.assertRaises(PermissionError):
                fsutil.replace_file(self.path, b"new\n", tries=3)
        self.assertEqual(self.path.read_bytes(), b"the old version, which is rather long\n")
        self.assertEqual(self.files(), ["main.tex"])

    def test_a_file_open_elsewhere_is_written_in_place(self):
        with mock.patch.object(fsutil.os, "replace", side_effect=REFUSED):
            fsutil.write_bytes(self.path, b"new\n")
        self.assertEqual(self.path.read_bytes(), b"new\n")
        self.assertEqual(self.files(), ["main.tex"])

    def test_an_in_place_write_that_fails_half_way_puts_the_old_version_back(self):
        real_open = open
        with mock.patch.object(fsutil.os, "replace", side_effect=REFUSED), \
                mock.patch.object(fsutil, "open", create=True,
                                  side_effect=lambda *a: HalfWritten(real_open(*a), 1)):
            with self.assertRaises(OSError) as e:
                fsutil.write_bytes(self.path, b"a new version, longer than the old one\n")
        self.assertEqual(e.exception.errno, 28)
        self.assertEqual(self.path.read_bytes(), b"the old version, which is rather long\n")

    def test_a_file_left_half_written_is_reported(self):
        real_open = open
        with mock.patch.object(fsutil.os, "replace", side_effect=REFUSED), \
                mock.patch.object(fsutil, "open", create=True,
                                  side_effect=lambda *a: HalfWritten(real_open(*a), 2)):
            with self.assertRaisesRegex(OSError, "main.tex was written only in part"):
                fsutil.write_bytes(self.path, b"a new version, longer than the old one\n")


if __name__ == "__main__":
    unittest.main()
