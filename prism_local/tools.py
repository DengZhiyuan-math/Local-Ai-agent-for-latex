"""The programs Prism needs besides itself: git, the GitHub CLI (gh), LaTeX (a TeX
distribution, or Tectonic) and, if you want the AI panel, Claude Code. Whether they are
there, and installing them from the Home page for someone who has never used any of them.

How each is installed:
- git: winget on Windows. Elsewhere the page says in a sentence how to.
- gh: winget on Windows; on macOS and Linux Prism downloads it from GitHub's release
  (checked against the release's checksums) into its own folder, bin_dir().
- tectonic: on every computer it has a download for, Prism downloads it into bin_dir().
- tex (a full TeX distribution): MiKTeX with winget on Windows. Elsewhere a sentence says
  how to: MacTeX's installer asks for the administrator's password, which a program
  running in the background cannot be given (Quick start, Tectonic, works meanwhile).
- claude: Claude Code's own installer (PowerShell on Windows, curl and bash elsewhere).

A program installed while Prism runs is found without a restart: bin_dir(), ~/.local/bin
and, on Windows, the PATH Windows keeps (in the registry) are added to this process's
PATH again (refresh_path).

    status()          # what the Home page shows: see status()
    install("git")    # in the background; status()["installing"]["git"] says how it goes
    claude_login()    # a terminal window running claude, to log in once
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

import proc
from errors import Refused, say

PROGRAMS = ("git", "gh", "tectonic", "tex", "claude")
PACKAGES = {"git": "Git.Git", "gh": "GitHub.cli", "tex": "MiKTeX.MiKTeX"}      # winget's ids
LABELS = {"git": "git", "gh": "The GitHub CLI", "tectonic": "Tectonic", "tex": "LaTeX",
          "claude": "Claude Code"}
INSTALL_TIME = 900                                          # a slow download, an installer
TEX_TIME = 3600                                             # a whole TeX distribution
ENGINES = ("pdflatex", "xelatex", "lualatex")
# Where their installers put them, also when Windows' PATH has not reached this process.
KNOWN_DIRS = {"git": [r"%ProgramFiles%\Git\cmd", r"%LOCALAPPDATA%\Programs\Git\cmd"],
              "gh": [r"%ProgramFiles%\GitHub CLI", r"%LOCALAPPDATA%\Programs\GitHub CLI"]}
USER_BIN = Path.home() / ".local" / "bin"                   # where Claude Code installs itself

# Downloads. GITHUB_API is a constant so that the tests can stand in for GitHub.
GITHUB_API = "https://api.github.com"
TECTONIC_RELEASES = "/repos/tectonic-typesetting/tectonic/releases"
GH_RELEASE = "/repos/cli/cli/releases/latest"
USER_AGENT = "prism-local"
NET_TIMEOUT = 30                    # seconds one network read may wait
DOWNLOAD_TIME = 600                 # seconds one whole download may take
MAX_DOWNLOAD = 300 * 1024 * 1024    # bytes: no release file of these is near this

INSTALL: dict[str, dict] = {}       # program -> {"state": running|done|failed, "error", "details"}
_lock = threading.Lock()


# ---------------------------------------------------------------- where programs are

def bin_dir() -> Path:
    """The folder of programs Prism downloads itself (Tectonic; gh on macOS and Linux)."""
    import registry
    return registry.state_dir() / "bin"


def refresh_path() -> None:
    """Prism's own bin_dir() and ~/.local/bin (Claude Code's), and on Windows what
    Windows' PATH says now (the system's and yours, in the registry) and where git and gh
    install themselves, added to this process's PATH: a program installed since Prism
    started is found without restarting it. Folders that are not there are left out."""
    parts = []
    if os.name == "nt":
        parts += _registry_path() + [os.path.expandvars(d) for dirs in KNOWN_DIRS.values() for d in dirs]
    parts += [str(bin_dir()), str(USER_BIN)]
    have = os.environ.get("PATH", "").split(os.pathsep)
    seen = {os.path.normcase(p.rstrip("\\/")) for p in have if p}
    add = []
    for p in parts:
        key = os.path.normcase(p.strip().rstrip("\\/"))
        if key and key not in seen and Path(p.strip()).is_dir():
            seen.add(key)
            add.append(p.strip())
    if add:
        os.environ["PATH"] = os.pathsep.join([*have, *add])


def _registry_path() -> list[str]:
    """The folders of Windows' PATH as it is now: the system's, then yours."""
    parts: list[str] = []
    try:
        import winreg
    except ImportError:
        return parts
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(hive, key) as k:
                parts += os.path.expandvars(str(winreg.QueryValueEx(k, "Path")[0])).split(";")
        except OSError:
            pass
    return parts


def find(name: str) -> str | None:
    """Where the program `name` ("git", "gh", "tectonic") is, or None."""
    refresh_path()
    return shutil.which(name)


def tex_engine() -> str | None:
    """A TeX engine (pdflatex, xelatex, lualatex): on PATH, or in a folder where TeX
    distributions put them (build.tex_dirs), as builds find them. None without one."""
    refresh_path()
    for e in ENGINES:
        if shutil.which(e):
            return shutil.which(e)
    import build
    for d in build.tex_dirs():
        for e in ENGINES:
            found = shutil.which(e, path=str(d))
            if found:
                return found
    return None


def claude_path() -> str | None:
    """Where Claude Code (claude) is, as the AI panel finds it, or None."""
    refresh_path()
    from backend_claude import claude_bin
    return claude_bin()


def _present(name: str) -> str | None:
    if name == "tex":
        return tex_engine()
    if name == "claude":
        return claude_path()
    return find(name)


def _system() -> str:
    """"windows", "macos", "linux", or another name for anything else."""
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux" if sys.platform.startswith("linux") else sys.platform


def _machine() -> str:
    """This computer's processor: "x86_64" or "aarch64" (or what else it says it is)."""
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(m, m)


