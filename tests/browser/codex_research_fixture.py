"""Isolated editor/Codex adapter driven by a local app-server protocol process.

No model, login, service or installation is used. CLI-owned files stay in its
private CODEX_HOME; execution and native session checks use the real adapter.
"""
import json
import os
from pathlib import Path
import sys
import uuid

work = Path(sys.argv[1]).resolve()
root, profile = work / 'project', work / 'profile'
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
os.environ.update(PRISM_STATE_DIR=str(work / 'state'), CODEX_HOME=str(profile),
                  PRISM_AUTO_UPDATE='0', PRISM_SNIPPETS=str(work / 'snippets.js'),
                  PRISM_AGENTS=str(work / 'agents.json'))
legacy_session=None
if '--legacy' in sys.argv:
    legacy_marker=profile/'legacy-session'
    legacy_session=legacy_marker.read_text() if legacy_marker.exists() else 'fixture-legacy-'+uuid.uuid4().hex
    legacy_marker.write_text(legacy_session)
    native=profile/'sessions'/('rollout-fixture-'+legacy_session+'.jsonl');native.parent.mkdir(exist_ok=True)
    if not native.exists():native.write_text(json.dumps({'type':'session_meta','payload':{'id':legacy_session,'cwd':str(root)}})+'\n')
cli = profile / 'codex-fixture'
cli.write_text('#!' + sys.executable + '\n' + r'''
import json,os,socket,subprocess,sys,time,uuid
from pathlib import Path
try:
 import tomllib
except ModuleNotFoundError:
 import tomli as tomllib
profile=Path(__file__).parent
root=Path.cwd().resolve()
argv=sys.argv[1:]
def emit(data): print(json.dumps(data),flush=True)
def record(kind,**data):
 with (profile/'cli-requests.jsonl').open('a') as f:
  f.write(json.dumps({'kind':kind,'pid':os.getpid(),**data})+'\n')
if '--version' in argv:print('codex-cli 0.165.0 (fixture)');sys.exit(0)
if '--help' in argv:print('--ignore-user-config --ignore-rules --listen');sys.exit(0)
if 'login' in argv:print('Logged in using fixture');sys.exit(0)
record('start',argv=argv,cwd=str(root))
sid,tid=None,None
relay=None
relay_name=None
dynamic_enabled=False
effective={'mcp_servers':{}}
def merge(target,values):
 for key,value in values.items():
  if isinstance(value,dict) and isinstance(target.get(key),dict):merge(target[key],value)
  else:target[key]=value
for index,arg in enumerate(argv[:-1]):
 if arg=='-c':merge(effective,tomllib.loads(argv[index+1]))
interrupted=False
last_id=0

def result(frame,value):emit({'id':frame['id'],'result':value})
def notify(method,params):
 if method=='item/started':params.setdefault('startedAtMs',int(time.time()*1000))
 if method=='item/completed':params.setdefault('completedAtMs',int(time.time()*1000))
 emit({'method':method,'params':params})
def completed(status='completed',error=None):
 notify('turn/completed',{'threadId':sid,'turn':{'id':tid,'items':[],'status':status,'error':error}})
def receive():
 global interrupted
 for line in sys.stdin:
  frame=json.loads(line);record('input',frame=frame)
  if frame.get('method')=='turn/interrupt':
   result(frame,{})
   completed('interrupted');interrupted=True
   return None
  return frame
 return None

def tool_result():
 while True:
  frame=receive()
  if frame is None:return None
  if 'result' in frame or 'error' in frame:return frame
  if 'id' in frame:result(frame,{})

def relay_rpc(method,params,mid):
 relay.stdin.write(json.dumps({'jsonrpc':'2.0','id':mid,'method':method,'params':params})+'\n');relay.stdin.flush()
 for line in relay.stdout:
  response=json.loads(line)
  if response.get('id')==mid:return response
 raise RuntimeError('The actual relay helper ended before answering')

def actions_for_turn():
 global interrupted
 script=profile/'cli-script.json'
 actions=json.loads(script.read_text()) if script.exists() else [{'type':'text','text':'Existing material can be discussed.'}]
 for index,action in enumerate(actions):
  kind=action['type'];item=action.get('item','fixture-reply' if kind in ('delta','text') else 'fixture-item-'+str(index))
  if kind=='emit':notify(action['method'],{'threadId':sid,'turnId':tid,**action.get('params',{})})
  elif kind=='text':notify('item/completed',{'threadId':sid,'turnId':tid,'item':{'type':'agentMessage','id':item,'text':action['text'],'phase':'final_answer'}})
  elif kind=='delta':notify('item/agentMessage/delta',{'threadId':sid,'turnId':tid,'itemId':item,'delta':action['text']})
  elif kind=='mcp':
   # Shared fixture action name; use actual Codex dynamic or native MCP transport.
   if relay is not None:
    answer=relay_rpc('tools/call',{'name':action['name'],'arguments':action.get('arguments',{})},index+10)
    record('mcp_result',name=action['name'],answer=answer,transport='stdio_mcp')
    payload=answer.get('result',{})
    notify('item/completed',{'threadId':sid,'turnId':tid,'item':{'type':'mcpToolCall','id':item,'server':relay_name,'tool':action['name'],'arguments':action.get('arguments',{}),'status':'completed' if not payload.get('isError') else 'failed','result':payload,'error':None,'durationMs':1}})
   else:
    if not dynamic_enabled:raise RuntimeError('A legacy session has no dynamic tools or required MCP relay')
    params={'threadId':sid,'turnId':tid,'callId':item,'tool':action['name'],'arguments':action.get('arguments',{})}
    notify('item/started',{'threadId':sid,'turnId':tid,'item':{'type':'dynamicToolCall','id':item,'tool':action['name'],'arguments':params['arguments'],'status':'inProgress'}})
    emit({'id':'tool-'+str(index),'method':'item/tool/call','params':params})
    answer=tool_result()
    if interrupted or answer is None:break
    record('mcp_result',name=action['name'],answer=answer,transport='dynamic')
    payload=answer.get('result',{})
    notify('item/completed',{'threadId':sid,'turnId':tid,'item':{'type':'dynamicToolCall','id':item,'tool':action['name'],'arguments':params['arguments'],'status':'completed' if payload.get('success') else 'failed',**payload}})
  elif kind in ('direct_write','direct_read','loopback','shell_write','patch_write'):
   try:
    if kind=='direct_write':Path(action['path']).write_text(action.get('content','bad'))
    elif kind=='direct_read':Path(action['path']).read_bytes()
    elif kind=='shell_write':
     command='from pathlib import Path;Path('+repr(action['path'])+').write_text('+repr(action.get('content','bad'))+')'
     run=subprocess.run([sys.executable,'-c',command],capture_output=True,text=True)
     if run.returncode:raise PermissionError(run.stderr[-300:])
    elif kind=='patch_write':
     first=Path(action['path']).read_text().splitlines()[0]
     patch='--- '+action['path']+'\n+++ '+action['path']+'\n@@ -1 +1 @@\n-'+first+'\n+bad\n'
     run=subprocess.run(['/usr/bin/patch',action['path']],input=patch,capture_output=True,text=True)
     record('patch_result',exit=run.returncode,stdout=run.stdout,stderr=run.stderr)
     if run.returncode or any(message in (run.stdout+run.stderr).lower() for message in ('operation not permitted','permission denied')):raise PermissionError((run.stderr+run.stdout)[-600:])
    else:
     with socket.create_connection(('127.0.0.1',action['port']),timeout=1) as sock:sock.sendall(b'GET / HTTP/1.0\r\n\r\n')
    record('attempt',operation=kind,blocked=False)
   except OSError as error:record('attempt',operation=kind,blocked=True,errno=error.errno,detail=str(error))
  elif kind=='sleep':time.sleep(action.get('seconds',1))
  elif kind=='stderr':print(action['text'],file=sys.stderr,flush=True)
  elif kind=='spawn':
   child=subprocess.Popen([sys.executable,'-c',action['code']]);record('child',pid=child.pid)
 if not interrupted:completed()

for line in sys.stdin:
 frame=json.loads(line);record('input',frame=frame)
 method,params=frame.get('method'),frame.get('params') or {}
 if method=='initialize':result(frame,{'userAgent':'fixture'})
 elif method=='initialized':continue
 elif method=='config/read':result(frame,{'config':effective,'layers':[]})
 elif method=='model/list':result(frame,{'data':[{'id':'default','model':'default','displayName':'Default','isDefault':True,'supportedReasoningEfforts':[{'reasoningEffort':'high'}]},{'id':'fast','model':'fast','displayName':'Fast','supportedReasoningEfforts':[]}],'nextCursor':None})
 elif method in ('thread/start','thread/resume'):
  sid=params.get('threadId') or 'fixture-'+uuid.uuid4().hex
  record('thread',method=method,params=params,session_id=sid)
  transcript=profile/'sessions'/('rollout-fixture-'+sid+'.jsonl');transcript.parent.mkdir(exist_ok=True)
  metadata={'id':sid,'cwd':str(root)}
  if method=='thread/start':metadata['dynamic_tools']=params.get('dynamicTools',[])
  elif transcript.exists():metadata=json.loads(transcript.read_text().splitlines()[0])['payload']
  if method=='thread/start' or not transcript.exists():
   with transcript.open('a') as f:f.write(json.dumps({'type':'session_meta','payload':metadata})+'\n')
  dynamic_enabled=bool(metadata.get('dynamic_tools'))
  for key,value in (params.get('config') or {}).items():
   target=effective
   keys=key.split('.')
   for part in keys[:-1]:target=target.setdefault(part,{})
   if isinstance(value,dict) and isinstance(target.get(keys[-1]),dict):merge(target[keys[-1]],value)
   else:target[keys[-1]]=value
  entries=[(name,spec) for name,spec in effective.get('mcp_servers',{}).items() if isinstance(spec,dict) and spec.get('enabled',True) and spec.get('command')]
  if not metadata.get('dynamic_tools') and entries:
   relay_name,spec=next((name,spec) for name,spec in entries if spec.get('required'))
   record('relay_start',server=relay_name,command=spec['command'],args=spec.get('args',[]))
   relay=subprocess.Popen([spec['command'],*spec.get('args',[])],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,bufsize=1)
   relay_rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'fixture','version':'1'}},1)
   relay.stdin.write(json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'})+'\n');relay.stdin.flush()
   record('relay_tools',server=relay_name,response=relay_rpc('tools/list',{},2))
  result(frame,{'thread':{'id':sid,'sessionId':sid,'cwd':str(root),'turns':[],'cliVersion':'0.165.0','createdAt':int(time.time()),'updatedAt':int(time.time()),'ephemeral':False,'modelProvider':'fixture','preview':'','projectId':None,'source':'appServer','status':{'type':'idle'}},'model':params.get('model','default'),'modelProvider':'fixture','cwd':str(root),'approvalPolicy':'never','approvalsReviewer':'user','sandbox':{'type':'readOnly'}})
 elif method=='turn/start':
  tid='turn-'+uuid.uuid4().hex;record('user',frame=frame,argv=argv,cwd=str(root),session_id=sid)
  result(frame,{'turn':{'id':tid,'items':[],'status':'inProgress','error':None}})
  notify('turn/started',{'threadId':sid,'turn':{'id':tid,'items':[],'status':'inProgress'}})
  actions_for_turn()
 elif method=='turn/interrupt':result(frame,{});completed('interrupted');break
 elif 'id' in frame:emit({'id':frame['id'],'error':{'code':-32601,'message':'Unsupported fixture method'}})
''')
cli.chmod(0o755)
(work / 'agents.json').write_text(json.dumps({'default':'codex','providers':{
    'codex':{'type':'codex','label':'Codex fixture','bin':str(cli),
             'models':['default','fast','deep'],'default_model':'default'}}}))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'prism_local'))
