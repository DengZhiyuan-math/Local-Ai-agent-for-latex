"""The Claude process boundary, independently of Claude or a real model."""
import os
from pathlib import Path
import tempfile
import unittest

DIRECTORY_DESCRIPTORS = (hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW')
                         and os.open in os.supports_dir_fd and os.scandir in os.supports_fd)


class ClaudeSandbox(unittest.TestCase):
    def test_environment_uses_private_temporary_files_without_changing_home(self):
        import claude_sandbox
        with tempfile.TemporaryDirectory() as temporary:
            scratch = Path(temporary).resolve()
            original = {"HOME": "/unchanged-home", "TOKEN_FOR_CLI": "fixture",
                        "CLAUDE_CODE_TMPDIR": "/outside-private-scratch"}
            env = claude_sandbox.environment(original, scratch)
            self.assertEqual(env["HOME"], "/unchanged-home")
            self.assertEqual(env["TOKEN_FOR_CLI"], "fixture")
            self.assertEqual(original, {"HOME": "/unchanged-home", "TOKEN_FOR_CLI": "fixture",
                                        "CLAUDE_CODE_TMPDIR": "/outside-private-scratch"})
            self.assertTrue(Path(env["TMPDIR"]).is_dir())
            self.assertTrue(Path(env["TMPDIR"]).is_relative_to(scratch))
            self.assertEqual(env["TMPDIR"], env["TMP"])
            self.assertEqual(env["TMPDIR"], env["TEMP"])
            self.assertEqual(env["CLAUDE_CODE_TMPDIR"], env["TMPDIR"])

    def test_cli_profile_can_include_large_installed_plugin_cache(self):
        import claude_sandbox
        import sys
        from unittest import mock
        if not DIRECTORY_DESCRIPTORS:
            self.skipTest("Directory descriptors are unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state = [top / name for name in ('project', 'profile', 'scratch', 'state')]
            for path in (root, profile, scratch, state):path.mkdir()
            for index in range(5001):
                (profile / str(index)).mkdir()
            command = [sys.executable, '-I', '-c', 'pass']
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}):
                wrapped = claude_sandbox.wrap(command, root, profile, scratch)
            self.assertEqual(wrapped[3:], command)

    def test_wrap_keeps_the_cli_argv_behind_a_specific_native_boundary(self):
        import claude_sandbox
        import json
        import sys
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state = [top / name for name in ('project', 'profile', 'scratch', 'state')]
            for path in (root, profile, scratch, state):path.mkdir()
            command = [sys.executable, '-I', '-c', 'print("fixture")']
            # This case tests policy serialization. Descriptor traversal has
            # separate capability-bound tests below.
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}), \
                    mock.patch.object(claude_sandbox, '_verify_unlinked_trees') as verify:
                wrapped = claude_sandbox.wrap(command, root, profile, scratch)
            verify.assert_called_once_with([root, profile, scratch])
            self.assertEqual(wrapped[:2], [str(claude_sandbox.SANDBOX), '-p'])
            self.assertEqual(wrapped[3:], command)
            self.assertIn('(deny default)', wrapped[2])
            self.assertIn('(deny file-write* (subpath ' + json.dumps(str(root)), wrapped[2])
            self.assertIn('(deny file-read* (subpath ' + json.dumps(str(state)), wrapped[2])
            self.assertIn('(remote ip "localhost:*")', wrapped[2])

    def test_cli_writable_folders_cannot_be_inside_the_original_or_private_state(self):
        import claude_sandbox
        import sys
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state = [top / name for name in ('project', 'profile', 'scratch', 'state')]
            for path in (root, profile, scratch, state):path.mkdir()
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}):
                with self.assertRaisesRegex(ValueError, 'outside'):
                    claude_sandbox.wrap([sys.executable], root, root / 'profile', scratch)
                with self.assertRaisesRegex(ValueError, 'outside'):
                    claude_sandbox.wrap([sys.executable], root, profile, state / 'scratch')

    def test_other_platforms_explicitly_report_unsupported(self):
        import claude_sandbox
        from unittest import mock
        claude_sandbox.availability.cache_clear()
        try:
            with mock.patch.object(claude_sandbox.sys, 'platform', 'unsupported'):
                result = claude_sandbox.availability()
            self.assertFalse(result['supported'])
            self.assertIn('macOS', result['reason'])
        finally:
            claude_sandbox.availability.cache_clear()

    def test_a_timed_out_probe_is_unavailable_and_cached(self):
        import claude_sandbox
        import subprocess
        from unittest import mock
        claude_sandbox.availability.cache_clear()
        try:
            with mock.patch.object(claude_sandbox.sys, 'platform', 'darwin'), \
                    mock.patch.object(claude_sandbox, 'SANDBOX') as sandbox, \
                    mock.patch.object(claude_sandbox, '_verify_unlinked_trees'), \
                    mock.patch.object(claude_sandbox, 'run_bounded', side_effect=subprocess.TimeoutExpired('fixture', 10)) as run:
                sandbox.is_file.return_value = True
                first = claude_sandbox.availability()
                second = claude_sandbox.availability()
            self.assertFalse(first['supported'])
            self.assertEqual(first, second)
            self.assertEqual(run.call_count, 1)
        finally:
            claude_sandbox.availability.cache_clear()

    @unittest.skipUnless(DIRECTORY_DESCRIPTORS, 'Hardlink scan requires POSIX directory descriptors.')
    def test_scan_limits_fail_closed_before_launch(self):
        import claude_sandbox
        import sys
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state = [top / name for name in ('project', 'profile', 'scratch', 'state')]
            for folder in (root, profile, scratch, state):folder.mkdir()
            for number in range(4):(root / str(number)).write_text('plain')
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}):
                with mock.patch.object(claude_sandbox, 'MAX_SCAN_ENTRIES', 3):
                    with self.assertRaisesRegex(ValueError, 'entry limit'):
                        claude_sandbox.wrap([sys.executable], root, profile, scratch)
                (root / 'child').mkdir()
                with mock.patch.object(claude_sandbox, 'MAX_SCAN_DIRS', 3):
                    with self.assertRaisesRegex(ValueError, 'directory limit'):
                        claude_sandbox.wrap([sys.executable], root, profile, scratch)

    @unittest.skipUnless(DIRECTORY_DESCRIPTORS, 'Hardlink scan requires POSIX directory descriptors.')
    def test_scan_does_not_follow_symlinks_or_repair_user_files(self):
        import claude_sandbox
        import sys
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state, outside = [top / name for name in ('project', 'profile', 'scratch', 'state', 'outside')]
            for folder in (root, profile, scratch, state, outside):folder.mkdir()
            source = outside / 'source'; source.write_text('outside')
            os.link(source, outside / 'hardlinked-file')
            link = profile / 'external'; link.symlink_to(outside, target_is_directory=True)
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}):
                wrapped = claude_sandbox.wrap([sys.executable], root, profile, scratch)
            self.assertEqual(wrapped[0], str(claude_sandbox.SANDBOX))
            self.assertTrue(link.is_symlink())
            self.assertEqual(source.read_text(), 'outside')
            self.assertEqual(source.stat().st_nlink, 2)

    @unittest.skipUnless(DIRECTORY_DESCRIPTORS, 'Hardlink scan requires POSIX directory descriptors.')
    def test_unreadable_tree_is_not_treated_as_verified(self):
        import claude_sandbox
        import sys
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state = [top / name for name in ('project', 'profile', 'scratch', 'state')]
            for folder in (root, profile, scratch, state):folder.mkdir()
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}), \
                    mock.patch.object(claude_sandbox.os, 'scandir', side_effect=PermissionError('fixture denied')):
                with self.assertRaisesRegex(ValueError, 'could not be verified'):
                    claude_sandbox.wrap([sys.executable], root, profile, scratch)

    def test_global_claude_config_cannot_be_a_preexisting_private_hardlink(self):
        import claude_sandbox
        import sys
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state, home = [top / name for name in ('project', 'profile', 'scratch', 'state', 'home')]
            for folder in (root, profile, scratch, state, home):folder.mkdir()
            private = state / 'decision-evidence'; private.write_text('editor-private-evidence')
            alias = home / '.claude.json'; os.link(private, alias)
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}), \
                    mock.patch.object(claude_sandbox, '_verify_unlinked_trees'), \
                    mock.patch.object(claude_sandbox.Path, 'home', return_value=home):
                with self.assertRaisesRegex(ValueError, 'hardlink'):
                    claude_sandbox.wrap([sys.executable], root, profile, scratch)
            self.assertEqual(private.read_text(), 'editor-private-evidence')
            self.assertTrue(alias.exists())
            self.assertEqual(alias.stat().st_ino, private.stat().st_ino)

    def test_login_keychain_cannot_be_a_preexisting_private_hardlink(self):
        import claude_sandbox
        import sys
        from unittest import mock
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state, home = [top / name for name in ('project', 'profile', 'scratch', 'state', 'home')]
            for folder in (root, profile, scratch, state, home):folder.mkdir()
            private = state / 'decision-evidence'; private.write_text('editor-private-evidence')
            alias = home / 'Library/Keychains/login.keychain-db'; alias.parent.mkdir(parents=True)
            os.link(private, alias)
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}), \
                    mock.patch.object(claude_sandbox, '_verify_unlinked_trees'), \
                    mock.patch.object(claude_sandbox.Path, 'home', return_value=home):
                with self.assertRaisesRegex(ValueError, 'hardlink'):
                    claude_sandbox.wrap([sys.executable], root, profile, scratch)
            self.assertEqual(private.read_text(), 'editor-private-evidence')

    def test_login_keychain_cannot_point_into_editor_private_state(self):
        import claude_sandbox
        import sys
        from unittest import mock
        if not DIRECTORY_DESCRIPTORS:
            self.skipTest('Directory descriptors are unavailable')
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state, home = [top / name for name in ('project', 'profile', 'scratch', 'state', 'home')]
            for folder in (root, profile, scratch, state, home):folder.mkdir()
            private = state / 'decision-evidence'; private.write_text('editor-private-evidence')
            alias = home / 'Library/Keychains/login.keychain-db'; alias.parent.mkdir(parents=True)
            alias.symlink_to(private)
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}), \
                    mock.patch.object(claude_sandbox.Path, 'home', return_value=home):
                with self.assertRaisesRegex(ValueError, 'private state'):
                    claude_sandbox.wrap([sys.executable], root, profile, scratch)