def winget() -> str | None:
    return shutil.which("winget") if _system() == "windows" else None


def _claude_installer() -> list[str] | None:
    """The command that runs Claude Code's own installer here, or None without the
    programs it needs (PowerShell; bash and curl)."""
    if _system() == "windows":
        ps = shutil.which("powershell") or shutil.which("pwsh")
        return [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                "irm https://claude.ai/install.ps1 | iex"] if ps else None
    bash = shutil.which("bash")
    return [bash, "-c", "curl -fsSL https://claude.ai/install.sh | bash"] \
        if bash and shutil.which("curl") else None


# ---------------------------------------------------------------- what the page shows

def how_to(name: str) -> str:
    """How to install `name` by hand, in a sentence, where Prism cannot."""
    system = _system()
    if name == "tectonic":
        return "Download Tectonic from https://tectonic-typesetting.github.io, then come back here."
    if name == "claude":
        if system == "windows":
            return ("Open PowerShell and run: irm https://claude.ai/install.ps1 | iex "
                    "(see https://claude.com/claude-code), then come back here.")
        return ("Open a terminal and run: curl -fsSL https://claude.ai/install.sh | bash "
                "(see https://claude.com/claude-code), then come back here.")
    if system == "windows":
        return {"git": "Install git from https://git-scm.com/download/win, then come back here.",
                "gh": "Install the GitHub CLI from https://cli.github.com, then come back here.",
                "tex": "Install MiKTeX from https://miktex.org/download, then come back here."}[name]
    if system == "macos":
        return {"git": "Open Terminal and run: xcode-select --install (Apple's developer tools include git).",
                "gh": "Install the GitHub CLI from https://cli.github.com (or, with Homebrew: brew install gh).",
                "tex": "Install MacTeX from https://tug.org/mactex/ (a large download; Quick start "
                       "works meanwhile), then come back here."}[name]
    return {"git": "Install git with your system's software installer (the package is called git).",
            "gh": "Install the GitHub CLI from https://cli.github.com (the package is called gh).",
            "tex": "Install TeX Live with your system's software installer. On Debian or Ubuntu: "
                   "sudo apt install texlive-latex-extra texlive-xetex texlive-luatex "
                   "texlive-bibtex-extra biber. On Fedora: sudo dnf install texlive-scheme-medium biber. "
                   "On Arch: sudo pacman -S texlive-basic texlive-latexextra texlive-xetex "
                   "texlive-luatex texlive-bibtexextra biber."}[name]


