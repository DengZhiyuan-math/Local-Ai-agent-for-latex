"""Writing files so that no reader sees half of one (fsutil.py)."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsutil  # noqa: E402
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
