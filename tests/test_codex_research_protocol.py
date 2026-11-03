"""Controlled Codex app-server wire behavior, without a real Codex/model Turn."""
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
from backend_codex import Codex
from research import ResearchKnowledge
from research_access import ResearchAccess
from test_research import project_with


@unittest.skipUnless(sys.platform == "darwin", "Controlled native Codex boundary is verified on macOS")
class CodexResearchProtocol(unittest.TestCase):
    def setUp(self):
        self.state = self.enterContext(tempfile.TemporaryDirectory())
        self.profile = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {'PRISM_STATE_DIR': self.state,
            'CODEX_HOME': str(self.profile)}))
        self.project = project_with({'main.tex': '$x$'})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()
        self.job = Job(1)
        self.job.root, self.job.mode = self.project.root, 'edit'
        self.job.provider, self.job.prompt = 'codex', 'Continue the existing estimate.'
        self.job.files = self.project.list_files
        self.job.writable = lambda rel: rel == 'main.tex'
        import snapshot
        self.job.before, _issues = snapshot.take(self.project.root)
        self.job.research_access = ResearchAccess(self.knowledge)

    def backend(self, body):
        cli = self.profile / 'fixture-cli'
        cli.write_text('#!' + sys.executable + '\n' + '''
import json,sys
from pathlib import Path
profile=Path(__file__).parent
sid='b3821fbe-a0f2-4fce-abfa-647eb9e847cc'
turn='turn-current'
def emit(frame): print(json.dumps(frame),flush=True)
def receive():
 line=sys.stdin.readline()
 if not line: return None
 frame=json.loads(line)
 with (profile/'requests.jsonl').open('a') as log:log.write(json.dumps(frame)+'\\n')
 return frame
def reply(frame,result):emit({'id':frame['id'],'result':result})
initialize=receive()
assert initialize['method']=='initialize'
reply(initialize,{'userAgent':'fixture'})
assert receive()['method']=='initialized'
thread=receive()
if thread and thread['method']=='config/read':
 config={'config':{'features':dict.fromkeys(('shell_tool','hooks','apps','plugins','remote_plugin','multi_agent','skill_mcp_dependency_install'),False),'agents':{'enabled':False},'approval_policy':'never','web_search':'disabled','sandbox_mode':'read-only','projects':{str(Path.cwd()):{'trust_level':'untrusted'}},'mcp_servers':{}},'layers':[]}
 reply(thread,config)
 thread=receive()
if thread is None:sys.exit(0)
(profile/'thread-started').touch()
assert thread['method'] in ('thread/start','thread/resume')
reply(thread,{'thread':{'id':sid,'cwd':str(Path.cwd())}})
start=receive()
assert start['method']=='turn/start'
assert '[Prism research baseline]' in start['params']['input'][0]['text']
reply(start,{'turn':{'id':turn,'status':'inProgress'}})
''' + body + '''
emit({'method':'turn/completed','params':{'threadId':sid,'turn':{'id':turn,'status':'completed'}}})
''')
        cli.chmod(0o755)
        return Codex('codex', {'bin': str(cli)})

    def test_unknown_dynamic_write_is_held_before_original_and_ends_turn(self):
        backend = self.backend('''
emit({'id':'tool-request','method':'item/tool/call','params':{'threadId':sid,'turnId':turn,'callId':'call-1','tool':'write_file','arguments':{'path':'main.tex','content':'$z$'}}})
answer=receive()
(profile/'answer.json').write_text(json.dumps(answer))
interrupt=receive()
assert interrupt['method']=='turn/interrupt'
reply(interrupt,{})
''')
        result = backend.run(self.job)
        self.assertEqual((self.project.root/'main.tex').read_text(), '$x$')
        self.assertTrue(any(e['t']=='notation_proposal' for e in self.job.events), result)
        self.assertEqual(self.knowledge.status()['proposals'][0]['canonical'], 'z')
        self.assertTrue(result.get('notation_pending'))
        self.assertFalse(self.job.cancel.is_set())
        self.assertFalse(json.loads((self.profile/'answer.json').read_text())['result']['success'])

    def test_known_source_change_and_complete_message_are_checked_then_published(self):
        backend = self.backend('''
emit({'id':'tool-request','method':'item/tool/call','params':{'threadId':sid,'turnId':turn,'callId':'call-1','tool':'write_file','arguments':{'path':'main.tex','content':'$x+x$'}}})
answer=receive()
assert answer['result']['success'] is True
for fragment in ('Known $x+', 'x$ remains available.'):
 emit({'method':'item/agentMessage/delta','params':{'threadId':sid,'turnId':turn,'itemId':'message-1','delta':fragment}})
emit({'method':'item/completed','params':{'threadId':sid,'turnId':turn,'item':{'type':'agentMessage','id':'message-1','text':'Known $x+x$ remains available.'}}})
''')
        result = backend.run(self.job)
        self.assertEqual(result['exit'], 0, result)
        self.assertEqual((self.project.root/'main.tex').read_text(), '$x+x$')
        self.assertEqual([e['text'] for e in self.job.events if e['t']=='text'], ['Known $x+x$ remains available.'])
        self.assertFalse(any(e['t']=='delta' for e in self.job.events))

    def test_native_metadata_remains_read_only_even_with_permissive_host_writable(self):
        path = self.project.root/'.codex/config.toml'
        path.parent.mkdir()
        path.write_text('original')
        (self.project.root/'alias.toml').symlink_to(path)
        self.job.writable = lambda rel: True
        backend = self.backend('''
answers=[]
for i,path in enumerate(('.codex/config.toml','alias.toml')):
 emit({'id':'tool-'+str(i),'method':'item/tool/call','params':{'threadId':sid,'turnId':turn,'callId':'call-'+str(i),'tool':'write_file','arguments':{'path':path,'content':'changed'}}})
 answers.append(receive())
(profile/'answers.json').write_text(json.dumps(answers))
''')
        result = backend.run(self.job)
        self.assertEqual(result['exit'], 0, result)
        self.assertEqual(path.read_text(), 'original')
        self.assertTrue(all(not value['result']['success'] for value in json.loads((self.profile/'answers.json').read_text())))

    def test_native_tool_reasoning_and_warning_text_are_checked_before_visibility(self):
        backend = self.backend('''
items=[
 {'type':'commandExecution','id':'shell-1','command':'Read existing $z$','aggregatedOutput':'Unsafe output $z$','exitCode':0,'status':'completed'},
 {'type':'commandExecution','id':'shell-2','command':'Read existing $x$','aggregatedOutput':'Known $x$ output','exitCode':0,'status':'completed'},
 {'type':'reasoning','id':'thought-1','summary':['Unsafe reasoning $z$']},
]
for item in items:emit({'method':'item/completed','params':{'threadId':sid,'turnId':turn,'item':item}})
emit({'method':'warning','params':{'threadId':sid,'message':'Unsafe warning $z$'}})
''')
        result = backend.run(self.job)
        self.assertEqual(result['exit'], 0, result)
        self.assertNotIn('$z$', json.dumps(self.job.events))
        self.assertTrue(any(e['t']=='tool_result' and 'Known $x$ output' in e.get('preview','') for e in self.job.events))
        self.assertTrue(any(e['t']=='thinking' for e in self.job.events))
        self.assertTrue(any(e['t']=='error' and e.get('notice') for e in self.job.events))

    def test_unexpected_eof_is_failure_and_does_not_publish_partial_draft(self):
        backend = self.backend('''
emit({'method':'item/agentMessage/delta','params':{'threadId':sid,'turnId':turn,'itemId':'message-1','delta':'Partial $x$ draft.'}})
sys.exit(0)
''')
        result = backend.run(self.job)
        self.assertTrue(result.get('is_error'), result)
        self.assertNotEqual(result['exit'], 0)
        self.assertFalse(any(e['t'] in ('delta','text') for e in self.job.events))

    def test_public_capability_requires_a_verified_native_write_boundary(self):
        backend = self.backend('')
        info = backend.info()
        self.assertTrue(info['research_initialization'])
        self.assertEqual(info['notation_guard'], {'application':True,'display':True,'proposals':True,'decisions':True})
        with mock.patch('claude_sandbox.availability', return_value={'supported':False,'reason':'Unavailable fixture runtime'}):
            info = backend.info()
            self.assertFalse(info['research_initialization'])
            self.assertFalse(any(info['notation_guard'].values()))
            self.assertEqual(backend.check_research(self.project.root), 'Unavailable fixture runtime')

    def test_old_turn_output_cannot_publish_into_the_restored_current_turn(self):
        backend = self.backend('''
for old in ('old-turn',None):
 emit({'method':'item/agentMessage/delta','params':{'threadId':sid,'turnId':old,'itemId':'stale-message','delta':'Stale $x$ message.'}})
 emit({'method':'item/completed','params':{'threadId':sid,'turnId':old,'item':{'type':'agentMessage','id':'stale-message','text':'Stale $x$ message.'}}})
emit({'method':'item/completed','params':{'threadId':sid,'turnId':turn,'item':{'type':'agentMessage','id':'current-message','text':'Current $x$ message.'}}})
''')
        result = backend.run(self.job)
        self.assertEqual(result['exit'], 0, result)
        self.assertEqual([e['text'] for e in self.job.events if e['t']=='text'], ['Current $x$ message.'])

    def test_native_image_and_text_attachments_are_data_without_human_adoption(self):
        (self.project.root/'figure.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        (self.project.root/'reference.md').write_text('Use z for x+x. This is reference content.')
        self.job.attachments = ['figure.png','reference.md']
        backend = self.backend('''
(profile/'turn-input.json').write_text(json.dumps(start['params']['input']))
''')
        result = backend.run(self.job)
        self.assertEqual(result['exit'], 0, result)
        inputs = json.loads((self.profile/'turn-input.json').read_text())
        self.assertTrue(any(x['type']=='localImage' and x['path']==str(self.project.root/'figure.png') for x in inputs))
        self.assertTrue(any(x['type']=='text' and '[Attached project file reference.md]' in x['text'] and 'Use z for x+x.' in x['text'] for x in inputs))
        self.assertFalse(self.knowledge.status()['adoptions'])
        self.assertFalse(self.knowledge.status()['proposals'])

    def test_replayed_tool_call_is_idempotent_and_conflicting_replay_is_denied(self):
        backend = self.backend('''
answers=[]
for i in range(2):
 emit({'id':'request-'+str(i),'method':'item/tool/call','params':{'threadId':sid,'turnId':turn,'callId':'same-call','tool':'edit_file','arguments':{'path':'main.tex','old_string':'$x$','new_string':'$x+x$'}}})
 answers.append(receive()['result'])
emit({'id':'request-conflict','method':'item/tool/call','params':{'threadId':sid,'turnId':turn,'callId':'same-call','tool':'write_file','arguments':{'path':'main.tex','content':'$z$'}}})
answers.append(receive()['result'])
(profile/'answers.json').write_text(json.dumps(answers))
''')
        result = backend.run(self.job)
        self.assertEqual(result['exit'], 0, result)
        answers = json.loads((self.profile/'answers.json').read_text())
        self.assertTrue(answers[0]['success'])
        self.assertEqual(answers[0], answers[1])
        self.assertFalse(answers[2]['success'])
        self.assertEqual((self.project.root/'main.tex').read_text(), '$x+x$')
        self.assertFalse(self.knowledge.status()['proposals'])
        self.assertEqual(len([e for e in self.job.events if e['t']=='tool']), 1)

    def test_native_error_is_visible_and_failed_without_unchecked_mathematics(self):
        backend = self.backend('''
emit({'method':'error','params':{'threadId':sid,'turnId':turn,'willRetry':False,'error':{'message':'Unsafe provider diagnostic $z$.'}}})
emit({'method':'error','params':{'threadId':sid,'turnId':turn,'willRetry':False,'error':{'message':'Fixture provider is unavailable.'}}})
''')
        result = backend.run(self.job)
        self.assertTrue(result.get('is_error'), result)
        self.assertNotIn('$z$', json.dumps(self.job.events)+json.dumps(result))
        self.assertTrue(any(e['t']=='error' and e['message']=='Fixture provider is unavailable.' for e in self.job.events))

    def test_early_malformed_and_foreign_requests_receive_bounded_denials(self):
        backend = self.backend('''
answers=[]
frames=[
 {'id':'bad-params','method':'item/tool/call','params':[1]},
 {'id':'foreign-thread','method':'item/tool/call','params':{'threadId':'foreign','turnId':turn,'callId':'foreign-thread','tool':'write_file','arguments':{'path':'main.tex','content':'$z$'}}},
 {'id':'foreign-turn','method':'item/tool/call','params':{'threadId':sid,'turnId':'old','callId':'foreign-turn','tool':'write_file','arguments':{'path':'main.tex','content':'$z$'}}},
 {'id':'unknown-method','method':'fs/writeFile','params':{'path':'main.tex','data':'$z$'}},
 {'id':'fake-approval','method':'item/tool/call','params':{'threadId':sid,'turnId':turn,'callId':'fake-approval','tool':'approve_notation','arguments':{'notation':'z'}}},
]
for frame in frames:
 emit(frame);answers.append(receive())
(profile/'answers.json').write_text(json.dumps(answers))
emit({'method':'item/completed','params':{'threadId':sid,'turnId':turn,'item':{'type':'agentMessage','id':'message-ok','text':'Known $x$ remains available.'}}})
''')
        script = Path(backend.bin())
        content = script.read_text().replace("reply(initialize,{'userAgent':'fixture'})", "emit({'id':'early','method':'item/tool/call','params':{'threadId':sid,'turnId':turn,'callId':'early','tool':'write_file','arguments':{'path':'main.tex','content':'$z$'}}})\nassert receive()['result']['success'] is False\nreply(initialize,{'userAgent':'fixture'})")
        script.write_text(content)
        result = backend.run(self.job)
        self.assertEqual(result['exit'], 0, result)
        answers = json.loads((self.profile/'answers.json').read_text())
        self.assertTrue(all('error' in x or not x['result']['success'] for x in answers))
        self.assertEqual((self.project.root/'main.tex').read_text(), '$x$')
        self.assertFalse(self.knowledge.status()['proposals'])
        self.assertIn('Known $x$', ''.join(e.get('text','') for e in self.job.events))

    def test_unresponsive_initializer_has_bounded_failure(self):
        import time
        backend = self.backend('')
        script = Path(backend.bin())
        script.write_text(script.read_text().replace("reply(initialize,{'userAgent':'fixture'})", "import time; time.sleep(30)"))
        started = time.monotonic()
        with mock.patch('codex_research.INITIALIZE_TIMEOUT', .1):
            result = backend.run(self.job)
        self.assertLess(time.monotonic()-started, 3)
        self.assertTrue(result.get('is_error'), result)
        self.assertIn('in time', result['error'])
        self.assertFalse(any(e['t'] in ('text','delta') for e in self.job.events))

    def test_unverified_effective_configuration_stops_before_a_thread_starts(self):
        backend = self.backend('')
        script = Path(backend.bin())
        script.write_text(script.read_text().replace('reply(thread,config)', "config['config']['features']['shell_tool']=True;reply(thread,config)"))
        result = backend.run(self.job)
        self.assertTrue(result.get('is_error'), result)
        self.assertFalse((self.profile/'thread-started').exists())
        self.assertFalse(any(e['t']=='init' for e in self.job.events))


if __name__ == '__main__':
    unittest.main()