def can_install() -> dict:
    """Which programs Prism can install itself on this computer."""
    system = _system()
    return {"git": bool(winget()),
            "gh": bool(winget()) if system == "windows" else _gh_names(system, _machine(), "") is not None,
            "tectonic": _tectonic_target(system, _machine()) is not None,
            "tex": bool(winget()),
            "claude": _claude_installer() is not None}


def status() -> dict:
    """What the Home page's Getting started panel shows: where each program is (None
    when it is not there), what Prism can install, how each install goes, and how to
    install each by hand."""
    with _lock:
        installing = {k: dict(v) for k, v in INSTALL.items()}
    return {"git": find("git"), "gh": find("gh"),
            "tex": {"engine": tex_engine(), "tectonic": find("tectonic")},
            "claude": claude_path(),
            "can_install": can_install(), "installing": installing,
            "how": {name: how_to(name) for name in PROGRAMS}}


# ---------------------------------------------------------------- installing

class _Failed(Exception):
    """An install that did not work: `error` for the page, `details` for Details."""

    def __init__(self, error: str, details: str | None = None):
        super().__init__(error)
        self.error, self.details = error, details


def install(name: str) -> dict:
    """Install `name` (one of PROGRAMS) in the background; its state at once. Refused,
    saying how to install it by hand, where Prism cannot."""
    if name not in PROGRAMS:
        raise Refused("unknown program")
    # Claimed with the same look that finds it free: two clicks at once start one install.
    with _lock:
        if INSTALL.get(name, {}).get("state") == "running":
            return dict(INSTALL[name])
        before = INSTALL.get(name)
        INSTALL[name] = {"state": "running", "error": None, "details": None, "at": time.time()}
        running = dict(INSTALL[name])
    try:
        if _present(name):
            with _lock:
                INSTALL[name] = {"state": "done", "error": None, "details": None}
                return dict(INSTALL[name])
        job = _job(name)
    except BaseException:
        _give_back(name, before)
        raise
    if job is None:
        _give_back(name, before)
        raise Refused(how_to(name))
    threading.Thread(target=_install, args=(name, job), daemon=True).start()
    return running


def _job(name: str) -> Callable[[], dict] | None:
    """What installs `name` here, or None where Prism cannot."""
    system = _system()
    if name == "tectonic":
        return _install_tectonic if _tectonic_target(system, _machine()) else None
    if name == "claude":
        cmd = _claude_installer()
        return (lambda: _run_installer(name, cmd, INSTALL_TIME)) if cmd else None
    if name == "gh" and system != "windows":
        return _install_gh if _gh_names(system, _machine(), "") else None
    exe = winget()
    if not exe:
        return None
    return lambda: _run_installer(name, [exe, "install", "--id", PACKAGES[name], "--exact", "--silent",
                                         "--accept-package-agreements", "--accept-source-agreements",
                                         "--disable-interactivity"],
                                  TEX_TIME if name == "tex" else INSTALL_TIME)


def _give_back(name: str, before: dict | None) -> None:
    """Nothing was started after all: `name`'s state as it was before install() claimed it."""
    with _lock:
        if before is None:
            INSTALL.pop(name, None)
        else:
            INSTALL[name] = before