class NativeClaudeSandbox(unittest.TestCase):
    def test_system_timezone_database_is_readable(self):
        import claude_sandbox
        import subprocess
        import sys
        from unittest import mock
        if sys.platform != 'darwin' or not claude_sandbox.SANDBOX.is_file():
            self.skipTest('The native CLI boundary requires macOS sandbox-exec.')
        localtime = Path('/etc/localtime').resolve()
        if not localtime.is_relative_to('/private/var/db/timezone'):
            self.skipTest('The system timezone is outside the macOS timezone database.')
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state = [top / name for name in ('project', 'profile', 'scratch', 'state')]
            for folder in (root, profile, scratch, state):folder.mkdir()
            command = [sys.executable, '-I', '-B', '-c',
                       "from pathlib import Path; assert Path('/etc/localtime').read_bytes().startswith(b'TZif')"]
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}):
                wrapped = claude_sandbox.wrap(command, root, profile, scratch)
                result = subprocess.run(wrapped, cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_claude_login_keychain_is_read_only_and_file_scoped(self):
        import claude_sandbox
        import subprocess
        import sys
        from unittest import mock
        if sys.platform != 'darwin' or not claude_sandbox.SANDBOX.is_file():
            self.skipTest('The native CLI boundary requires macOS sandbox-exec.')
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state, home = [top / name for name in ('project', 'profile', 'scratch', 'state', 'home')]
            for folder in (root, profile, scratch, state, home):folder.mkdir()
            keychains = home / 'Library/Keychains'; keychains.mkdir(parents=True)
            login = keychains / 'login.keychain-db'; login.write_text('synthetic login store')
            other = keychains / 'other.keychain-db'; other.write_text('other private store')
            code = '''import errno,pathlib,sys
login,other=map(pathlib.Path,sys.argv[1:])
assert login.read_text() == 'synthetic login store'
for operation in [lambda: login.write_text('bad'), lambda: other.read_bytes()]:
    try: operation()
    except OSError as error: assert error.errno in (errno.EACCES,errno.EPERM)
    else: raise AssertionError('Unexpected keychain access')
'''
            command = [sys.executable, '-I', '-B', '-c', code, str(login), str(other)]
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}), \
                    mock.patch.object(claude_sandbox.Path, 'home', return_value=home):
                wrapped = claude_sandbox.wrap(command, root, profile, scratch)
                result = subprocess.run(wrapped, cwd=root, capture_output=True, text=True, timeout=10)
                for provider in ('codex', 'deepcode'):
                    other_provider = claude_sandbox.wrap(command, root, profile, scratch, provider=provider)
                    self.assertNotIn('com.apple.SecurityServer', other_provider[2])
                    denied = subprocess.run(other_provider, cwd=root, capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(denied.returncode, 0)
                    self.assertIn('PermissionError', denied.stderr)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(login.read_text(), 'synthetic login store')

    def test_system_preference_cache_is_read_only_and_name_scoped(self):
        import claude_sandbox
        import ctypes
        import mmap
        import subprocess
        import sys
        import uuid
        from unittest import mock
        if sys.platform != 'darwin' or not claude_sandbox.SANDBOX.is_file():
            self.skipTest('The native CLI boundary requires macOS sandbox-exec.')
        libc = ctypes.CDLL(None, use_errno=True)
        # On macOS, mode is variadic; ARM64 passes it differently from fixed args.
        libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
        libc.shm_unlink.argtypes = [ctypes.c_char_p]
        suffix = uuid.uuid4().hex[:8]
        names = [('apple.cfprefs.prism-' + suffix).encode(), ('prism-private-' + suffix).encode()]
        created = []
        try:
            for name in names:
                fd = libc.shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, ctypes.c_uint(0o644))
                self.assertGreaterEqual(fd, 0, ctypes.get_errno())
                created.append(name)
                try:
                    self.assertTrue(os.fstat(fd).st_mode & 0o400, 'fixture must be owner-readable')
                    os.ftruncate(fd, 7)
                    with mmap.mmap(fd, 7) as data:data[:] = b'fixture'
                finally: os.close(fd)
            with tempfile.TemporaryDirectory() as temporary:
                top = Path(temporary).resolve()
                root, profile, scratch, state = [top / name for name in ('project', 'profile', 'scratch', 'state')]
                for folder in (root, profile, scratch, state):folder.mkdir()
                code = '''import ctypes,errno,mmap,os,sys
libc=ctypes.CDLL(None,use_errno=True)
libc.shm_open.argtypes=[ctypes.c_char_p,ctypes.c_int]
name,other=(value.encode() for value in sys.argv[1:])
fd=libc.shm_open(name,os.O_RDONLY)
assert fd >= 0, ctypes.get_errno()
with mmap.mmap(fd,7,access=mmap.ACCESS_READ) as data:assert data[:] == b'fixture'
os.close(fd)
for candidate,flags in [(name,os.O_RDWR),(other,os.O_RDONLY)]:
    fd=libc.shm_open(candidate,flags)
    assert fd == -1 and ctypes.get_errno() in (errno.EACCES,errno.EPERM)
'''
                command = [sys.executable, '-I', '-B', '-c', code, *[name.decode() for name in names]]
                with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}):
                    wrapped = claude_sandbox.wrap(command, root, profile, scratch, provider='codex')
                    result = subprocess.run(wrapped, cwd=root, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            for name in created:libc.shm_unlink(name)

    def test_native_runtime_checks_writes_private_reads_and_both_loopbacks(self):
        import claude_sandbox
        import sys
        if sys.platform != 'darwin' or not claude_sandbox.SANDBOX.is_file():
            self.skipTest('The native Claude boundary requires macOS sandbox-exec.')
        claude_sandbox.availability.cache_clear()
        result = claude_sandbox.availability()
        self.assertTrue(result['supported'], result['reason'])
        self.assertIn('loopback', result['reason'])

    def test_preexisting_profile_hardlink_cannot_modify_the_original(self):
        import claude_sandbox
        import subprocess
        import sys
        from unittest import mock
        if sys.platform != 'darwin' or not claude_sandbox.SANDBOX.is_file():
            self.skipTest('The native Claude boundary requires macOS sandbox-exec.')
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state = [top / name for name in ('project', 'profile', 'scratch', 'state')]
            for folder in (root, profile, scratch, state):folder.mkdir()
            original = root / 'main.tex'; original.write_text('original')
            alias = profile / 'preexisting-alias'; os.link(original, alias)
            command = [sys.executable, '-I', '-B', '-c',
                       'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text("unapproved")', str(alias)]
            with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}):
                try:
                    wrapped = claude_sandbox.wrap(command, root, profile, scratch)
                except ValueError as error:
                    self.assertIn('hardlink', str(error).lower())
                else:
                    result = subprocess.run(wrapped, cwd=root,
                        env=claude_sandbox.environment(dict(os.environ), scratch),
                        capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(original.read_text(), 'original')
            self.assertTrue(alias.exists(), 'unsafe aliases are rejected, never deleted')
            self.assertEqual(alias.stat().st_ino, original.stat().st_ino)

    def test_preexisting_private_evidence_aliases_cannot_be_read(self):
        import claude_sandbox
        import subprocess
        import sys
        from unittest import mock
        if sys.platform != 'darwin' or not claude_sandbox.SANDBOX.is_file():
            self.skipTest('The native Claude boundary requires macOS sandbox-exec.')
        for location in ('project', 'profile', 'scratch'):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temporary:
                top = Path(temporary).resolve()
                root, profile, scratch, state = [top / name for name in ('project', 'profile', 'scratch', 'state')]
                for folder in (root, profile, scratch, state):folder.mkdir()
                private = state / 'decision-evidence'; private.write_text('editor-private-evidence')
                alias = top / location / 'preexisting-alias'; os.link(private, alias)
                command = [sys.executable, '-I', '-B', '-c',
                           'import pathlib,sys;print(pathlib.Path(sys.argv[1]).read_text())', str(alias)]
                output = ''
                with mock.patch.dict(os.environ, {'PRISM_STATE_DIR': str(state)}):
                    try:
                        wrapped = claude_sandbox.wrap(command, root, profile, scratch)
                    except ValueError as error:
                        self.assertIn('hardlink', str(error).lower())
                    else:
                        result = subprocess.run(wrapped, cwd=root,
                            env=claude_sandbox.environment(dict(os.environ), scratch),
                            capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        output = result.stdout
                self.assertNotIn('editor-private-evidence', output)
                self.assertEqual(private.read_text(), 'editor-private-evidence')
                self.assertTrue(alias.exists(), 'unsafe aliases are rejected, never deleted')


if __name__ == "__main__":
    unittest.main()
