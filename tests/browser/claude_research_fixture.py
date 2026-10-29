"""Isolated real-editor/Claude-adapter fixture with a local CLI protocol process.

No model, account, service, or installation is used. CLI-owned files live in its
private profile so the same fixture runs inside the real read-only OS sandbox.
"""
import json
import os
from pathlib import Path
import sys

work = Path(sys.argv[1]).resolve()
root = work / 'project'
profile = work / 'profile'
root.mkdir(parents=True, exist_ok=True)
profile.mkdir(exist_ok=True)
if '--empty' not in sys.argv and not (root / 'main.tex').exists():
    (root / 'sections').mkdir(exist_ok=True)
    (root / 'main.tex').write_text(
        '\\documentclass{article}\n\\begin{document}\n'
        '\\emph{compact} is a finite covering property.\n'
        'Existing $\\ell+\\ell_+$.\nLet $x$ be a real number.\n'
        '\\input{sections/intro}\n\\end{document}\n')
    (root / 'sections/intro.tex').write_text('\\section{Intro}\nExisting $x$.\n')
os.environ.update(PRISM_STATE_DIR=str(work / 'state'), CLAUDE_CONFIG_DIR=str(profile),
                  PRISM_STARTED_CLAUDE_CONFIG_DIR=str(profile),
                  PRISM_AUTO_UPDATE='0', PRISM_SNIPPETS=str(work / 'snippets.js'),
                  PRISM_AGENTS=str(work / 'agents.json'))
cli = profile / 'claude-fixture'
cli.write_text('#!' + sys.executable + '\n' + r'''
import json,os,re,socket,subprocess,sys,time,uuid
from pathlib import Path
profile=Path(__file__).parent
root=Path.cwd().resolve()
argv=sys.argv[1:]
def emit(data): print(json.dumps(data),flush=True)
def record(kind,**data):
 with (profile/'cli-requests.jsonl').open('a') as f:
  f.write(json.dumps({'kind':kind,'pid':os.getpid(),**data})+'\n')
if '--version' in argv:
 print('2.1.300 (Claude Code fixture)');sys.exit(0)
if '--help' in argv:
 print('--tools --mcp-config --strict-mcp-config --setting-sources --input-format --output-format --resume --permission-prompt-tool');sys.exit(0)
if 'auth' in argv:
 emit({'loggedIn':True,'email':'fixture@example.test','authMethod':'claude.ai','apiProvider':'firstParty'});sys.exit(0)
record('start',argv=argv,cwd=str(root))
def option(name,default=None):
 if name in argv:return argv[argv.index(name)+1]
 return next((value.split('=',1)[1] for value in argv if value.startswith(name+'=')),default)
sid=option('--resume') or 'fixture-'+uuid.uuid4().hex
interrupted=False
final_sent=False

def receive():
 global interrupted
 for line in sys.stdin:
  frame=json.loads(line);record('input',frame=frame)
  if frame.get('type')=='control_request' and frame.get('request',{}).get('subtype')=='interrupt':
   emit({'type':'control_response','response':{'subtype':'success','request_id':frame['request_id'],'response':{}}})
   emit({'type':'result','session_id':sid,'is_error':False,'subtype':'success'})
   interrupted=True
   return None
  return frame
 return None

frame=receive()
if not frame:sys.exit(0)
if frame.get('type')=='control_request':
 req=frame.get('request',{})
 if req.get('subtype')=='initialize':
  emit({'type':'control_response','response':{'subtype':'success','request_id':frame['request_id'],'response':{'models':[{'value':'default','displayName':'Default','supportsEffort':True,'supportedEffortLevels':['low','high']},{'value':'fast','displayName':'Fast','supportsEffort':False},{'value':'deep','displayName':'Deep','supportsEffort':True,'supportedEffortLevels':['low','high']}]}}})
  frame=receive()
 if frame and frame.get('type')=='control_request':
  emit({'type':'control_response','response':{'subtype':'success','request_id':frame['request_id'],'response':{'rate_limits_available':True,'rate_limits':{'five_hour':{'utilization':10,'resets_at':'2030-01-01T12:00:00Z'}}}}});sys.exit(0)
if not frame or frame.get('type')!='user':sys.exit(0)
record('user',frame=frame,argv=argv,cwd=str(root),session_id=sid)
transcript=profile/'projects'/re.sub(r'[^A-Za-z0-9]','-',str(root))/f'{sid}.jsonl'
transcript.parent.mkdir(parents=True,exist_ok=True)
with transcript.open('a') as f:f.write(json.dumps({'sessionId':sid,'cwd':str(root)})+'\n')
emit({'type':'system','subtype':'init','session_id':sid,'model':option('--model','default'),'apiKeySource':'none'})
script=profile/'cli-script.json'
actions=json.loads(script.read_text()) if script.exists() else [{'type':'text','text':'Existing material can be discussed.'}]
for index,action in enumerate(actions):
 kind=action['type']
 if kind=='emit':
  emit(action['frame'])
  if action['frame'].get('type')=='result':
   final_sent=True;break
 elif kind=='text':emit({'type':'assistant','message':{'content':[{'type':'text','text':action['text']}]}})
 elif kind=='delta':emit({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':action['text']}}})
 elif kind=='mcp':
  rid='mcp-'+str(index)
  emit({'type':'control_request','request_id':rid,'request':{'subtype':'mcp_message','server_name':'prism','message':{'jsonrpc':'2.0','id':index+1,'method':'tools/call','params':{'name':action['name'],'arguments':action.get('arguments',{})}}}})
  answer=receive()
  if interrupted or answer is None:break
  record('mcp_result',name=action['name'],answer=answer)
 elif kind=='web':
  emit({'type':'control_request','request_id':'web-'+str(index),'request':{'subtype':'can_use_tool','tool_name':action.get('name','WebSearch'),'input':action.get('input',{'query':'Existing notation reference'})}})
  answer=receive()
  if interrupted or answer is None:break
  record('web_result',answer=answer)
 elif kind in ('direct_write','direct_read','loopback'):
  try:
   if kind=='direct_write':Path(action['path']).write_text(action.get('content','bad'))
   elif kind=='direct_read':Path(action['path']).read_bytes()
   else:
    with socket.create_connection(('127.0.0.1',action['port']),timeout=1) as sock:sock.sendall(b'GET / HTTP/1.0\r\n\r\n')
   record('attempt',operation=kind,blocked=False)
  except OSError as error:record('attempt',operation=kind,blocked=True,errno=error.errno)
 elif kind=='sleep':time.sleep(action.get('seconds',1))
 elif kind=='stderr':print(action['text'],file=sys.stderr,flush=True)
 elif kind=='spawn':
  child=subprocess.Popen([sys.executable,'-c',action['code']]);record('child',pid=child.pid)
if not interrupted and not final_sent:
 emit({'type':'result','session_id':sid,'is_error':False,'subtype':'success','duration_ms':10})
''')
cli.chmod(0o755)
(work / 'agents.json').write_text(json.dumps({'default':'claude','providers':{
    'claude':{'type':'claude','label':'Claude fixture','bin':str(cli),
              'models':['default','fast','deep'],'default_model':'default'}}}))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'prism_local'))
