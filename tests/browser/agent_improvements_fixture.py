"""Isolated UI fixture using the real server/Claude adapter and a local protocol double.

Run: python tests/browser/agent_improvements_fixture.py /tmp/prism-agent-ui
All settings, CLI state and sources stay under that folder. No model/network tools run.
"""
import json
import os
from pathlib import Path
import sys

work = Path(sys.argv[1]).resolve()
work.mkdir(parents=True, exist_ok=True)
root = work / "project"
(root / "sections").mkdir(parents=True, exist_ok=True)
(root / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\n\\input{sections/intro}\n\\label{dup}\n\\end{document}\n")
(root / "sections/intro.tex").write_text("\\section{Intro}\n\\label{eq:one}\n\\label{dup}\nHello\n")
(work / "profile").mkdir(exist_ok=True)
os.environ.update(PRISM_STATE_DIR=str(work / "state"), CLAUDE_CONFIG_DIR=str(work / "profile"))
cli = work / "claude-fixture"
cli.write_text("#!" + sys.executable + "\n" + r'''
import json, sys
from pathlib import Path
work=Path(__file__).parent
if 'auth' in sys.argv:
 print(json.dumps({'loggedIn':True,'email':'fixture@example.test','authMethod':'claude.ai','apiProvider':'firstParty'})); sys.exit(0)
with (work/'cli-requests.jsonl').open('a') as log:
 log.write(json.dumps({'argv':sys.argv[1:]})+'\n')
 def emit(data): print(json.dumps(data),flush=True)
 for line in sys.stdin:
  d=json.loads(line); log.write(json.dumps(d)+'\n'); log.flush()
  if d['type']=='control_request':
   if d['request']['subtype']=='initialize':
    result={'models':[{'value':'default','displayName':'Default','supportsEffort':True,'supportedEffortLevels':['low','high']},{'value':'fast','displayName':'Fast','supportsEffort':False},{'value':'deep','displayName':'Deep','supportsEffort':True,'supportedEffortLevels':['low','high']}]}
   else:
    if (work/'usage-fail').exists():
     emit({'type':'control_response','response':{'subtype':'error','request_id':d['request_id'],'error':'fixture usage offline'}}); continue
    counter=work/'usage-count'; n=int(counter.read_text())+1 if counter.exists() else 1; counter.write_text(str(n))
    result={'rate_limits_available':True,'rate_limits':{'five_hour':{'utilization':20+n,'resets_at':'2030-01-01T12:00:00Z'},'seven_day':{'utilization':10,'resets_at':'2030-01-07T12:00:00Z'}}}
   emit({'type':'control_response','response':{'subtype':'success','request_id':d['request_id'],'response':result}})
  elif d['type']=='user':
   emit({'type':'system','subtype':'init','session_id':'fixture-chat','model':'deep','apiKeySource':'none'})
   emit({'type':'control_request','request_id':'web-1','request':{'subtype':'can_use_tool','tool_name':'WebSearch','input':{'query':'LaTeX label example'}}})
  elif d['type']=='control_response':
   decision=d['response']['response']['behavior']
   emit({'type':'assistant','message':{'content':[{'type':'text','text':'Continued: '+decision+r'. See \ref{eq:one}, \ref{dup}, and \ref{missing}. Math $\eqref{eq:one}$.'}]}})
   emit({'type':'result','session_id':'fixture-chat','is_error':False,'duration_ms':10})
   break
''')
cli.chmod(0o755)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "prism_local"))
import server
from backend_claude import ClaudeCode
class FixtureClaude(ClaudeCode):
    def check_session(self, root, session_id):
        return None  # fixture has no native transcript files
server.set_root(root)
backend = FixtureClaude("claude", {"bin": str(cli)})
server.AGENT.backends = {"claude": backend}
server.AGENT.default = "claude"
srv = server.httpbase.listen(server.Handler, 0, 1)
url = f"http://127.0.0.1:{srv.server_address[1]}/"
server.AGENT.server_url = url
(work / "ready.json").write_text(json.dumps({"url": url}))
print(url, flush=True)
try:
    srv.serve_forever()
finally:
    srv.server_close()
    server.AGENT.shutdown()