import research
import server
import guarded_compile
from backend_codex import Codex

original_replace = research.replace_file
def replace_file(path, data, **kwargs):
    if (work / 'fail-persistence').exists():raise OSError('fixture research persistence unavailable')
    return original_replace(path, data, **kwargs)
research.replace_file = replace_file
original_compile = guarded_compile.guarded_compile
def controlled_compile(project, *, clean=False, cancel=None):
    if not (work / 'compile-stub').exists():return original_compile(project,clean=clean,cancel=cancel)
    with (work / 'compile-calls.jsonl').open('a') as log:log.write(json.dumps({'root':str(project.root),'clean':clean,'has_cancel':cancel is not None,'cancelled':bool(cancel and cancel.is_set())})+'\n')
    return {'busy':False,'exit':1,'main':'main.tex','cancelled':False,'timed_out':False,
            'diagnostics':[{'severity':'error','file':'main.tex','line':1,'message':'fixture guarded compile diagnostic'}],
            'output':'fixture diagnostic only','pdf_updated':False}
guarded_compile.guarded_compile=controlled_compile
server.set_root(root)
backend=Codex('codex',{'bin':str(cli),'models':['default','fast','deep'],'default_model':'default'})
server.AGENT.backends={'codex':backend};server.AGENT.default='codex'
srv=server.httpbase.listen(server.Handler,0,1);url=f'http://127.0.0.1:{srv.server_address[1]}/';server.AGENT.server_url=url
(work/'ready.json').write_text(json.dumps({'url':url,'root':str(root),'profile':str(profile),'legacy_session':legacy_session}));print(url,flush=True)
try:srv.serve_forever()
finally:srv.server_close();server.AGENT.shutdown()