import research
import server
import guarded_compile
from backend_claude import ClaudeCode
from backends import Backend

original_replace = research.replace_file
def replace_file(path, data, **kwargs):
    if (work / 'fail-persistence').exists():
        raise OSError('fixture research persistence unavailable')
    return original_replace(path, data, **kwargs)
research.replace_file = replace_file
original_compile = guarded_compile.guarded_compile
def controlled_compile(project, *, clean=False, cancel=None):
    # An explicit harness marker verifies routing without running a TeX engine.
    # The sandboxed CLI cannot create this marker outside its profile.
    if not (work / 'compile-stub').exists():
        return original_compile(project, clean=clean, cancel=cancel)
    with (work / 'compile-calls.jsonl').open('a') as log:
        log.write(json.dumps({'root':str(project.root),'clean':clean,
                              'has_cancel':cancel is not None,
                              'cancelled':bool(cancel and cancel.is_set())})+'\n')
    return {'busy':False,'exit':1,'main':'main.tex','cancelled':False,'timed_out':False,
            'diagnostics':[{'severity':'error','file':'main.tex','line':1,
                            'message':'fixture guarded compile diagnostic'}],
            'output':'fixture diagnostic only','pdf_updated':False}
guarded_compile.guarded_compile = controlled_compile
server.set_root(root)
# Catalog metadata is deterministic; execution, permissions, session checks and the
# OS guard remain those of the actual adapter.
class FixtureClaude(ClaudeCode):
    model_catalog = Backend.model_catalog
backend = FixtureClaude('claude', {'bin':str(cli),'models':['default','fast','deep'],
                                  'default_model':'default'})
server.AGENT.backends = {'claude':backend}
server.AGENT.default = 'claude'
srv = server.httpbase.listen(server.Handler,0,1)
url = f'http://127.0.0.1:{srv.server_address[1]}/'
server.AGENT.server_url=url
(work / 'ready.json').write_text(json.dumps({'url':url,'root':str(root),'profile':str(profile)}))
print(url,flush=True)
try:
    srv.serve_forever()
finally:
    srv.server_close()
    server.AGENT.shutdown()
