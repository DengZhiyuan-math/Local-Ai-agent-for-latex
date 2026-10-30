"""Isolated research HTTP/browser fixture; no real model or GitHub calls.

Run: python tests/browser/research_fixture.py /tmp/prism-research-ui [--empty]
The ready.json file gives the editor URL. Tests replace api-script.json with
FakeAPI SSE scripts; requests are recorded in api-requests.jsonl.
"""
import json
import os
from pathlib import Path
import sys

if os.environ.get("CI") == "true":
    import faulthandler
    faulthandler.dump_traceback_later(5, repeat=True)

work = Path(sys.argv[1]).resolve()
work.mkdir(parents=True, exist_ok=True)
root = work / "project"
root.mkdir(exist_ok=True)
if "--empty" not in sys.argv and not (root / "main.tex").exists():
    (root / "sections").mkdir(exist_ok=True)
    (root / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "\\emph{compact} is a finite covering property.\n"
        "\\emph{unresolved}\nExisting $\\ell+\\ell_+$.\nLet $x$ be a real number.\n"
        "\\input{sections/intro}\n\\end{document}\n", encoding="utf-8")
    (root / "sections/intro.tex").write_text(
        "\\section{Intro}\n\\emph{compact} means a conflicting definition.\n"
        "$x$ denotes a complex number.\n\\input{missing}\n", encoding="utf-8")

os.environ.update(PRISM_STATE_DIR=str(work / "state"), PRISM_AUTO_UPDATE="0",
                  PRISM_SNIPPETS=str(work / "snippets.js"),
                  PRISM_AGENTS=str(work / "agents.json"))
(work / "agents.json").write_text(json.dumps({"default": "fixture", "providers": {
    "fixture": {"type": "openai", "label": "Research fixture", "base_url": "http://127.0.0.1:1",
                "models": ["fixture-model", "fixture-model-2"], "default_model": "fixture-model"}}}), encoding="utf-8")
repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo / "prism_local"))
sys.path.insert(0, str(repo / "tests"))
from test_agent import FakeAPI
from backend_openai import OpenAICompat
from backends import Backend
import research
import server


class ScriptQueue:
    def pop(self, index):
        path = work / "api-script.json"
        chunks = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if not chunks:
            return [{"choices": [{"delta": {"content": "Existing material can be discussed."}}]}]
        result = chunks.pop(index)
        path.write_text(json.dumps(chunks), encoding="utf-8")
        return result


class RequestLog(list):
    def append(self, value):
        super().append(value)
        with (work / "api-requests.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(value, ensure_ascii=False) + "\n")


port_file = work / "api-port"
api = FakeAPI([], port=int(port_file.read_text()) if port_file.exists() else 0)
port_file.write_text(str(api.server_port))
api.script, api.requests = ScriptQueue(), RequestLog()
original_replace = research.replace_file


def replace_file(path, data, **kwargs):
    if (work / "fail-persistence").exists():
        raise OSError("fixture research persistence unavailable")
    return original_replace(path, data, **kwargs)


research.replace_file = replace_file
server.set_root(root)
class FixtureAPI(OpenAICompat):
    # Catalog discovery is separate from this fixture's scripted inference API.
    model_catalog = Backend.model_catalog


server.AGENT.backends = {"fixture": FixtureAPI("fixture", {
    "label": "Research fixture",
    "base_url": f"http://127.0.0.1:{api.server_port}",
    "models": ["fixture-model", "fixture-model-2"], "default_model": "fixture-model"})}
server.AGENT.default = "fixture"
srv = server.httpbase.listen(server.Handler, 0, 1)
url = f"http://127.0.0.1:{srv.server_address[1]}/"
server.AGENT.server_url = url
(work / "ready.json").write_text(json.dumps({"url": url, "root": str(root),
                                           "api_url": f"http://127.0.0.1:{api.server_port}"}), encoding="utf-8")
if os.environ.get("CI") == "true":
    faulthandler.cancel_dump_traceback_later()
print(url, flush=True)
try:
    srv.serve_forever()
finally:
    srv.server_close()
    server.AGENT.shutdown()
    api.shutdown()
    api.server_close()
