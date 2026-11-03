"""Isolated real editor/Deep Code adapter; no native CLI, model or service."""
import json
import os
from pathlib import Path
import sys

work = Path(sys.argv[1]).resolve()
root, home = work/'project', work/'home'
profile = home/'.deepcode'
root.mkdir(parents=True, exist_ok=True)
profile.mkdir(parents=True, exist_ok=True)
if '--empty' not in sys.argv and not (root/'main.tex').exists():
    (root/'sections').mkdir(exist_ok=True)
    (root/'main.tex').write_text('\\documentclass{article}\n\\begin{document}\n'
        '\\emph{compact} is a finite covering property.\n'
        'Existing $\\ell+\\ell_+$.\nLet $x$ be a real number.\n'
        '\\input{sections/intro}\n\\end{document}\n')
    (root/'sections/intro.tex').write_text('\\section{Intro}\nExisting $x$.\n')
os.environ.update(HOME=str(home), PRISM_STATE_DIR=str(work/'state'),
    PRISM_AUTO_UPDATE='0', PRISM_SNIPPETS=str(work/'snippets.js'),
    PRISM_AGENTS=str(work/'agents.json'), DEEPCODE_API_KEY='fixture-only')
if (work/'runtime-env.json').exists():os.environ.update(json.loads((work/'runtime-env.json').read_text()))
cli=profile/'deepcode-fixture.py'
cli.write_text('#!' + sys.executable + '\n' + r'''
import hashlib,json,os,re,socket,subprocess,sys,time,uuid
from pathlib import Path
profile=Path.home()/'.deepcode'
root=Path.cwd().resolve()
argv=sys.argv[1:]
def record(kind,**data):
 with (profile/'cli-requests.jsonl').open('a') as f:f.write(json.dumps({'kind':kind,'pid':os.getpid(),**data})+'\n')
if '--version' in argv:print('Deep Code 0.4.5 (fixture)');sys.exit(0)
if '--help' in argv:print('--exec --prompt --resume --session-id --settings --json');sys.exit(0)
def option(name,default=None):return argv[argv.index(name)+1] if name in argv else default
if '--settings' not in argv:raise SystemExit('The controlled fixture requires explicit settings.')
settings_path=Path(option('--settings'))
if not settings_path.is_absolute():settings_path=root/settings_path
project_settings=json.loads(settings_path.read_text())
user_settings=json.loads((profile/'settings.json').read_text()) if (profile/'settings.json').exists() else {}
settings={**user_settings,**project_settings,'mcpServers':{**user_settings.get('mcpServers',{}),**project_settings.get('mcpServers',{})},'env':{**user_settings.get('env',{}),**project_settings.get('env',{})}}
message=sys.stdin.read()
sid=option('--resume') or option('--session-id') or 'fixture-'+uuid.uuid4().hex
legacy=re.sub(r'[\\/]', '-', str(root)).replace(':','')
if len(legacy)<=64:code=legacy
else:
 key=str(root).lower() if os.name=='nt' else str(root)
 digest=hashlib.sha256(key.encode()).hexdigest()[:16]
 base=re.sub(r'-+','-',re.sub(r'[^A-Za-z0-9._-]','-',root.name)).strip('-.')
 prefix=re.sub(r'[-.]+$','',base[:47]) or 'project'
 code=prefix+'-'+digest
directory=profile/'projects'/code;directory.mkdir(parents=True,exist_ok=True)
transcript=directory/(sid+'.jsonl')
def append(value):
 value={'id':uuid.uuid4().hex,'sessionId':sid,'createTime':int(time.time()*1000),**value}
 with transcript.open('a') as f:f.write(json.dumps(value)+'\n')
 return value
servers=[(name,spec) for name,spec in settings.get('mcpServers',{}).items() if isinstance(spec,dict) and spec.get('command')]
if len(servers)!=1:raise SystemExit('The fixture requires one actual checked MCP server.')
name,spec=servers[0]
record('relay_start',server=name,command=spec['command'],args=spec.get('args',[]))
if (profile/'startup-delay').exists():time.sleep(float((profile/'startup-delay').read_text()))
relay=subprocess.Popen([spec['command'],*spec.get('args',[])],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,bufsize=1)
def rpc(method,params,mid):
 relay.stdin.write(json.dumps({'jsonrpc':'2.0','id':mid,'method':method,'params':params})+'\n');relay.stdin.flush()
 for line in relay.stdout:
  answer=json.loads(line)
  if answer.get('id')==mid:return answer
 raise RuntimeError('The actual MCP helper ended before answering')
rpc('initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'deepcode-fixture','version':'1'}},1)
relay.stdin.write(json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'})+'\n');relay.stdin.flush()
record('relay_tools',answer=rpc('tools/list',{},2))
if not transcript.exists():append({'role':'system','content':'# Local Workspace Environment\n\n```json\n'+json.dumps({'root path':str(root),'pwd':str(root)})+'\n```'})
index_path=directory/'sessions-index.json';index=json.loads(index_path.read_text()) if index_path.exists() else {'entries':[]}
index['originalPath']=str(root);index['entries']=[e for e in index['entries'] if e.get('id')!=sid]+[{'id':sid,'updateTime':int(time.time()*1000)}];index_path.write_text(json.dumps(index))
record('user',argv=argv,message=message,session_id=sid,cwd=str(root),model=os.environ.get('DEEPCODE_MODEL'),effort=os.environ.get('DEEPCODE_REASONING_EFFORT'),settings_path=str(settings_path),session_path=str(transcript),home=str(Path.home()))
append({'role':'user','content':message})
actions=json.loads((profile/'cli-script.json').read_text()) if (profile/'cli-script.json').exists() else [{'type':'text','text':'Existing material can be discussed.'}]
reply=''
final_fields={}
chunked=False
for number,action in enumerate(actions):
 kind=action['type']
 if kind=='mcp':
  fn={'name':'mcp__'+name+'__'+action['name'],'arguments':json.dumps(action.get('arguments',{}))}
  call_id='fixture-call-'+str(number)
  append({'role':'assistant','content':'','messageParams':{'tool_calls':[{'id':call_id,'type':'function','function':fn}]}})
  answer=rpc('tools/call',{'name':action['name'],'arguments':action.get('arguments',{})},number+10)
  record('mcp_result',name=action['name'],answer=answer,transport='stdio_mcp')
  append({'role':'tool','content':json.dumps(answer.get('result',{})),'messageParams':{'tool_call_id':call_id},'meta':{'function':fn}})
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
 elif kind=='mark':Path(action['path']).write_text('started')
 elif kind=='wait':
  deadline=time.monotonic()+5
  while not Path(action['path']).exists() and time.monotonic()<deadline:time.sleep(.01)
  if not Path(action['path']).exists():raise RuntimeError('Fixture release timed out')
 elif kind=='sleep':time.sleep(action.get('seconds',1))
 elif kind=='text':reply+=action['text']
 elif kind=='delta':reply+=action['text'];chunked=True
 elif kind=='websearch':
  command=os.environ.get('DEEPCODE_WEB_SEARCH_TOOL') or settings.get('webSearchTool')
  if command:
   operation=subprocess.run(command,shell=True,capture_output=True,text=True)
   record('script',operation='websearch',command=command,exit=operation.returncode)
 elif kind=='session':append(action['message'])
 elif kind=='final':final_fields.update(action['message'])
 elif kind=='stderr':print(action['text'],file=sys.stderr,flush=True)
 elif kind=='spawn':
  child=subprocess.Popen([sys.executable,'-c',action['code']]);record('child',pid=child.pid)
 else:raise RuntimeError('Unsupported fixture action '+kind)
final=append({'role':'assistant','content':reply,**final_fields})
def clean(value):
 if isinstance(value,dict):return {key:clean(item) for key,item in value.items() if item is not None and item!='' and item!=[] and item!={}}
 if isinstance(value,list):return [clean(item) for item in value]
 return value
if '--json' in argv:
 value=json.dumps(clean(final))
 if chunked:
  for offset in range(0,len(value),9):sys.stdout.write(value[offset:offset+9]);sys.stdout.flush();time.sleep(.002)
  print(flush=True)
 else:print(value,flush=True)
else:print(reply,flush=True)
notify=os.environ.get('DEEPCODE_NOTIFY') or settings.get('notify')
if notify:
 operation=subprocess.run(notify,shell=True,capture_output=True,text=True)
 record('script',operation='notify',command=notify,exit=operation.returncode)
relay.stdin.close();relay.wait(timeout=3)
''')
(work/'agents.json').write_text(json.dumps({'default':'deepcode','providers':{
    'deepcode':{'type':'deepcode','bin':str(cli),'models':['default','fast','deep'],'default_model':'default'}}}))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'prism_local'))
