"""Controlled Deep Code CLI/settings/MCP wire behavior; no real CLI or model."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'prism_local'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backends import Job
from backend_deepcode import DeepCode
from research import ResearchKnowledge
from research_access import ResearchAccess
from test_research import project_with


@unittest.skipUnless(sys.platform == 'darwin', 'controlled native boundary is verified on macOS')
class DeepCodeResearchProtocol(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.profile = self.home/'.deepcode'; self.profile.mkdir()
        self.state = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.dict(os.environ, {'HOME':str(self.home),
            'PRISM_STATE_DIR':self.state, 'DEEPCODE_API_KEY':'fixture-only'}))
        self.project = project_with({'main.tex':'$x$'})
        self.knowledge = ResearchKnowledge(self.project); self.knowledge.initialize()
        self.job = Job(1)
        self.job.root, self.job.mode = self.project.root, 'edit'
        self.job.provider, self.job.prompt = 'deepcode','Continue the existing estimate.'
        self.job.files = self.project.list_files
        self.job.writable = lambda rel: rel == 'main.tex'
        self.job.research_access = ResearchAccess(self.knowledge)
        import snapshot
        self.job.before, _issues = snapshot.take(self.project.root)

    def backend(self, body):
        path=self.profile/'fixture.py'
        path.write_text('''import json,os,subprocess,sys,time,uuid
from pathlib import Path
profile=Path.home()/'.deepcode'
root=Path.cwd()
argv=sys.argv[1:]
if '--help' in argv:
 print('--exec --json --settings <file> --session-id <uuid> --resume <id>');sys.exit(0)
message=sys.stdin.read()
settings=Path(argv[argv.index('--settings')+1]) if '--settings' in argv else root/'.deepcode/settings.json'
cfg=json.loads(settings.read_text())
servers=cfg.get('mcpServers',{})
relay=next((s for s in servers.values() if '--request-pipe' in s.get('args',[])),None)
if relay is None:
 print('No controlled project tool was installed.');sys.exit(0)
assert '--json' in argv
assert '[Prism research baseline]' in message
sid=argv[argv.index('--resume')+1] if '--resume' in argv else argv[argv.index('--session-id')+1]
# The genuine native ownership record is checked by backend.check_session.
from hashlib import sha256
import re
legacy=re.sub(r'[\\\\/]', '-',str(root)).replace(':','')
if len(legacy)<=64:code=legacy
else:
 base=re.sub(r'-+','-',re.sub(r'[^A-Za-z0-9._-]','-',root.name)).strip('-.')
 prefix=re.sub(r'[-.]+$','',base[:47]) or 'project'
 code=prefix+'-'+sha256(str(root).encode()).hexdigest()[:16]
directory=profile/'projects'/code;directory.mkdir(parents=True,exist_ok=True)
with (directory/(sid+'.jsonl')).open('a') as log:
 if '--resume' not in argv:log.write(json.dumps({'role':'system','sessionId':sid,'content':'# Local Workspace Environment\\n\\n```json\\n'+json.dumps({'root path':str(root),'pwd':str(root)})+'\\n```'})+'\\n')
 log.write(json.dumps({'role':'user','content':message})+'\\n')
(directory/'sessions-index.json').write_text(json.dumps({'entries':[{'id':sid,'updateTime':int(time.time()*1000)}]}))
child=subprocess.Popen([relay['command'],*relay.get('args',[])],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
serial=0
def call(method,params=None,mid=None):
 global serial
 if mid is None:mid=serial
 serial=max(serial,mid+1)
 child.stdin.write(json.dumps({'jsonrpc':'2.0','id':mid,'method':method,'params':params or {}})+'\\n');child.stdin.flush()
 line=child.stdout.readline()
 if not line:sys.exit(2)
 return json.loads(line)
call('initialize',{'protocolVersion':'2025-03-26'},0)
call('tools/list')
''' + body + '''
child.stdin.close();child.wait(timeout=2)
print(json.dumps({'id':'reply','sessionId':sid,'role':'assistant','content':'Known $x$ remains available.'}))
''')
        return DeepCode('deepcode',{'bin':str(path)})

    def test_unknown_mcp_write_is_held_before_original_and_ends_turn(self):
        backend=self.backend('''
answer=call('tools/call',{'name':'write_file','arguments':{'path':'main.tex','content':'$z$'}})
(profile/'answer.json').write_text(json.dumps(answer))
''')
        result=backend.run(self.job)
        self.assertEqual((self.project.root/'main.tex').read_text(),'$x$')
        self.assertTrue(any(e['t']=='notation_proposal' for e in self.job.events),result)
        self.assertEqual(self.knowledge.status()['proposals'][0]['canonical'],'z')
        self.assertTrue(result.get('notation_pending'))
        self.assertFalse(self.job.cancel.is_set())
        self.assertFalse(any(e['t']=='text' for e in self.job.events))
        self.assertFalse((self.project.root/'.deepcode/settings.json').exists())

    def test_public_capability_requires_verified_native_boundary(self):
        backend=self.backend('')
        info=backend.info()
        self.assertTrue(info['research_initialization'])
        self.assertTrue(info['enforces_scope'])
        self.assertTrue(info['read_only_ask'])
        self.assertEqual(info['notation_guard'],{'application':True,'display':True,'proposals':True,'decisions':True})
        with mock.patch('claude_sandbox.availability',return_value={'supported':False,'reason':'Unavailable fixture runtime'}):
            info=backend.info()
            self.assertFalse(info['research_initialization'])
            self.assertFalse(any(info['notation_guard'].values()))
            self.assertEqual(backend.check_research(self.project.root),'Unavailable fixture runtime')

    def test_native_tool_and_reasoning_previews_are_checked_before_publication(self):
        backend=self.backend('''
with (directory/(sid+'.jsonl')).open('a') as log:
 for entry in [
  {'role':'assistant','tool_calls':[{'id':'native-read','function':{'name':'read_file','arguments':json.dumps({'path':'main.tex'})}}]},
  {'role':'tool','tool_call_id':'native-read','content':'Unsafe preview $z$'},
  {'role':'assistant','tool_calls':[{'id':'known-read','function':{'name':'read_file','arguments':json.dumps({'path':'main.tex'})}}]},
  {'role':'tool','tool_call_id':'known-read','content':'Known preview $x$'}
 ]:log.write(json.dumps(entry)+'\\n')
child.stdin.close();child.wait(timeout=2)
print(json.dumps({'id':'reply','sessionId':sid,'role':'assistant','content':'Known $x$ remains available.',
 'messageParams':{'reasoning_content':'Unsafe reasoning $z$'}}));sys.exit(0)
''')
        result=backend.run(self.job)
        self.assertEqual(result['exit'],0,result)
        visible=json.dumps(self.job.events)
        self.assertNotIn('$z$',visible)
        self.assertTrue(any(e['t']=='tool_result' and 'Known preview $x$' in e.get('preview','') for e in self.job.events))
        self.assertTrue(any(e['t']=='thinking' for e in self.job.events))

    def test_omitted_empty_native_content_is_a_complete_tool_only_reply(self):
        backend=self.backend('''
child.stdin.close();child.wait(timeout=2)
print(json.dumps({'id':'reply','sessionId':sid,'role':'assistant'}));sys.exit(0)
''')
        result=backend.run(self.job)
        self.assertEqual(result['exit'],0,result)
        self.assertFalse(result.get('is_error'),result)
        self.assertFalse(any(e['t']=='text' for e in self.job.events))

    def test_unsupported_cli_flags_refuse_the_controlled_entry_before_a_turn(self):
        backend=self.backend('')
        Path(backend.bin()).write_text("import sys\nprint('--exec --resume')\n")
        reason=backend.check_research(self.project.root)
        self.assertIn('controlled',reason or '')
        self.assertFalse(backend.info()['research_initialization'])

    def test_known_reply_without_initializing_the_required_parent_mcp_is_failure(self):
        backend=self.backend('')
        path=Path(backend.bin())
        path.write_text(path.read_text().replace("call('initialize',{'protocolVersion':'2025-03-26'},0)\ncall('tools/list')",'pass'))
        result=backend.run(self.job)
        self.assertTrue(result.get('is_error'),result)
        self.assertNotEqual(result['exit'],0)
        self.assertFalse(any(e['t']=='text' for e in self.job.events))

    def test_incremental_native_log_cannot_exceed_the_whole_turn_budget(self):
        backend=self.backend('''
for index in range(12):
 with (directory/(sid+'.jsonl')).open('a') as log:
  log.write(json.dumps({'role':'tool','tool_call_id':'large-'+str(index),'content':'x'*400000})+'\\n')
 time.sleep(.3)
''')
        result=backend.run(self.job)
        self.assertTrue(result.get('is_error'),result)
        self.assertNotEqual(result['exit'],0)
        self.assertFalse(any(e['t']=='text' for e in self.job.events))
        self.assertEqual((self.project.root/'main.tex').read_text(),'$x$')

    def test_notification_and_web_scripts_are_inert_even_with_environment_overrides(self):
        marker=self.profile/'native-script-ran'
        command='/usr/bin/touch '+str(marker)
        (self.profile/'settings.json').write_text(json.dumps({'notify':command,'webSearchTool':command}))
        self.enterContext(mock.patch.dict(os.environ,{'DEEPCODE_NOTIFY':command,'DEEPCODE_WEB_SEARCH_TOOL':command}))
        backend=self.backend('''
user=json.loads((profile/'settings.json').read_text())
for native,env in [('notify','DEEPCODE_NOTIFY'),('webSearchTool','DEEPCODE_WEB_SEARCH_TOOL')]:
 command=os.environ.get(env) or cfg.get(native) or user.get(native)
 subprocess.run(command,shell=True,check=False)
''')
        result=backend.run(self.job)
        self.assertEqual(result['exit'],0,result)
        self.assertFalse(marker.exists())
        self.assertEqual(json.loads((self.profile/'settings.json').read_text())['notify'],command)

    def test_parent_tools_cannot_replace_runtime_metadata_or_internal_aliases(self):
        path=self.project.root/'.deepcode/settings.json';path.parent.mkdir();path.write_text('{}')
        (self.project.root/'alias.json').symlink_to(path)
        self.job.writable=lambda rel: True
        backend=self.backend('''
answers=[]
for path in ('.deepcode/settings.json','alias.json'):
 answers.append(call('tools/call',{'name':'write_file','arguments':{'path':path,'content':'{"env":{"BASE_URL":"https://example.invalid"}}'}}))
(profile/'answers.json').write_text(json.dumps(answers))
''')
        result=backend.run(self.job)
        self.assertEqual(result['exit'],0,result)
        self.assertEqual(path.read_text(),'{}')
        self.assertTrue(all(value['result']['isError'] for value in json.loads((self.profile/'answers.json').read_text())))

    def test_partial_or_foreign_final_reply_is_failure_without_public_math(self):
        for payload in ('print(\'{"role":"assistant","content":"Partial $z$\')',
                        "print(json.dumps({'role':'assistant','sessionId':'foreign-session','content':'Foreign $x$'}))"):
            with self.subTest(payload=payload):
                backend=self.backend("child.stdin.close();child.wait(timeout=2)\n"+payload+"\nsys.exit(0)\n")
                result=backend.run(self.job)
                self.assertTrue(result.get('is_error'),result)
                self.assertNotEqual(result['exit'],0)
                self.assertFalse(any(e['t'] in ('text','delta') for e in self.job.events))
                self.assertEqual((self.project.root/'main.tex').read_text(),'$x$')

    def test_native_log_reader_refuses_a_replaced_symlink(self):
        from deepcode_research import Log
        private=Path(self.state)/'private-record'
        private.write_text(json.dumps({'role':'tool','content':'private witness'})+'\n')
        path=self.profile/'native-log.jsonl'
        log=Log(path,False)
        path.symlink_to(private)
        with self.assertRaises((OSError,ValueError)):
            log.read()
        self.assertEqual(private.read_text(),json.dumps({'role':'tool','content':'private witness'})+'\n')

    def test_old_category_permission_version_is_not_protected_settings_support(self):
        backend=self.backend('')
        with mock.patch('backend_deepcode.installed_version',return_value=(0,4,3)):
            self.assertIn('controlled',backend.check_research(self.project.root) or '')
            self.assertFalse(backend.info()['research_initialization'])

    def test_whole_file_write_requires_the_observed_version_after_a_manual_change(self):
        backend=self.backend('''
call('tools/call',{'name':'read_file','arguments':{'path':'main.tex'}})
(profile/'read-complete').touch()
while not (profile/'manual-done').exists():time.sleep(.01)
answer=call('tools/call',{'name':'write_file','arguments':{'path':'main.tex','content':'$x+x$'}})
(profile/'stale-answer.json').write_text(json.dumps(answer))
''')
        import threading,time
        def manual():
            deadline=time.monotonic()+3
            while not (self.profile/'read-complete').exists() and time.monotonic()<deadline:time.sleep(.01)
            (self.project.root/'main.tex').write_text('$x$\nHuman paragraph.\n')
            (self.profile/'manual-done').touch()
        writer=threading.Thread(target=manual);writer.start()
        try:result=backend.run(self.job)
        finally:writer.join(3)
        self.assertEqual(result['exit'],0,result)
        self.assertTrue(json.loads((self.profile/'stale-answer.json').read_text())['result']['isError'])
        self.assertEqual((self.project.root/'main.tex').read_text(),'$x$\nHuman paragraph.\n')

    def test_precise_agent_edit_updates_full_write_version_only_without_external_changes(self):
        for human in (False,True):
            with self.subTest(human=human):
                (self.project.root/'main.tex').write_text('$x$')
                backend=self.backend('''
call('tools/call',{'name':'read_file','arguments':{'path':'main.tex'}})
''' + ('''(profile/'read-complete').touch()
while not (profile/'manual-done').exists():time.sleep(.01)
''' if human else '') + '''
call('tools/call',{'name':'edit_file','arguments':{'path':'main.tex','old_string':'$x$','new_string':'$x+x$'}})
answer=call('tools/call',{'name':'write_file','arguments':{'path':'main.tex','content':'$x+x+x$'}})
(profile/'combo-answer.json').write_text(json.dumps(answer))
''')
                import threading,time
                def manual():
                    deadline=time.monotonic()+3
                    while not (self.profile/'read-complete').exists() and time.monotonic()<deadline:time.sleep(.01)
                    (self.project.root/'main.tex').write_text('$x$\nHuman paragraph.\n')
                    (self.profile/'manual-done').touch()
                writer=threading.Thread(target=manual) if human else None
                if writer:writer.start()
                try:result=backend.run(self.job)
                finally:
                    if writer:writer.join(3)
                self.assertEqual(result['exit'],0,result)
                self.assertEqual(json.loads((self.profile/'combo-answer.json').read_text())['result']['isError'],human)
                self.assertEqual((self.project.root/'main.tex').read_text(),'$x+x$\nHuman paragraph.\n' if human else '$x+x+x$')

    def test_fragmented_final_json_cannot_publish_new_math_before_complete_review(self):
        backend=self.backend('''
child.stdin.close();child.wait(timeout=2)
value=json.dumps({'id':'reply','sessionId':sid,'role':'assistant','content':'Unchecked $z$ is established.'})
cut=value.index('$z')+1
for fragment in (value[:cut],value[cut:]):
 sys.stdout.write(fragment);sys.stdout.flush();time.sleep(.05)
sys.stdout.write('\\n');sys.stdout.flush();sys.exit(0)
''')
        result=backend.run(self.job)
        self.assertTrue(result.get('notation_pending'),result)
        self.assertFalse(any(e['t'] in ('text','delta') for e in self.job.events))
        self.assertTrue(any(p['canonical']=='z' and p['status']=='pending' for p in self.knowledge.status()['proposals']))

    def test_user_telemetry_and_debug_defaults_are_disabled_in_the_controlled_exec(self):
        (self.profile/'settings.json').write_text(json.dumps({'telemetryEnabled':True,'debugLogEnabled':True}))
        self.enterContext(mock.patch.dict(os.environ,{'DEEPCODE_TELEMETRY_ENABLED':'true','DEEPCODE_DEBUG_LOG_ENABLED':'true'}))
        backend=self.backend('''
user=json.loads((profile/'settings.json').read_text())
for native,env in [('telemetryEnabled','DEEPCODE_TELEMETRY_ENABLED'),('debugLogEnabled','DEEPCODE_DEBUG_LOG_ENABLED')]:
 value=os.environ[env] if env in os.environ else cfg[native] if native in cfg else user.get(native,False)
 enabled=value if isinstance(value,bool) else value.lower()=='true'
 if enabled:(profile/(native+'-enabled')).touch()
''')
        result=backend.run(self.job)
        self.assertEqual(result['exit'],0,result)
        self.assertFalse((self.profile/'telemetryEnabled-enabled').exists())
        self.assertFalse((self.profile/'debugLogEnabled-enabled').exists())
        self.assertTrue(json.loads((self.profile/'settings.json').read_text())['telemetryEnabled'])

    def test_fifo_settings_fail_preflight_without_waiting_for_a_writer(self):
        import subprocess
        for path in (self.profile/'settings.json',self.project.root/'.deepcode/settings.json'):
            with self.subTest(path=str(path)):
                path.parent.mkdir(exist_ok=True)
                os.mkfifo(path)
                code="import sys;sys.path.insert(0,"+repr(str(Path(__file__).resolve().parents[1]/'prism_local'))+");from backend_deepcode import DeepCode;from pathlib import Path;print(DeepCode('deepcode').research_preflight(Path("+repr(str(self.project.root))+")))"
                try:
                    result=subprocess.run([sys.executable,'-c',code],text=True,capture_output=True,timeout=2)
                    self.assertEqual(result.returncode,0,result.stderr)
                    self.assertIn('settings could not be verified',result.stdout)
                finally:path.unlink()

    def test_non_json_numeric_settings_fail_before_the_native_turn(self):
        backend=self.backend('')
        config=self.project.root/'.deepcode/settings.json'
        config.parent.mkdir()
        config.write_text('{"temperature":NaN}')
        with self.assertRaises(ValueError):
            backend.run(self.job)
        from backend_deepcode import sessions_dir
        self.assertFalse(sessions_dir(self.project.root).exists())

    def test_fifo_native_index_cannot_block_session_validation(self):
        import subprocess
        from backend_deepcode import sessions_dir
        directory=sessions_dir(self.project.root);directory.mkdir(parents=True)
        os.mkfifo(directory/'sessions-index.json')
        code="import sys;sys.path.insert(0,"+repr(str(Path(__file__).resolve().parents[1]/'prism_local'))+");from backend_deepcode import DeepCode;from pathlib import Path;print(DeepCode('deepcode').check_session(Path("+repr(str(self.project.root))+"),'fixture-session'))"
        result=subprocess.run([sys.executable,'-c',code],text=True,capture_output=True,timeout=2)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('does not belong',result.stdout)

    def test_unverified_session_cannot_publish_even_a_known_final_reply(self):
        backend=self.backend('''
header={'role':'system','sessionId':sid,'content':'# Local Workspace Environment\\n\\n```json\\n'+json.dumps({'root path':'/another/project','pwd':'/another/project'})+'\\n```'}
(directory/(sid+'.jsonl')).write_text(json.dumps(header)+'\\n')
''')
        result=backend.run(self.job)
        self.assertTrue(result.get('is_error'),result)
        self.assertNotEqual(result['exit'],0)
        self.assertFalse(any(e['t'] in ('text','delta') for e in self.job.events))

    def test_invalid_final_metadata_is_rejected_before_the_known_text_is_published(self):
        backend=self.backend('''
child.stdin.close();child.wait(timeout=2)
print(json.dumps({'id':'reply','sessionId':sid,'role':'assistant','content':'Known $x$ remains available.',
 'messageParams':['invalid']}));sys.exit(0)
''')
        result=backend.run(self.job)
        self.assertTrue(result.get('is_error'),result)
        self.assertNotEqual(result['exit'],0)
        self.assertFalse(any(e['t'] in ('text','delta') for e in self.job.events))
