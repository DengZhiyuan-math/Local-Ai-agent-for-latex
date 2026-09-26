#!/usr/bin/env python3
"""prism-local Home: one page to manage all your LaTeX projects.

    python3 prism_local/hub.py [--port 8790] [--no-browser] [--exit-when-idle]
                               [--port-tries N] [--ready-file F]

Lists the projects in the shared project list (registry.py), with title, PDF
thumbnail, git state and whether an editor is running. From here you can add an
existing folder, create a new project from a template, and open a project: each
project still runs in its own prism-local server (server.py), started through
the launcher, so it keeps its stable port and per-project browser state.

Like server.py it listens on 127.0.0.1 only, requires X-Prism-Local: 1 on every
state-changing request, and uses the Python standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpbase  # noqa: E402
import registry  # noqa: E402
from backend_claude import claude_account, claude_bin  # noqa: E402
from fsutil import EDITABLE_SUFFIXES, SKIP_DIRS  # noqa: E402
from presence import Presence  # noqa: E402
from proc import NO_WINDOW  # noqa: E402
from texutil import tex_title  # noqa: E402

PRESENCE = Presence()
MAX_SCAN = 3000
BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------- project facts

def read_prism_json(root: Path) -> dict:
    try:
        data = json.loads((root / "prism.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def guess_main(root: Path) -> str | None:
    if (root / "main.tex").is_file():
        return "main.tex"
    for p in sorted(root.glob("*.tex")):
        try:
            if not p.name.startswith("._") and \
                    "\\documentclass" in p.read_text(encoding="utf-8", errors="replace")[:5000]:
                return p.name
        except OSError:
            pass
    return None


def scan_sources(root: Path, outdir: str) -> tuple[int, float | None]:
    """How many editable source files the project has, and the newest mtime."""
    count, newest = 0, None
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root).as_posix()
        rel = "" if rel == "." else rel + "/"
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
                       and rel + d != outdir]
        for f in filenames:
            if Path(f).suffix in EDITABLE_SUFFIXES and not f.startswith("._"):
                count += 1
                try:
                    t = os.stat(os.path.join(dirpath, f)).st_mtime
                    newest = t if newest is None or t > newest else newest
                except OSError:
                    pass
        if count > MAX_SCAN:
            break
    return count, newest


def pdf_path(root: Path) -> tuple[Path | None, str | None, str]:
    cfg = read_prism_json(root)
    main = cfg.get("main") or guess_main(root)
    outdir = str(cfg.get("outdir") or "build").strip("/") or "build"
    if not main:
        return None, None, outdir
    return root / outdir / f"{Path(main).stem}.pdf", main, outdir


def project_info(entry: dict) -> dict:
    root = Path(entry["path"])
    d = {"id": registry.project_key(root), "path": str(root),
         "name": entry.get("name") or root.name, "folder": root.name,
         "custom_name": bool(entry.get("name")), "pinned": bool(entry.get("pinned")),
         "opened": entry.get("opened"), "added": entry.get("added"),
         "exists": root.is_dir(), "running": None}
    if not d["exists"]:
        return d
    pdf, main, outdir = pdf_path(root)
    files, edited = scan_sources(root, outdir)
    d.update(main=main, title=tex_title(root / main) if main else None,
             files=files, edited=edited,
             pdf_mtime=pdf.stat().st_mtime if pdf and pdf.is_file() else None)
    # Only a look: a server that is slow to answer right now keeps its instance file.
    inst = registry.running_instance(registry.instance_file(d["id"]), "prism-local", root,
                                     timeout=0.8, cleanup=False)
    if inst:
        d["running"] = {"url": inst["url"], "pages": inst.get("pages", 0)}
    return d


def list_projects() -> dict:
    data = registry.load_projects()
    entries = data["projects"]
    with ThreadPoolExecutor(max_workers=8) as ex:
        projects = list(ex.map(project_info, entries))
    return {"projects": projects, "default_parent": default_parent(data),
            "home": str(Path.home())}


def default_parent(data: dict) -> str:
    chosen = load_settings()["default_parent"]
    if chosen and Path(chosen).is_dir():
        return chosen
    if data.get("last_parent") and Path(data["last_parent"]).is_dir():
        return data["last_parent"]
    recent = sorted(data["projects"], key=lambda p: p.get("opened") or p.get("added") or 0,
                    reverse=True)
    for p in recent:
        parent = Path(p["path"]).parent
        if parent.is_dir():
            return str(parent)
    docs = Path.home() / "Documents"
    return str(docs if docs.is_dir() else Path.home())


def git_info(root: Path) -> dict | None:
    """Branch and changes of the project's own repository. A project folder inside some
    other repository (such as prism-local's) has none of its own: report which one."""
    def git(*args):      # read-only, and never holding .git/index.lock (see server.git)
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=root, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=8, **NO_WINDOW)
    try:
        top = git("rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return None
        toplevel = Path(top.stdout.strip())
        if registry.norm(toplevel.resolve()) != registry.norm(root.resolve()):
            return {"nested": True, "toplevel": toplevel.name, "toplevel_path": str(toplevel)}
        r = git("status", "--porcelain", "-b")
        remote = git("remote", "get-url", "origin").stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    lines = r.stdout.splitlines()
    head = lines[0][3:] if lines and lines[0].startswith("## ") else ""
    head = re.sub(r"^(No commits yet on|Initial commit on) ", "", head)
    branch = re.split(r"\.\.\.| ", head)[0] if head else ""
    ahead = re.search(r"ahead (\d+)", head)
    behind = re.search(r"behind (\d+)", head)
    web = None
    m = re.match(r"(?:https://github\.com/|git@github\.com:)(.+?)(?:\.git)?$", remote)
    if m:
        web = "https://github.com/" + m.group(1)
    return {"branch": branch, "changes": len(lines) - 1,
            "ahead": int(ahead.group(1)) if ahead else 0,
            "behind": int(behind.group(1)) if behind else 0, "github": web}


def entry_for(pid: str) -> dict:
    for e in registry.load_projects()["projects"]:
        if registry.project_key(Path(e["path"])) == pid:
            return e
    raise KeyError("unknown project")


# ---------------------------------------------------------------- actions

def add_project(path: str) -> dict:
    p = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"')))).resolve()
    if not p.is_dir():
        raise ValueError(f"not a folder: {p}")
    has_tex = any(p.glob("*.tex"))
    registry.update_projects(lambda data: _add(data, p))
    return {"id": registry.project_key(p), "path": str(p), "has_tex": has_tex}


def _add(data: dict, p: Path) -> None:
    if registry.find(data, p) is None:
        data["projects"].append({"path": str(p), "added": time.time()})


# ---------------------------------------------------------------- settings

SETTINGS_DEFAULTS = {"default_parent": "", "git_init": True, "github_repo": False,
                     "github_owner": "", "claude_account": ""}
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


def settings_path() -> Path:
    return registry.state_dir() / "settings.json"


def load_settings() -> dict:
    data = registry.read_json(settings_path()) or {}
    return {k: data.get(k, v) for k, v in SETTINGS_DEFAULTS.items()}


def save_settings(body: dict) -> dict:
    cur = load_settings()
    if "default_parent" in body:
        p = str(body["default_parent"] or "").strip().strip('"')
        if p and not Path(os.path.expanduser(p)).is_dir():
            raise ValueError(f"not a folder: {p}")
        cur["default_parent"] = str(Path(os.path.expanduser(p)).resolve()) if p else ""
    for k in ("git_init", "github_repo"):
        if k in body:
            cur[k] = bool(body[k])
    if "claude_account" in body:
        acct = str(body["claude_account"] or "").strip()
        if acct and not EMAIL_RE.fullmatch(acct):
            raise ValueError("the Claude account must be an email address")
        cur["claude_account"] = acct
    if "github_owner" in body:
        owner = str(body["github_owner"] or "").strip()
        if owner and not OWNER_RE.fullmatch(owner):
            raise ValueError("not a GitHub user or organization name")
        cur["github_owner"] = owner
    registry.write_json(settings_path(), cur)
    return cur


# ---------------------------------------------------------------- GitHub

_gh_cache: dict = {}


def gh_status(refresh: bool = False) -> dict:
    """Whether the GitHub CLI (gh) is installed and logged in, and as whom."""
    if _gh_cache and not refresh and time.time() - _gh_cache["at"] < 120:
        return _gh_cache["status"]
    exe = shutil.which("gh")
    status = {"gh": exe, "logged_in": False, "account": None}
    if not exe:
        status["error"] = "GitHub CLI not found. Install it from https://cli.github.com, then run: gh auth login"
    else:
        try:
            r = subprocess.run([exe, "auth", "status", "--hostname", "github.com"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=20, env={**os.environ, "GH_PROMPT_DISABLED": "1"},
                               **NO_WINDOW)
            out = r.stdout + r.stderr
            m = re.search(r"Logged in to github\.com (?:account|as) (\S+)", out)
            if m and r.returncode == 0:
                status.update(logged_in=True, account=m.group(1).strip("()"))
            else:
                status["error"] = "GitHub CLI is not logged in. Run: gh auth login"
        except (OSError, subprocess.SubprocessError) as e:
            status["error"] = f"gh auth status failed: {e}"
    _gh_cache.update(at=time.time(), status=status)
    return status


def claude_status(refresh: bool = False) -> dict:
    """The account Claude Code is logged in to, and anything that would override it."""
    return claude_account(claude_bin(), None, max_age=0 if refresh else 30)


def repo_name(folder: str) -> str:
    """A GitHub repository name for a folder name: letters, digits, '.', '-', '_'."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", folder).strip("-.")
    return name[:100] or "latex-project"


def run_git(root: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, check=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **NO_WINDOW)


def git_init(root: Path) -> None:
    """A repository of the project's own, with build output ignored."""
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_bytes(GITIGNORE.encode("utf-8"))
    if not (root / ".git").exists():
        try:
            run_git(root, "init", "-q", "-b", "main")
        except subprocess.CalledProcessError:          # git older than 2.28
            run_git(root, "init", "-q")


def create_github_repo(root: Path, owner: str = "", name: str = "") -> dict:
    """Commit the project and push it to a new private GitHub repository.

    Returns {"url"} or {"error"}. The project itself is never removed on failure."""
    st = gh_status()
    if not st.get("logged_in"):
        return {"error": st.get("error") or "GitHub CLI is not ready"}
    if owner and not OWNER_RE.fullmatch(owner):
        return {"error": "not a GitHub user or organization name"}
    name = name.strip() or repo_name(root.name)
    if not REPO_RE.fullmatch(name):
        return {"error": f"not a GitHub repository name: {name}"}
    full = f"{owner}/{name}" if owner else name
    try:
        git_init(root)
        run_git(root, "add", "-A")
        if run_git(root, "status", "--porcelain").stdout.strip():
            run_git(root, "commit", "-q", "-m", "Initial commit")
        r = subprocess.run([st["gh"], "repo", "create", full, "--private", "--source", ".",
                            "--remote", "origin", "--push"],
                           cwd=root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180,
                           env={**os.environ, "GH_PROMPT_DISABLED": "1"}, **NO_WINDOW)
    except subprocess.CalledProcessError as e:
        return {"error": f"git {e.cmd[1]} failed: {(e.stderr or e.stdout or '').strip()[:400]}"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)}
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout).strip()[:500] or "gh repo create failed"}
    m = re.search(r"https://github\.com/\S+", r.stdout + r.stderr)
    url = m.group(0).rstrip(".") if m else f"https://github.com/{owner or st['account']}/{name}"
    return {"url": url}


