"""The native write boundary around controlled CLIs, separate from editor-owned tools.

The CLI reads the project and uses its own profile, but only the trusted editor
may modify source or read editor decision evidence. MCP tools travel over stdio;
loopback HTTP cannot impersonate an editor message.
"""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile

from proc import run as run_bounded
import registry

SANDBOX = Path("/usr/bin/sandbox-exec")
MAX_SCAN_ENTRIES = 50000
MAX_SCAN_DIRS = 5000


def environment(base_env: dict, scratch: Path) -> dict:
    """Keep auth and HOME intact while confining temporary files to this turn."""
    temporary = Path(scratch).resolve() / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    return {**base_env, "TMPDIR": str(temporary), "TMP": str(temporary),
            "TEMP": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}



def _verify_unlinked_trees(roots):
    """Path rules cannot distinguish pre-existing hardlink aliases of one inode.

    Check untrusted project/profile/scratch metadata before launch. Do not follow
    symlinks or inspect file contents, and never repair or delete user files.
    System and installed runtime roots remain part of the trusted environment.
    """
    entries_seen = directories_seen = 0
    selected = []
    for root in sorted(set(roots), key=lambda path: len(path.parts)):
        if any(root.is_relative_to(parent) for parent in selected):
            continue
        selected.append(root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    for root in selected:
        stack = []
        try:
            descriptor = os.open(root, flags)
            try:
                root_entries = os.scandir(descriptor)
            except BaseException:
                os.close(descriptor)
                raise
            stack.append((descriptor, root_entries, root))
            directories_seen += 1
            if directories_seen > MAX_SCAN_DIRS:
                raise ValueError("The controlled CLI hardlink scan exceeded its directory limit.")
            while stack:
                descriptor, children, directory = stack[-1]
                try:
                    entry = next(children)
                except StopIteration:
                    children.close()
                    os.close(descriptor)
                    stack.pop()
                    continue
                entries_seen += 1
                if entries_seen > MAX_SCAN_ENTRIES:
                    raise ValueError("The controlled CLI hardlink scan exceeded its entry limit.")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    raise ValueError("The controlled CLI cannot use a tree with a pre-existing hardlink: "
                                     + str(directory / entry.name))
                if not stat.S_ISDIR(info.st_mode):
                    continue
                directories_seen += 1
                if directories_seen > MAX_SCAN_DIRS:
                    raise ValueError("The controlled CLI hardlink scan exceeded its directory limit.")
                child = os.open(entry.name, flags, dir_fd=descriptor)
                try:
                    current = os.fstat(child)
                    if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                        raise ValueError("The controlled CLI tree changed during its hardlink scan.")
                    child_entries = os.scandir(child)
                except BaseException:
                    os.close(child)
                    raise
                stack.append((child, child_entries, directory / entry.name))
        except OSError as error:
            raise ValueError("The controlled CLI tree could not be verified before launch: " + str(error)) from error
        finally:
            for descriptor, children, _directory in reversed(stack):
                children.close()
                os.close(descriptor)

def _profile(cmd, root, profile, scratch, *, private=(), provider='claude', runtime_paths=()):
    root, profile, scratch = (Path(p).expanduser().resolve() for p in (root, profile, scratch))
    state = registry.state_dir().resolve()
    for writable in (profile, scratch):
        if writable == root or writable.is_relative_to(root) or writable == state or writable.is_relative_to(state):
            raise ValueError("The controlled CLI profile and scratch must be outside the project and editor private state.")
    if root == state or root.is_relative_to(state):
        raise ValueError("The controlled CLI project must be outside editor private state.")
    _verify_unlinked_trees([root, profile, scratch])
    global_config = Path.home() / ".claude.json" if provider == 'claude' else None
    if global_config is not None:
        try:
            global_info = global_config.lstat()
        except FileNotFoundError:
            global_info = None
        except OSError as error:
            raise ValueError("The global Claude config could not be verified before launch: " + str(error)) from error
        if global_info is not None and stat.S_ISREG(global_info.st_mode) and global_info.st_nlink > 1:
            raise ValueError("Claude cannot read a global config with a pre-existing hardlink: " + str(global_config))
    executable = Path(shutil.which(str(cmd[0])) or cmd[0]).expanduser().resolve()
    code = Path(__file__).resolve().parent
    reads = [root, profile, scratch, code, executable.parent, Path(sys.prefix), Path(sys.base_prefix),
             Path(sys.executable).resolve().parent, Path("/System"), Path("/Library"),
             Path("/usr"), Path("/opt"), Path("/private/etc"), Path("/etc")]
    reads += [Path(path).expanduser().resolve() for path in runtime_paths]
    quote = lambda path: json.dumps(str(path), ensure_ascii=False)
    rules = ["(version 1)", "(deny default)",
             "(allow process-exec process-fork)", "(allow sysctl-read)",
             "(allow file-read-metadata)", '(allow file-read* (literal "/"))',
             "(allow file-read* " + " ".join(f"(subpath {quote(p)})" for p in reads) + ")",
             '(allow file-read* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
             *([f"(allow file-read* (literal {quote(global_config)}))"] if global_config is not None else []),
             f"(allow file-write* (subpath {quote(profile)}) (subpath {quote(scratch)}) (literal \"/dev/null\"))",
             f"(deny file-write* (subpath {quote(root)}))",
             # Claude's normal auth/session profile is distinct from editor-issued
             # human evidence. A child cannot read or replace that evidence.
             "(deny file-read* " + " ".join(f"(subpath {quote(p)})" for p in [state, *private]) + ")",
             "(deny file-write* " + " ".join(f"(subpath {quote(p)})" for p in [state, *private]) + ")",
             "(deny network-bind)", "(deny network-inbound)",
             # This is the system profile's non-loopback remote filter. The
             # editor listens on loopback, and all trusted tools use stdio.
             '(allow network-outbound (require-all (require-any (remote tcp "*:*") (remote udp "*:*") '
             '(literal "/private/var/run/mDNSResponder")) (require-not (remote ip "localhost:*"))))',
             # A specific deny is required as well: the broader remote allow can
             # otherwise admit TCP loopback despite its require-not condition.
             '(deny network-outbound (remote ip "localhost:*"))',
             # Authentication uses system trust/keychain services; this permits
             # service lookup, not arbitrary Apple events or host automation.
             '(allow mach-lookup (global-name "com.apple.securityd") (global-name "com.apple.trustd") '
             '(global-name "com.apple.trustd.agent") (global-name "com.apple.cfprefsd.agent") '
             '(global-name "com.apple.cfprefsd.daemon") (global-name "com.apple.networkd") '
             '(global-name "com.apple.system.opendirectoryd.libinfo"))']
    return "\n".join(rules)


def wrap(cmd: list[str], root: Path, profile: Path, scratch: Path, *, provider='claude', runtime_paths=()) -> list[str]:
    """Run the CLI behind the OS boundary; there is no unguarded fallback."""
    if not isinstance(cmd, list) or not cmd or not all(isinstance(part, str) for part in cmd):
        raise ValueError("The controlled CLI command must be a nonempty argv list.")
    return [str(SANDBOX), "-p", _profile(cmd, root, profile, scratch, provider=provider,
                                      runtime_paths=runtime_paths), *cmd]


_PROBE = r"""
import errno, json, os, pathlib, socket, sys
root, profile, scratch, private = map(pathlib.Path, sys.argv[1:5])
ports = json.loads(sys.argv[5])
original = root / 'main.tex'
assert original.read_text() == 'original'
assert (profile / 'credential').read_text() == 'fixture'
(profile / 'session').write_text('saved')
(scratch / 'scratch').write_text('saved')
checks = {}
def blocked(name, operation):
    try:
        operation()
    except OSError as error:
        checks[name] = error.errno in (errno.EPERM, errno.EACCES)
    else:
        checks[name] = False
blocked('source_write', lambda: original.write_text('bad'))
blocked('private_read', lambda: private.read_bytes())
blocked('private_write', lambda: private.write_text('bad'))
(scratch / 'source-link').symlink_to(original)
blocked('source_symlink_write', lambda: (scratch / 'source-link').write_text('bad'))
def hardlink():
    alias = scratch / 'source-hardlink'
    os.link(original, alias)
    alias.write_text('bad')
blocked('source_hardlink_write', hardlink)
def connect(family, host, port):
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        sock.connect((host, port))
        sock.sendall(b'POST /api/agent HTTP/1.0\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}')
blocked('ipv4_loopback_post', lambda: connect(socket.AF_INET, '127.0.0.1', ports[0]))
blocked('ipv6_loopback_post', lambda: connect(socket.AF_INET6, '::1', ports[1]))
print(json.dumps(checks), flush=True)
sys.exit(0 if all(checks.values()) else 3)
"""


# Keep the verified native boundary cached, rather than starting a probe whenever
# the browser polls its provider list. No user prompt or real Claude process runs.
@functools.lru_cache(maxsize=3)
def availability(provider='claude') -> dict:
    label = {'codex': 'Codex', 'deepcode': 'Deep Code'}.get(provider, 'Claude')
    if sys.platform != "darwin" or not SANDBOX.is_file():
        return {"supported": False,
                "reason": f"Guarded {label} needs a verified macOS sandbox-exec runtime."}
    try:
        with tempfile.TemporaryDirectory(prefix="prism-claude-probe-") as temporary:
            top = Path(temporary).resolve()
            root, profile, scratch, state = [top / name for name in ("project", "profile", "scratch", "private")]
            for folder in (root, profile, scratch, state):
                folder.mkdir()
            original, private = root / "main.tex", state / "decision-evidence"
            original.write_text("original")
            private.write_text("private")
            (profile / "credential").write_text("fixture")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as ipv4, \
                    socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as ipv6:
                ipv4.bind(("127.0.0.1", 0)); ipv4.listen(1)
                ipv6.bind(("::1", 0)); ipv6.listen(1)
                ports = [ipv4.getsockname()[1], ipv6.getsockname()[1]]
                command = [sys.executable, "-I", "-B", "-c", _PROBE,
                           str(root), str(profile), str(scratch), str(private), json.dumps(ports)]
                policy = _profile(command, root, profile, scratch, private=[state], provider=provider)
                result = run_bounded([str(SANDBOX), "-p", policy, *command], timeout=10,
                                     cwd=root, env=environment(dict(os.environ), scratch), text=True,
                                     encoding="utf-8", errors="replace")
            checks = json.loads(result.stdout.strip()) if result.stdout.strip().startswith("{") else {}
            if (result.returncode == 0 and all(checks.get(name) is True for name in (
                        "source_write", "private_read", "private_write", "source_symlink_write",
                        "source_hardlink_write", "ipv4_loopback_post", "ipv6_loopback_post"))
                    and original.read_text() == "original" and private.read_text() == "private"
                    and (profile / "session").read_text() == "saved"
                    and (scratch / "scratch").read_text() == "saved"):
                return {"supported": True, "reason": "macOS source-write, private-state and IPv4/IPv6 loopback restrictions verified."}
            detail = (result.stderr or result.stdout).strip()[:400] or f"probe exit {result.returncode}"
            return {"supported": False, "reason": f"Guarded {label} is unavailable: " + detail}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        return {"supported": False, "reason": f"Guarded {label} is unavailable: " + str(error)}