def _install(name: str, job: Callable[[], dict]) -> None:
    label = LABELS[name]
    try:
        result = job()
    except _Failed as e:
        result = {"state": "failed", "error": e.error, "details": e.details}
    except (OSError, subprocess.SubprocessError) as e:
        result = {"state": "failed", "error": f"{label} could not be installed. " + how_to(name), "details": say(e)}
    except Exception as e:           # a bad archive, or a bug: never left "running"
        result = {"state": "failed", "error": f"{label} could not be installed. " + how_to(name),
                  "details": f"{type(e).__name__}: {e}"}
    if result["state"] == "done":
        if name == "gh":
            import github
            github.GITHUB.status(refresh=True)
        if name == "claude":
            import backend_claude
            backend_claude._bin_cache.clear()        # looked up again, not "not there" for a minute
    with _lock:
        INSTALL[name] = result


def _run_installer(name: str, cmd: list[str], timeout: float) -> dict:
    """Run an installer (winget, Claude Code's) and say whether `name` is there now."""
    label = LABELS[name]
    r = proc.run(cmd, timeout=timeout, text=True, encoding="utf-8", errors="replace")
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if name == "claude":
        import backend_claude
        backend_claude._bin_cache.clear()
    if _present(name):
        return {"state": "done", "error": None, "details": None}
    return {"state": "failed", "details": out[-600:] or f"{Path(cmd[0]).stem} exited with {r.returncode}",
            "error": f"{label} could not be installed. Try again, or: " + how_to(name)}


# ---------------------------------------------------------------- downloads

def _opener() -> urllib.request.OpenerDirector:
    """urllib's, with the proxy settings as they are now."""
    return urllib.request.build_opener()


def _fetch(url: str, out=None, limit: int = MAX_DOWNLOAD) -> bytes | None:
    """GET `url`: its body, or written to the open file `out` (then None). Each read waits
    at most NET_TIMEOUT seconds, the whole at most DOWNLOAD_TIME. Raises OSError (also
    urllib's errors, which are OSErrors) when it does not work."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    deadline, got, chunks = time.monotonic() + DOWNLOAD_TIME, 0, []
    try:
        answer = _opener().open(req, timeout=NET_TIMEOUT)
    except urllib.error.HTTPError as e:
        e.close()
        raise
    with answer as r:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            got += len(chunk)
            if got > limit:
                raise OSError(f"{url} is larger than {limit} bytes")
            if time.monotonic() > deadline:
                raise TimeoutError(f"{url} took longer than {DOWNLOAD_TIME} seconds to download")
            if out is None:
                chunks.append(chunk)
            else:
                out.write(chunk)
    return b"".join(chunks) if out is None else None


def _api(path: str, label: str):
    """GitHub's answer (JSON) for `path`, or _Failed in a sentence."""
    url = GITHUB_API.rstrip("/") + path
    try:
        return json.loads(_fetch(url, limit=10 * 1024 * 1024).decode("utf-8"))
    except (OSError, ValueError) as e:
        raise _Failed(f"{label} could not be downloaded: GitHub did not answer. Check your internet "
                      "connection and try again.", f"{url}: {say(e)}") from None