TEMPLATES = {
    "amsart": {
        "main.tex": r"""\documentclass[11pt]{amsart}
\usepackage{amsmath,amssymb,amsthm}
\usepackage{hyperref}
\usepackage[capitalize]{cleveref}

\newtheorem{theorem}{Theorem}[section]
\newtheorem{proposition}[theorem]{Proposition}
\newtheorem{lemma}[theorem]{Lemma}
\newtheorem{corollary}[theorem]{Corollary}
\theoremstyle{definition}
\newtheorem{definition}[theorem]{Definition}
\theoremstyle{remark}
\newtheorem{remark}[theorem]{Remark}

\title{@TITLE@}
\author{@AUTHOR@}

\begin{document}

\begin{abstract}
\end{abstract}

\maketitle

\input{sections/intro}

% Uncomment once the paper has a \cite:
% \bibliographystyle{amsplain}
% \bibliography{refs}
\end{document}
""",
        "sections/intro.tex": "\\section{Introduction}\\label{sec:intro}\n\n",
        "refs.bib": "% BibTeX entries for this paper.\n",
    },
    "article": {
        "main.tex": r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage{amsmath,amssymb}
\usepackage{hyperref}

\title{@TITLE@}
\author{@AUTHOR@}
\date{\today}

\begin{document}
\maketitle

\section{Introduction}\label{sec:intro}

\end{document}
""",
    },
    "empty": {
        "main.tex": "\\documentclass{article}\n\\begin{document}\n\n\\end{document}\n",
    },
}
GITIGNORE = "build/\n*.prism-tmp\n.DS_Store\n"


def create_project(body: dict) -> dict:
    name = str(body.get("name", "")).strip()
    if not name or BAD_NAME.search(name) or name in (".", "..") or name.endswith((".", " ")):
        raise ValueError("choose a folder name without \\ / : * ? \" < > |")
    parent = Path(os.path.expanduser(str(body.get("parent", "")).strip().strip('"')))
    if not parent.is_absolute() or not parent.is_dir():
        raise ValueError(f"location is not a folder: {parent}")
    tpl = TEMPLATES.get(body.get("template", "amsart"))
    if tpl is None:
        raise ValueError("unknown template")
    root = (parent / name).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"{root} already exists and is not empty")
    title = str(body.get("title") or "").strip() or name
    author = str(body.get("author") or "").strip()
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in tpl.items():           # \n line ends on every system, as git stores them
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(text.replace("@TITLE@", title).replace("@AUTHOR@", author).encode("utf-8"))
    (root / "prism.json").write_bytes((json.dumps({"main": "main.tex", "outdir": "build"},
                                                  indent=2) + "\n").encode("utf-8"))
    git_note, github = None, None
    if body.get("git") or body.get("github"):
        try:
            git_init(root)
        except (OSError, subprocess.SubprocessError) as e:
            git_note = f"git init failed: {e}"
    if body.get("github") and not git_note:
        github = create_github_repo(root, str(body.get("github_owner") or "").strip(),
                                    str(body.get("github_name") or ""))

    def fn(data):
        _add(data, root)
        data["last_parent"] = str(parent.resolve())
    registry.update_projects(fn)
    return {"id": registry.project_key(root), "path": str(root), "git_note": git_note,
            "github": github}


def change_project(pid: str, body: dict) -> dict:
    def fn(data):
        for e in data["projects"]:
            if registry.project_key(Path(e["path"])) == pid:
                if "pinned" in body:
                    e["pinned"] = bool(body["pinned"])
                if "name" in body:
                    name = str(body["name"] or "").strip()[:120]
                    if name and name != Path(e["path"]).name:
                        e["name"] = name
                    else:
                        e.pop("name", None)
                return True
        return False
    if not registry.update_projects(fn):
        raise KeyError("unknown project")
    return {"ok": True}


def remove_project(pid: str) -> dict:
    """Forget a project. Its files are not touched."""
    def fn(data):
        before = len(data["projects"])
        data["projects"] = [e for e in data["projects"]
                            if registry.project_key(Path(e["path"])) != pid]
        return len(data["projects"]) < before
    if not registry.update_projects(fn):
        raise KeyError("unknown project")
    return {"ok": True}


def open_project(pid: str) -> dict:
    root = Path(entry_for(pid)["path"])
    if not root.is_dir():
        return {"error": f"folder not found: {root}"}
    r = registry.ensure_server(root)
    if "url" in r:
        registry.safe_touch(root)
    return r


def reveal(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


PICK_FOLDER = r"""
import sys, tkinter, tkinter.filedialog
root = tkinter.Tk(); root.withdraw(); root.attributes("-topmost", True)
p = tkinter.filedialog.askdirectory(parent=root, initialdir=sys.argv[1] or None,
                                    title=sys.argv[2], mustexist=True)
sys.stdout.write(p or "")
"""


def pick_folder(start: str, title: str) -> dict:
    """A native folder dialog (tkinter, in a child process so it has its own main loop)."""
    try:
        r = subprocess.run([sys.executable, "-c", PICK_FOLDER, start or "", title or "Choose a folder"],
                           capture_output=True, timeout=900,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"folder dialog unavailable: {e}"}
    if r.returncode != 0:
        return {"error": "folder dialog unavailable (tkinter missing?)"}
    p = r.stdout.decode("utf-8", "replace").strip()
    return {"path": str(Path(p).resolve()) if p else None}


# ---------------------------------------------------------------- http

class Handler(httpbase.Handler):
    """The Home page's requests. The checks every request passes are in httpbase.py."""

    server_version = "prism-home/1"
    presence = PRESENCE
    pages = {"/": "home.html", "/index.html": "home.html"}

    def get(self, path, q):
        try:
            if path == "/api/ping":
                return self._json({"app": "prism-home", "pid": os.getpid(),
                                   "pages": PRESENCE.count()})
            if path == "/api/projects":
                return self._json(list_projects())
            if path == "/api/settings":
                return self._json({"settings": load_settings(),
                                   "github": gh_status(refresh=q.get("refresh") == "1"),
                                   "claude": claude_status(q.get("refresh") == "1")})
            if path == "/api/git":
                return self._json({"git": git_info(Path(entry_for(q["id"])["path"]))})
            if path == "/api/pdf":
                pdf, _, _ = pdf_path(Path(entry_for(q["id"])["path"]))
                if not pdf or not pdf.is_file():
                    return self._err(404, "no PDF yet")
                return self._send(200, pdf.read_bytes(), "application/pdf")
            return self._err(404, "not found")
        except KeyError as e:
            if e.args[0] == "unknown project":
                return self._err(404, "unknown project")
            return self._err(400, f"missing {e.args[0]}")
        except (ValueError, OSError) as e:
            return self._err(400, str(e))

    def post(self, path, body):
        try:
            if path == "/api/settings":
                return self._json({"settings": save_settings(body), "github": gh_status(),
                                   "claude": claude_status()})
            if path == "/api/projects/add":
                return self._json(add_project(str(body["path"])))
            if path == "/api/projects/create":
                return self._json(create_project(body))
            if path == "/api/projects/update":
                return self._json(change_project(body["id"], body))
            if path == "/api/projects/remove":
                return self._json(remove_project(body["id"]))
            if path == "/api/projects/open":
                r = open_project(body["id"])
                return self._json(r, 502 if "error" in r else 200)
            if path == "/api/projects/reveal":
                p = Path(entry_for(body["id"])["path"])
                if not p.is_dir():
                    raise ValueError(f"folder not found: {p}")
                reveal(p)
                return self._json({"ok": True})
            if path == "/api/pick-folder":
                return self._json(pick_folder(str(body.get("start") or ""),
                                              str(body.get("title") or "")))
            return self._err(404, "not found")
        except KeyError as e:
            if e.args[0] == "unknown project":
                return self._err(404, "unknown project")
            return self._err(400, f"missing {e.args[0]}")
        except (ValueError, OSError, TimeoutError) as e:
            return self._err(400, str(e))


def main():
    httpbase.quiet_stdio()
    ap = argparse.ArgumentParser(description="prism-local Home: manage your LaTeX projects")
    ap.add_argument("--port", type=int, default=registry.HOME_PORT, help="port (0: any free port)")
    ap.add_argument("--port-tries", type=int, default=1,
                    help="if the port is taken, try this many ports upwards")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--exit-when-idle", action="store_true",
                    help="exit shortly after the last Home page is closed")
    ap.add_argument("--ready-file", type=Path,
                    help="once listening, write {pid, port, url} as JSON to this file")
    ap.add_argument("--idle-timings", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.idle_timings:
        f, g, st = (float(x) for x in a.idle_timings.split(","))
        PRESENCE.first_wait, PRESENCE.grace, PRESENCE.stale = f, g, st
    try:
        srv = httpbase.listen(Handler, a.port, a.port_tries)
    except OSError as e:
        sys.exit(f"prism-home: {e}")
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"prism-local Home: {url}\n  projects: {registry.state_dir() / 'projects.json'}",
          flush=True)
    if a.ready_file:
        httpbase.write_ready_file(a.ready_file, {"app": "prism-home", "pid": os.getpid(),
                                                 "port": srv.server_address[1], "url": url})
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if a.exit_when_idle:
        threading.Thread(target=httpbase.idle_watchdog, args=(srv, PRESENCE), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if a.ready_file:
            httpbase.remove_ready_file(a.ready_file)
        httpbase.log("stopped")


if __name__ == "__main__":
    sys.exit(main())

