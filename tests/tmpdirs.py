"""Temporary folders for the tests, removed when the test run ends."""
import atexit
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

_made: list = []


def tmpdir() -> Path:
    """A new, empty temporary folder (prism-test-…) that is removed at the end of the run."""
    d = Path(tempfile.mkdtemp(prefix="prism-test-"))
    _made.append(d)
    return d


def _writable(func, path, *_):
    os.chmod(path, stat.S_IWRITE)       # git's object files are read-only on Windows
    func(path)


@atexit.register
def _remove_all():
    # At exit, after every test has stopped its servers. rmtree removes a junction
    # without following it into its target.
    kw = {"onexc": _writable} if sys.version_info >= (3, 12) else {"onerror": _writable}
    for d in _made:
        try:
            shutil.rmtree(d, **kw)
        except OSError:
            pass
