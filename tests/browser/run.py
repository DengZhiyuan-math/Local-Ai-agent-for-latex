"""The pages driven in headless Chrome (not part of the unit tests: it needs Chrome and Node).

    python tests/browser/run.py <empty work folder>

Starts isolated servers (an editor on a scratch project, the Home page, a slow
OpenAI-compatible API for agent turns; PRISM_STATE_DIR and PRISM_AGENTS point into the
work folder, so your own projects and settings are not touched) and Chrome with remote
debugging on port 9333, runs each check script, and stops everything it started."""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

here = Path(__file__).resolve().parent
repo, work = here.parents[1], Path(sys.argv[1]).resolve()
scripts = here
shutil.rmtree(work, ignore_errors=True)
(work / "proj/sec").mkdir(parents=True)
(work / "proj/figs").mkdir()
(work / "proj2").mkdir()
(work / "proj/main.tex").write_text("\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n", encoding="utf-8")
(work / "proj/sec/a.tex").write_text("section\n", encoding="utf-8")
(work / "proj2/main.tex").write_text("x\n", encoding="utf-8")
(work / "agents.json").write_text(json.dumps({"default": "slow", "providers": {"slow": {
    "type": "openai", "label": "Slow", "base_url": "http://127.0.0.1:18765/v1", "api_key_env": "SLOW_KEY",
    "models": ["m"], "default_model": "m"}}}), encoding="utf-8")
env = {**os.environ, "PRISM_STATE_DIR": str(work / "state"), "PRISM_AGENTS": str(work / "agents.json"), "SLOW_KEY": "x"}
procs = []


def start(args, **kw):
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, **kw)
    procs.append(p)
    return p


def ready(file):
    for _ in range(60):
        if file.exists() and file.stat().st_size:
            return json.loads(file.read_text(encoding="utf-8"))["url"]
        time.sleep(0.5)
    raise RuntimeError(f"no {file}")


results = {}
try:
    start([sys.executable, str(here / "slow_api.py"), "18765"])
    start([sys.executable, str(repo / "prism_local/server.py"), str(work / "proj"), "--port", "0", "--no-browser",
           "--ready-file", str(work / "ready.json")])
    start([sys.executable, str(repo / "prism_local/hub.py"), "--port", "0", "--no-browser",
           "--ready-file", str(work / "hub.json")])
    start([r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--headless=new", "--remote-debugging-port=9333",
           f"--user-data-dir={work / 'chrome'}", "--no-first-run", "about:blank"])
    editor, home = ready(work / "ready.json"), ready(work / "hub.json")
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:9333/json/version", timeout=2).close()
            break
        except OSError:
            time.sleep(0.5)

    def node(script, *args):
        r = subprocess.run(["node", str(script), *args], capture_output=True, text=True, encoding="utf-8",
                           timeout=180, cwd=here)
        return (r.stdout + r.stderr).strip()
    results["editor files menu"] = node(scripts / "files_menu.mjs", editor)
    results["turn reload, double send"] = node(scripts / "agent_turn.mjs", editor)
    results["home menu"] = node(scripts / "home_menu.mjs", home, str(work / "proj"), str(work / "proj2"))
    results["pages load"] = node(scripts / "pages.mjs", editor, home)
finally:
    for p in procs:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
for name, out in results.items():
    lines = [l for l in out.splitlines() if l.startswith(("PASS", "FAIL")) or "Error" in l]
    fails = [l for l in lines if not l.startswith("PASS")]
    print(f"== {name}: {sum(l.startswith('PASS') for l in lines)} passed, {len(fails)} not")
    for l in fails:
        print("   ", l[:300])
