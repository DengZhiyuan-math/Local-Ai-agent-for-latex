"""Protected Claude SDK-MCP wire behavior; no installed CLI or model calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'prism_local'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backends import Job
from research import ResearchKnowledge
from research_access import ResearchAccess
from test_research import project_with


class ClaudeResearchProtocol(unittest.TestCase):
    def setUp(self):
        self.state = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.dict(os.environ, {'PRISM_STATE_DIR': self.state}))
        self.project = project_with({'main.tex': '$x$'})
        self.knowledge = ResearchKnowledge(self.project)
        self.knowledge.initialize()
        self.job = Job(1)
        self.job.root, self.job.mode = self.project.root, 'edit'
        self.job.files = self.project.list_files
        self.job.writable = lambda rel: rel == 'main.tex'
        import snapshot
        self.job.before, _issues = snapshot.take(self.project.root)
        self.job.research_access = ResearchAccess(self.knowledge)

    def controller(self):
        from claude_research import Controller
        return Controller(self.job)

    @staticmethod
    def call(name, arguments, mid=1):
        return {'jsonrpc': '2.0', 'id': mid, 'method': 'tools/call',
                'params': {'name': name, 'arguments': arguments}}

    def test_sdk_tool_writes_known_draft_through_shared_check(self):
        guard = self.controller()
        result = guard.answer(self.call('write_file', {'path': 'main.tex', 'content': '$x+x$'}))
        self.assertFalse(result['result']['isError'])
        self.assertEqual((self.project.root / 'main.tex').read_text(), '$x+x$')

    def test_unknown_draft_is_held_before_original_and_all_later_calls_stop(self):
        guard = self.controller()
        guard.answer(self.call('write_file', {'path':'main.tex','content':'$z$'}))
        self.assertEqual((self.project.root/'main.tex').read_text(), '$x$')
        self.assertTrue(guard.halted)
        self.assertTrue(any(e['t']=='notation_proposal' for e in self.job.events))
        guard.answer(self.call('write_file', {'path':'main.tex','content':'$x+x$'}, 2))
        self.assertEqual((self.project.root/'main.tex').read_text(), '$x$')

    def test_ask_scope_and_no_approval_tool_are_parent_enforced(self):
        self.job.mode = 'ask'
        guard = self.controller()
        self.assertNotIn('write_file', guard.names())
        self.assertNotIn('approve_notation', guard.names())
        result = guard.answer(self.call('write_file', {'path':'main.tex','content':'$x+x$'}))
        self.assertTrue(result['result']['isError'])
        self.job.mode = 'edit'; self.job.writable = lambda rel: False
        guard = self.controller()
        result = guard.answer(self.call('edit_file', {'path':'main.tex','old_string':'x','new_string':'x+x'}))
        self.assertTrue(result['result']['isError'])
        self.assertEqual((self.project.root/'main.tex').read_text(), '$x$')

    def test_batch_proposal_records_once_then_halts_without_a_web_wait(self):
        guard = self.controller()
        items = [{'notation':n,'definition':'An estimate.','scope':'This proof.','reason':'A possible shorthand.','source_expression':'x+x'} for n in ('z','w')]
        result = guard.answer(self.call('propose_notations', {'proposals':items}))
        self.assertFalse(result['result']['isError'])
        self.assertEqual([p['canonical'] for p in self.knowledge.status()['proposals']], ['z','w'])
        self.assertTrue(guard.halted)
        self.assertFalse(any(e['t']=='permission' for e in self.job.events))

    def test_split_chat_cannot_publish_even_the_first_fragment(self):
        from backend_claude import ClaudeCode
        backend, guard, state = ClaudeCode('claude'), self.controller(), {}
        for fragment in ('Use $', 'z$ now.'):
            guard.handle({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':fragment}}}, backend, state)
            self.assertFalse(any(e['t'] in ('delta','text') for e in self.job.events))
        guard.handle({'type':'assistant','message':{'content':[{'type':'text','text':'Use $z$ now.'}]}}, backend, state)
        self.assertTrue(guard.halted)
        self.assertFalse(any(e['t'] in ('delta','text') for e in self.job.events))

    def test_known_fragments_are_checked_as_one_message_without_duplicate(self):
        from backend_claude import ClaudeCode
        backend, guard, state = ClaudeCode('claude'), self.controller(), {}
        for fragment in ('Use $x+', 'x$.'):
            guard.handle({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':fragment}}}, backend, state)
        full = {'type':'assistant','message':{'content':[{'type':'text','text':'Use $x+'},{'type':'text','text':'x$.'}]}}
        guard.handle(full, backend, state)
        guard.handle({'type':'result','is_error':False,'result':'Use $x+x$.'}, backend, state)
        self.assertEqual([e['text'] for e in self.job.events if e['t']=='text'], ['Use $x+x$.'])

    def test_tool_input_and_private_thinking_are_not_unchecked_public_math(self):
        from backend_claude import ClaudeCode
        backend, guard, state = ClaudeCode('claude'), self.controller(), {}
        guard.handle({'type':'stream_event','event':{'type':'content_block_start','index':1,'content_block':{'type':'tool_use','id':'w','name':'Write'}}},backend,state)
        guard.handle({'type':'stream_event','event':{'type':'content_block_delta','index':1,'delta':{'type':'input_json_delta','partial_json':json.dumps({'file_path':'main.tex','content':'$z$'})}}},backend,state)
        guard.handle({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'Try $z$.'}}},backend,state)
        guard.handle({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'w','content':'Draft $z$.'}]}},backend,state)
        guard.handle({'type':'result','is_error':False},backend,state)
        self.assertFalse(any(e['t']=='tool_live' for e in self.job.events))
        self.assertNotIn('$z$', json.dumps(self.job.events))

    def test_stopped_stream_discards_unchecked_draft(self):
        from backend_claude import ClaudeCode
        guard = self.controller(); backend = ClaudeCode('claude')
        guard.handle({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'Draft $z$'}}},backend,{})
        self.job.cancel.set()
        guard.handle({'type':'result','is_error':False},backend,{})
        self.assertFalse(self.job.events)
        self.assertEqual(self.knowledge.status()['proposals'], [])

    def test_malformed_mcp_is_a_failed_response_without_mutation(self):
        guard = self.controller()
        for message in (None, [], {'id':1,'method':'tools/call','params':[]}, {'id':1,'method':'tools/call','params':[1]}, {'id':1,'method':'tools/call','params':{'name':[]}}):
            result = guard.answer(message)
            self.assertTrue('error' in result or result['result']['isError'])
        self.assertEqual((self.project.root/'main.tex').read_text(), '$x$')

    def test_result_diagnostics_cannot_publish_unreviewed_subtype_or_denial(self):
        from agent import outcome
        guard = self.controller()
        state = {'exit':1,'is_error':True,'subtype':'Derive with $z$ as a new name.',
                 'denials':['Tool $z$'], 'denied':[{'tool':'Tool $z$','what':'Result $z$'}]}
        guard.sanitize_result(state, '')
        self.assertNotIn('$z$', json.dumps(state))
        self.assertNotIn('$z$', outcome(self.job, state)[1])
        self.assertEqual(self.knowledge.status()['proposals'], [])


class ClaudeResearchNativeProtocol(unittest.TestCase):
    """Actual guarded subprocess wire round trips, with no native Claude binary."""
    setUp = ClaudeResearchProtocol.setUp

    def native(self, body):
        import claude_sandbox
        if not claude_sandbox.availability()['supported']:
            self.skipTest(claude_sandbox.availability()['reason'])
        from backend_claude import ClaudeCode
        profile = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(profile)}))
        self.enterContext(mock.patch('backend_claude.use_config_dir'))
        cli = profile / 'fixture-cli'
        cli.write_text('#!' + sys.executable + '\n' + '''
import json,os,sys
from pathlib import Path
profile=Path(__file__).parent
def emit(d): print(json.dumps(d),flush=True)
def receive(): return json.loads(sys.stdin.readline())
initialize=receive()
emit({'type':'control_response','response':{'subtype':'success','request_id':initialize['request_id'],'response':{}}})
user=receive()
content=user['message']['content']
text=content if isinstance(content,str) else ''.join(b.get('text','') for b in content if b.get('type')=='text')
assert user['type']=='user' and '[Prism research baseline]' in text
emit({'type':'system','subtype':'init','session_id':'wire-session','apiKeySource':'none'})
''' + body + '''
emit({'type':'assistant','message':{'content':[{'type':'text','text':'Known $x$ remains available.'}]}})
emit({'type':'result','session_id':'wire-session','is_error':False})
''')
        cli.chmod(0o755)
        self.job.provider, self.job.prompt = 'claude', 'Continue the existing work.'
        return ClaudeCode('claude', {'bin':str(cli)}), profile

    def test_malformed_control_and_mcp_receive_bounded_error_replies(self):
        backend, profile = self.native('''
frames=[
 {'type':'control_request','request_id':'bad-request','request':[1]},
 {'type':'control_request','request_id':'bad-server','request':{'subtype':'mcp_message','server_name':'other','message':[1]}},
 {'type':'control_request','request_id':'bad-params','request':{'subtype':'mcp_message','server_name':'prism','message':{'jsonrpc':'2.0','id':1,'method':'tools/call','params':[1]}}},
]
responses=[]
for frame in frames:
 emit(frame);responses.append(receive())
(profile/'responses.json').write_text(json.dumps(responses))
''')
        result = backend.run(self.job)
        self.assertEqual(result['exit'], 0, result)
        responses = json.loads((profile/'responses.json').read_text())
        self.assertEqual(responses[0]['response']['subtype'], 'error')
        self.assertIn('error', responses[1]['response']['response']['mcp_response'])
        self.assertIn('error', responses[2]['response']['response']['mcp_response'])
        self.assertIn('Known $x$', ''.join(e.get('text','') for e in self.job.events))

    def test_web_hook_and_session_grant_continue_the_same_guarded_process(self):
        backend, profile = self.native('''
emit({'type':'control_request','request_id':'hook','request':{'subtype':'hook_callback','callback_id':'prism-web-guard','input':{'tool_name':'WebSearch','tool_input':{'query':'existing $z$ notation'}}}})
assert receive()['response']['response']['hookSpecificOutput']['permissionDecision']=='ask'
answers=[]
for i in range(2):
 emit({'type':'control_request','request_id':'web-'+str(i),'request':{'subtype':'can_use_tool','tool_name':'WebSearch','input':{'query':'existing $z$ notation'}}})
 answer=receive(); assert answer['response']['response']['updatedInput']['query']=='existing $z$ notation'; answers.append(answer)
(profile/'answers.json').write_text(json.dumps(answers))
''')
        result = {}
        thread = threading.Thread(target=lambda: result.update(backend.run(self.job)))
        thread.start()
        try:
            deadline = time.monotonic()+5
            while time.monotonic()<deadline and not any(e['t']=='permission' for e in self.job.events):
                time.sleep(.01)
            self.assertNotIn('$z$', json.dumps([e for e in self.job.events if e['t']=='permission']))
            self.assertEqual(backend.permissions.resolve(self.job,'web-0','allow','session'), {'ok':True})
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result['exit'], 0, result)
            self.assertEqual(len([e for e in self.job.events if e['t']=='permission']),1)
            answers = json.loads((profile/'answers.json').read_text())
            self.assertTrue(all(a['response']['response']['behavior']=='allow' for a in answers))
        finally:
            backend.stop(self.job); thread.join(3)


    def test_image_pdf_and_text_attachments_are_wire_data_not_adoption(self):
        import base64
        (self.project.root/'image.png').write_bytes(b'\x89PNG-fixture')
        (self.project.root/'paper.pdf').write_bytes(b'%PDF-fixture')
        (self.project.root/'quote.txt').write_text('Use $z$ for $x+x$.')
        self.job.attachments=['image.png','paper.pdf','quote.txt']
        backend, profile=self.native('''
(profile/'payload.json').write_text(json.dumps(user['message']['content']))
''')
        result=backend.run(self.job)
        self.assertEqual(result['exit'],0,result)
        content=json.loads((profile/'payload.json').read_text())
        self.assertIsInstance(content,list)
        self.assertEqual(content[1]['type'],'image')
        self.assertEqual(content[1]['source']['media_type'],'image/png')
        self.assertEqual(base64.b64decode(content[1]['source']['data']),b'\x89PNG-fixture')
        self.assertEqual(content[2]['type'],'document')
        self.assertEqual(content[2]['source']['media_type'],'application/pdf')
        self.assertEqual(base64.b64decode(content[2]['source']['data']),b'%PDF-fixture')
        self.assertIn('Use $z$',content[3]['text'])
        self.assertNotIn('z',{s['canonical'] for s in self.knowledge.status()['symbols']})

    def test_tool_call_before_current_user_prompt_cannot_write(self):
        backend, profile = self.native('')
        cli = profile/'fixture-cli'
        script=cli.read_text().replace('initialize=receive()\n', '''initialize=receive()
emit({'type':'control_request','request_id':'early','request':{'subtype':'mcp_message','server_name':'prism','message':{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'write_file','arguments':{'path':'main.tex','content':'$x+x$'}}}}})
early=receive()
assert 'error' in early['response']['response']['mcp_response']
''')
        cli.write_text(script)
        result=backend.run(self.job)
        self.assertEqual((self.project.root/'main.tex').read_text(),'$x$')
        self.assertEqual(result['exit'],0,result)

    def test_missing_initialization_ack_fails_with_bounded_timeout(self):
        backend, profile = self.native('')
        cli=profile/'fixture-cli'
        cli.write_text('#!'+sys.executable+'\nimport sys,time\nsys.stdin.readline()\ntime.sleep(8)\n')
        result={}
        with mock.patch('backend_claude.INITIALIZE_TIMEOUT',.1,create=True):
            thread=threading.Thread(target=lambda:result.update(backend.run(self.job)))
            thread.start()
            try:
                thread.join(2)
                self.assertFalse(thread.is_alive(),'missing init acknowledgment must not hold Send indefinitely')
                self.assertTrue(result['is_error'])
                self.assertIn('initialization',result['subtype'].lower())
                self.assertEqual((self.project.root/'main.tex').read_text(),'$x$')
            finally:
                backend.stop(self.job);thread.join(3)


if __name__ == '__main__':
    unittest.main()
