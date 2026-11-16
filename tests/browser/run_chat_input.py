"""Run the chat input browser check through a Chrome debugging pipe.

    python tests/browser/run_chat_input.py <new or empty work folder>

The editor, settings and browser profile are isolated in that folder. The pipe avoids
requiring a listening Chrome debugging port; the editor still uses a local HTTP port.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

here = Path(__file__).resolve().parent
repo, work = here.parents[1], Path(sys.argv[1]).resolve()
if work.exists() and any(work.iterdir()):
    raise ValueError("Use a new or empty browser-test folder; existing contents are never removed")
(work / "project/sec").mkdir(parents=True)
(work / "project/main.tex").write_text("\\documentclass{article}\n\\begin{document}Hi\\end{document}\n", encoding="utf-8")
(work / "project/sec/a.tex").write_text("Hi\n", encoding="utf-8")
(work / "agents.json").write_text(json.dumps({"default": "slow", "providers": {"slow": {
    "type": "openai", "label": "Slow", "base_url": "http://127.0.0.1:18765/v1",
    "api_key_env": "SLOW_KEY", "models": ["m"], "default_model": "m"}}}), encoding="utf-8")
env = {**os.environ, "PRISM_STATE_DIR": str(work / "state"), "PRISM_AGENTS": str(work / "agents.json"),
       "PRISM_SNIPPETS": str(work / "snippets.js"), "PRISM_TEST_CHROME_PROFILE": str(work / "chrome"),
       "PRISM_AUTO_UPDATE": "0", "SLOW_KEY": "x"}
ready = work / "ready.json"
editor = subprocess.Popen([sys.executable, str(repo / "prism_local/server.py"), str(work / "project"),
                           "--port", "0", "--no-browser", "--ready-file", str(ready)],
                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        if ready.exists() and ready.stat().st_size:
            break
        time.sleep(.1)
    url = json.loads(ready.read_text(encoding="utf-8"))["url"]
    result = subprocess.run(["node", str(here / "chat_input.mjs"), url], env=env, timeout=90)
    sys.exit(result.returncode)
finally:
    editor.terminate()
    editor.wait(timeout=10)
