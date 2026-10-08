"""Opt-in native CLI acceptance with a temporary project. Deep Code calls only a synthetic local model and build server."""
import json, sys, tempfile, threading, os, re, argparse, shutil
from pathlib import Path
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'prism_local'))
sys.path.insert(0, str(REPO / 'tests'))
from backend_deepcode import DeepCode
from test_agent import manager, wait_done

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--bin', default=os.environ.get('DEEPCODE_BIN') or shutil.which('deepcode'))
options = parser.parse_args()
if not options.bin:
    parser.error('Pass --bin /path/to/deepcode or install Deep Code first')

class H(BaseHTTPRequestHandler):
    step=0; phase='edit'; builds=0; requests=[]; root=None
    def log_message(self,*a): pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path=='/api/build':
            H.builds+=1
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'exit':1,'diagnostics':[{'severity':'error','file':'main.tex','line':1,'message':'fixture'}]}).encode()); return
        H.requests.append(body)
        tools=[t['function']['name'] for t in body.get('tools',[])]
        if body['messages'][0].get('content','').startswith('When users ask you to perform tasks'):
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
            self.wfile.write(('data: '+json.dumps({'choices':[{'delta':{'content':'{"skillNames":[]}'},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n').encode()); return
        call=None
        if H.phase=='edit' and H.step==0:
            call=('read',{'file_path':str(H.root/'main.tex')})
        elif H.phase=='edit' and H.step==1:
            result=next(m['content'] for m in reversed(body['messages']) if m['role']=='tool')
            data=json.loads(result)
            snippet=data.get('snippet_id') or data.get('metadata',{}).get('snippet',{}).get('id')
            if not snippet:
                match=re.search(r'snippet_id["\s:=]+([\w-]+)',result)
                snippet=match.group(1) if match else None
            assert snippet,result
            call=('edit',{'snippet_id':snippet,'old_string':'original','new_string':'edited'})
        elif H.phase=='edit' and H.step==2:
            name=next(n for n in tools if 'prism' in n and 'compile' in n)
            call=(name,{})
        elif H.phase in ('ask','scope') and H.step==0:
            call=('read',{'file_path':str(H.root/('main.tex' if H.phase=='ask' else 'other.tex'))})
        elif H.phase=='ask' and H.step==1:
            call=('write',{'file_path':str(H.root/'main.tex'),'content':'forbidden\n'})
        elif H.phase=='scope' and H.step==1:
            call=('write',{'file_path':str(H.root/'other.tex'),'content':'outside-scope\n'})
        H.step+=1
        delta={'tool_calls':[{'index':0,'id':f'c{H.phase}{H.step}','type':'function','function':{'name':call[0],'arguments':json.dumps(call[1])}}]} if call else {'content':'fixture complete'}
        self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.end_headers()
        self.wfile.write(('data: '+json.dumps({'choices':[{'index':0,'delta':delta,'finish_reason':None}]})+'\n\n').encode())
        self.wfile.write(('data: '+json.dumps({'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls' if call else 'stop'}]})+'\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')

http=ThreadingHTTPServer(('127.0.0.1',0),H)
threading.Thread(target=http.serve_forever,daemon=True).start()
try:
  with tempfile.TemporaryDirectory(prefix='prism-deepcode-live-') as tmp:
    base=Path(tmp).resolve(); root=base/'project'; root.mkdir(); home=base/'home'; home.mkdir(); H.root=root
    for name in ('main.tex','other.tex'): (root/name).write_text('original\n')
    settings=root/'.deepcode/settings.json'; settings.parent.mkdir()
    original=json.dumps({'env':{'API_KEY':'fixture-key','BASE_URL':f'http://127.0.0.1:{http.server_port}'},'thinkingEnabled':False}).encode()
    settings.write_bytes(original)
    with mock.patch.dict(os.environ,{'HOME':str(home),'USERPROFILE':str(home),'DEEPCODE_API_KEY':'fixture-key','DEEPCODE_TELEMETRY_ENABLED':'false','DEEPCODE_DEBUG_LOG_ENABLED':'false'}):
      backend=DeepCode('deepcode',{'bin':options.bin})
      m=manager(root,lambda:['main.tex','other.tex'],deepcode=backend); m.server_url=f'http://127.0.0.1:{http.server_port}/'; sid=None
      results=[]
      for phase,mode,scope in [('edit','edit',['main.tex']),('ask','ask',None),('scope','edit',['main.tex'])]:
        H.phase=phase; H.step=0
        r=m.start('Use native tools for this acceptance fixture.',sid,mode,scope=scope)
        if 'error' in r: raise RuntimeError(r)
        job=m.jobs[r['job']]; done=wait_done(job,timeout=60); sid=done['session_id']
        print(json.dumps({'phase':phase,'done':done,'events':job.events}),flush=True)
        assert done['exit']==0,done
        assert settings.read_bytes()==original
        assert (root/'main.tex').read_text()=='edited\n'
        if phase=='ask': assert not done['changed']
        if phase=='scope': assert done['reverted']==['other.tex']
        results.append(done)
      assert H.builds==1,H.builds
      assert (root/'other.tex').read_text()=='original\n'
      print('DEEPCODE_NATIVE_RUNTIME_ACCEPTANCE_PASSED (synthetic local model/build)',flush=True)
finally: http.shutdown(); http.server_close()