def _download(url: str, dest: Path, label: str) -> str:
    """`url` saved as `dest`; its SHA-256 (hex). _Failed in a sentence when it does not work."""
    try:
        with open(dest, "wb") as f:
            _fetch(url, f)
    except OSError as e:
        raise _Failed(f"{label} could not be downloaded. Check your internet connection and try again.",
                      f"{url}: {say(e)}") from None
    h = hashlib.sha256()
    with open(dest, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _safe_member(name: str) -> bool:
    """An archive's member name that stays inside the archive's folder."""
    parts = name.replace("\\", "/").split("/")
    return not name.startswith(("/", "\\")) and ".." not in parts and ":" not in parts[0]


def _extract(archive: Path, kind: str, wanted: Callable[[list[str]], bool], dest: Path) -> bool:
    """The first file of `archive` ("zip" or "tar.gz") whose path (as its parts) is
    `wanted`, written to `dest`. Only regular files, and only names that stay inside the
    archive. Whether one was found."""
    if kind == "zip":
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                is_link = (info.external_attr >> 16) & 0o170000 == 0o120000
                if info.is_dir() or is_link or not _safe_member(info.filename):
                    continue
                if wanted(info.filename.replace("\\", "/").split("/")):
                    with z.open(info) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
                    return True
        return False
    with tarfile.open(archive, "r:gz") as t:
        for m in t:
            if not m.isfile() or not _safe_member(m.name):
                continue
            if wanted(m.name.split("/")):
                src = t.extractfile(m)
                with src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                return True
    return False


def _place(name: str, url: str, kind: str, wanted: Callable[[list[str]], bool], program: str,
           checksum: str | None = None) -> Path:
    """Download the archive at `url` (for the program `name`), check it against `checksum`
    (SHA-256, if given), and put its `wanted` file into bin_dir() as `program`, runnable.
    Written to temporary files there and renamed into place: a half download is never the
    program."""
    label = LABELS[name]
    folder = bin_dir()
    folder.mkdir(parents=True, exist_ok=True)
    fd, archive = tempfile.mkstemp(dir=folder, prefix=f".{program}-", suffix=".download")
    os.close(fd)
    fd, part = tempfile.mkstemp(dir=folder, prefix=f".{program}-", suffix=".part")
    os.close(fd)
    try:
        got = _download(url, Path(archive), label)
        if checksum is not None and got.lower() != checksum.lower():
            raise _Failed(f"{label}'s download was not the file GitHub published for it, so it was not "
                          "installed. Try again.", f"{url}: SHA-256 {got}, expected {checksum}")
        try:
            found = _extract(Path(archive), kind, wanted, Path(part))
        except (zipfile.BadZipFile, tarfile.TarError, EOFError) as e:
            raise _Failed(f"{label}'s download could not be unpacked. Try again.", f"{url}: {e}") from None
        if not found:
            raise _Failed(f"{label}'s download did not contain the program. " + how_to(name),
                          f"{url}: no {program} in the archive")
        os.chmod(part, 0o755)
        target = folder / program
        os.replace(part, target)
    finally:
        for p in (archive, part):
            try:
                os.unlink(p)
            except OSError:
                pass
    refresh_path()
    return target


# Tectonic: https://github.com/tectonic-typesetting/tectonic/releases, tags "tectonic@0.15.0",
# files such as tectonic-0.15.0-x86_64-pc-windows-msvc.zip.
def _tectonic_target(system: str, machine: str) -> str | None:
    """The name Tectonic's release files give this computer, or None where there is none."""
    return {("windows", "x86_64"): "x86_64-pc-windows-msvc",
            ("macos", "x86_64"): "x86_64-apple-darwin", ("macos", "aarch64"): "aarch64-apple-darwin",
            ("linux", "x86_64"): "x86_64-unknown-linux-musl",
            ("linux", "aarch64"): "aarch64-unknown-linux-musl"}.get((system, machine))


def _tectonic_asset(releases: list, system: str, machine: str) -> dict | None:
    """The file to download, from GitHub's list of Tectonic's releases: the newest
    published (not a draft, not a prerelease) "tectonic@…" release that has one for this
    computer. {"name", "url", "kind"}, or None."""
    target = _tectonic_target(system, machine)
    if target is None:
        return None
    kind = "zip" if system == "windows" else "tar.gz"
    for rel in releases if isinstance(releases, list) else []:
        if rel.get("draft") or rel.get("prerelease") or not str(rel.get("tag_name", "")).startswith("tectonic@"):
            continue
        for a in rel.get("assets") or []:
            if str(a.get("name", "")).endswith(f"-{target}.{kind}"):
                return {"name": a["name"], "url": a["browser_download_url"], "kind": kind}
    return None


def _install_tectonic() -> dict:
    system = _system()
    asset = _tectonic_asset(_api(TECTONIC_RELEASES, "Tectonic"), system, _machine())
    if asset is None:
        raise _Failed("Tectonic has no download for this computer. " + how_to("tectonic"),
                      f"no release file for {_tectonic_target(system, _machine())}")
    program = "tectonic.exe" if system == "windows" else "tectonic"
    _place("tectonic", asset["url"], asset["kind"], lambda parts: parts[-1] == program, program)
    return {"state": "done", "error": None, "details": None}


# The GitHub CLI: https://github.com/cli/cli/releases, tag "v2.62.0", files such as
# gh_2.62.0_macOS_universal.zip and gh_2.62.0_linux_amd64.tar.gz, and gh_2.62.0_checksums.txt.
def _gh_names(system: str, machine: str, version: str) -> list[str] | None:
    """The release file names to look for, best first, or None where there is none."""
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine)
    if arch is None:
        return None
    v = version
    if system == "macos":
        return [f"gh_{v}_macOS_universal.zip", f"gh_{v}_macOS_{arch}.zip", f"gh_{v}_macOS_{arch}.tar.gz"]
    if system == "linux":
        return [f"gh_{v}_linux_{arch}.tar.gz"]
    return None