import server
import research
import guarded_compile
from backend_deepcode import DeepCode
original_replace=research.replace_file
def replace_file(path,data,**kwargs):
    if (work/'fail-persistence').exists():raise OSError('fixture research persistence unavailable')
    return original_replace(path,data,**kwargs)
research.replace_file=replace_file
original_compile=guarded_compile.guarded_compile
def controlled_compile(project,*,clean=False,cancel=None):
    if not (work/'compile-stub').exists():return original_compile(project,clean=clean,cancel=cancel)
    with (work/'compile-calls.jsonl').open('a') as log:log.write(json.dumps({'root':str(project.root),'clean':clean,'has_cancel':cancel is not None,'cancelled':bool(cancel and cancel.is_set())})+'\n')
    return {'busy':False,'exit':1,'main':'main.tex','cancelled':False,'timed_out':False,
            'diagnostics':[{'severity':'error','file':'main.tex','line':1,'message':'fixture guarded compile diagnostic'}],
            'output':'fixture diagnostic only','pdf_updated':False}
guarded_compile.guarded_compile=controlled_compile
server.set_root(root)
backend=DeepCode('deepcode',{'bin':str(cli),'models':['default','fast','deep'],'default_model':'default'})
server.AGENT.backends={'deepcode':backend};server.AGENT.default='deepcode'
srv=server.httpbase.listen(server.Handler,0,1);url=f'http://127.0.0.1:{srv.server_address[1]}/';server.AGENT.server_url=url
(work/'ready.json').write_text(json.dumps({'url':url,'root':str(root),'profile':str(profile)}));print(url,flush=True)
try:srv.serve_forever()
finally:srv.server_close();server.AGENT.shutdown()