def _install_gh() -> dict:
    system, machine = _system(), _machine()
    rel = _api(GH_RELEASE, "The GitHub CLI")
    version = str(rel.get("tag_name", "")).lstrip("v") if isinstance(rel, dict) else ""
    assets = {a.get("name"): a.get("browser_download_url") for a in (rel.get("assets") or [])} \
        if isinstance(rel, dict) else {}
    name = next((n for n in _gh_names(system, machine, version) or [] if n in assets), None)
    sums_name = f"gh_{version}_checksums.txt"
    if name is None or sums_name not in assets:
        raise _Failed("The GitHub CLI has no download for this computer. " + how_to("gh"),
                      f"release {rel.get('tag_name') if isinstance(rel, dict) else '?'}: no "
                      f"{(_gh_names(system, machine, version) or ['file'])[0]} or {sums_name}")
    try:
        sums = _fetch(assets[sums_name], limit=1024 * 1024).decode("utf-8", "replace")
    except OSError as e:
        raise _Failed("The GitHub CLI could not be downloaded. Check your internet connection and try again.",
                      f"{assets[sums_name]}: {say(e)}") from None
    want = next((line.split()[0] for line in sums.splitlines()
                 if len(line.split()) == 2 and line.split()[1].lstrip("*") == name), None)
    if want is None:
        raise _Failed("The GitHub CLI's download could not be checked, so it was not installed. Try again.",
                      f"{sums_name} has no line for {name}")
    _place("gh", assets[name], "zip" if name.endswith(".zip") else "tar.gz",
           lambda parts: parts[-2:] == ["bin", "gh"], "gh", checksum=want)
    return {"state": "done", "error": None, "details": None}


# ---------------------------------------------------------------- Claude Code's login

def claude_login() -> dict:
    """A terminal window running claude, so that you can log in to Claude Code once
    (it asks on its first start). Refused, in a sentence, where none can be opened."""
    exe = claude_path()
    if not exe:
        raise Refused("Claude Code is not installed on this computer. Install it from the Home page (Getting started).")
    system = _system()
    home = str(Path.home())
    try:
        if system == "windows":
            subprocess.Popen([os.environ.get("COMSPEC") or "cmd", "/k", exe], cwd=home,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))
            return {"ok": True}
        if system == "macos":
            line = shlex.quote(exe).replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{line}"',
                              "-e", 'tell application "Terminal" to activate'], cwd=home,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True}
        for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("konsole", "-e"),
                           ("xterm", "-e")):
            found = shutil.which(term)
            if found:
                subprocess.Popen([found, flag, exe], cwd=home, start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return {"ok": True}
    except OSError as e:
        raise Refused("A terminal window could not be opened. Open a terminal yourself and run: claude",
                      details={"details": say(e)}) from None
    raise Refused("No terminal program was found on this computer. Open a terminal yourself and run: claude")
