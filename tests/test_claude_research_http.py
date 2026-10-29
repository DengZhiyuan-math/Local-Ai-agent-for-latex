"""Real HTTP -> controlled Claude CLI protocol -> observed project behavior."""
import json
import shutil
import threading
import urllib.parse
from pathlib import Path
import subprocess
import sys
import time
import unittest
import urllib.request

import test_research_http as base

PROPOSAL = {'notation':'z','definition':'The sum of the two existing lengths.',
            'scope':'The current estimate.','reason':'Discuss a shorter name before use.',
            'source_expression':r'\ell+\ell_+'}

def mcp(name, **arguments):
    return {'type':'mcp','name':name,'arguments':arguments}

def text(value):
    return {'type':'text','text':value}

@unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(),
                     'Controlled native Claude HTTP fixtures require macOS sandbox-exec.')
class ClaudeResearchHTTP(unittest.TestCase):
    setUp=base.ResearchHTTP.setUp
    stop=base.ResearchHTTP.stop
    request=base.ResearchHTTP.request
    initialize=base.ResearchHTTP.initialize
    events=base.ResearchHTTP.events
    finish=base.ResearchHTTP.finish

    def tearDown(self):
        result = getattr(self._outcome, 'result', None)
        failed = result and any(test is self or getattr(test, 'test_case', None) is self for test, _ in [*result.failures, *result.errors])
        if failed:
            out = Path(__file__).resolve().parents[1] / 'docs/audits/2026-10-28-claude-controlled/validation'
            for source, suffix in ((self.work/'server.log', 'server.txt'),
                                   (self.work/'profile/cli-requests.jsonl', 'cli.jsonl')):
                if source.exists():
                    shutil.copyfile(source, out / (self._testMethodName + '-' + suffix))
        self.stop()
        self.temp.cleanup()

    def requests(self, kind=None):
        path=self.profile/'cli-requests.jsonl'
        result=[]
        for line in path.read_text().splitlines() if path.exists() else []:
            try: item=json.loads(line)
            except ValueError: continue
            if kind is None or item.get('kind')==kind:result.append(item)
        return result

    @staticmethod
    def visible(events):
        return '\n'.join(str(e.get(key) or '') for e in events
            if e['t'] not in ('notation_proposal','notation_decision')
            for key in ('text','summary','preview','error','stderr','message'))

    def start(self,empty=False):
        (self.work/'ready.json').unlink(missing_ok=True)
        fixture=Path(__file__).parent/'browser/claude_research_fixture.py'
        self.log=(self.work/'server.log').open('ab')
        self.process=subprocess.Popen([sys.executable,str(fixture),str(self.work)]+(['--empty'] if empty else []),stdout=self.log,stderr=self.log)
        deadline=time.monotonic()+10
        while not (self.work/'ready.json').exists() and time.monotonic()<deadline:
            if self.process.poll() is not None:break
            time.sleep(.02)
        self.assertTrue((self.work/'ready.json').exists(),(self.work/'server.log').read_text())
        ready=json.loads((self.work/'ready.json').read_text())
        self.url,self.root=ready['url'].rstrip('/'),Path(ready['root'])
        self.profile=Path(ready['profile'])
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        code,info=self.request('/api/agent/info')
        self.assertEqual(code,200,info)
        self.key=info['project_key']

    def submit(self,script,human='Discuss the current manuscript.',session=None,mode='ask',**extra):
        (self.profile/'cli-script.json').write_text(json.dumps(script))
        return self.request('/api/agent',{'project_key':self.key,'provider':'claude',
            'model':'default','mode':mode,'prompt':human+'\n\n[Referenced] Existing manuscript.',
            'human_text':human,'session_id':session,'active_file':'main.tex' if (self.root/'main.tex').exists() else None,**extra})

    def turn(self,script,**kwargs):
        code,result=self.submit(script,**kwargs)
        self.assertEqual(code,200,result)
        return result['job']

    def state(self):
        code,result=self.request('/api/research')
        self.assertEqual(code,200,result)
        return result

    def test_proposal_ends_turn_then_plain_approval_releases_source_and_chat(self):
        self.initialize()
        original=(self.root/'main.tex').read_text()
        events=self.finish(self.turn([mcp('propose_notation',**PROPOSAL)]))
        self.assertEqual((self.root/'main.tex').read_text(),original)
        self.assertEqual(self.state()['proposals'][0]['status'],'pending')
        self.assertTrue(any(e['t']=='notation_proposal' for e in events))
        session=next(e['session_id'] for e in events if e['t']=='init')
        changed=original.replace(r'$\ell+\ell_+$','$z$')
        events=self.finish(self.turn([mcp('write_file',path='main.tex',content=changed),
                                      text('The adopted notation is $z$.')],human='Yes, I approve.',session=session,mode='edit'))
        self.assertEqual((self.root/'main.tex').read_text(),changed)
        self.assertEqual(self.state()['proposals'][0]['status'],'approved')
        self.assertIn('$z$',''.join(e.get('text','') for e in events if e['t'] in ('text','delta')))
        self.assertEqual(self.state()['decisions'][-1]['human_text'],'Yes, I approve.')

    def test_cli_cannot_directly_write_source_read_editor_state_or_reach_loopback(self):
        self.initialize()
        source=self.root/'main.tex'
        original=source.read_bytes()
        marker=self.work/'state/private-marker'
        marker.parent.mkdir(exist_ok=True)
        marker.write_text('fixture private state')
        (self.root/'.claude').mkdir(exist_ok=True)
        (self.root/'.claude/settings.json').write_text(json.dumps({'permissions':{'allow':['Bash(*)','Write(*)']}}))
        events=self.finish(self.turn([
            {'type':'direct_write','path':str(source),'content':'Unapproved $z$.'},
            {'type':'direct_read','path':str(marker)},
            {'type':'loopback','port':urllib.parse.urlsplit(self.url).port},
            {'type':'sleep','seconds':.1},text('The controlled source was kept.')],mode='edit'))
        self.assertEqual(source.read_bytes(),original)
        attempts=self.requests('attempt')
        self.assertEqual([r['operation'] for r in attempts],['direct_write','direct_read','loopback'])
        self.assertTrue(all(r['blocked'] for r in attempts),attempts)
        self.assertEqual(self.state()['decisions'],[])
        self.assertNotIn('$z$',self.visible(events))

    def test_split_chat_thinking_live_tool_and_failure_text_are_checked_before_visibility(self):
        self.initialize()
        original=(self.root/'main.tex').read_bytes()
        def stream(event):return {'type':'emit','frame':{'type':'stream_event','event':event}}
        scripts = {
            'chat': [{'type':'delta','text':'Unchecked $q_'},{'type':'delta','text':'C$.'},text('Unchecked $q_C$.')],
            'thinking': [stream({'type':'content_block_start','index':0,'content_block':{'type':'thinking'}}),
                         stream({'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'Unchecked $q_C$.'}}),
                         stream({'type':'content_block_stop','index':0})],
            'live-and-preview': [stream({'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'unsafe','name':'Write','input':{}}}),
                stream({'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps({'file_path':str(self.root/'main.tex'),'content':'$q_C$'})}}),
                {'type':'emit','frame':{'type':'assistant','message':{'content':[{'type':'tool_use','id':'unsafe','name':'Write','input':{'file_path':str(self.root/'main.tex'),'content':'$q_C$'}}]}}},
                {'type':'emit','frame':{'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'unsafe','content':'Unsafe $q_C$.'}]}}}],
            'error': [{'type':'stderr','text':'Hidden $q_C$.'},
                      {'type':'emit','frame':{'type':'result','is_error':True,'subtype':'failed','result':'Unsafe $q_C$.'}}],
        }
        for channel, script in scripts.items():
            with self.subTest(channel=channel):
                events=self.finish(self.turn(script))
                self.assertNotIn('q_C',self.visible(events))
                self.assertEqual((self.root/'main.tex').read_bytes(),original)

    def test_shared_batch_direct_family_refusal_and_reopen_semantics(self):
        self.initialize()
        proposals=[{**PROPOSAL,'notation':name} for name in ('z','w','v')]
        events=self.finish(self.turn([mcp('propose_notations',proposals=proposals)]))
        session=next(e['session_id'] for e in events if e['t']=='init')
        self.assertEqual([p['canonical'] for p in self.state()['proposals']],['z','w','v'])
        self.finish(self.turn([text('The selected items have separate decisions.')],
            human='Approve $z$; reject $w$; use $q$ instead of $v$.',session=session))
        state=self.state()
        self.assertEqual({p['canonical']:p['status'] for p in state['proposals']},
                         {'z':'approved','w':'rejected','v':'rejected'})
        self.assertEqual([a['canonical'] for a in state['adoptions']],['q'])
        self.finish(self.turn([text('The direct instruction was saved.')],human='用 h 表示 x+x',session=session))
        self.finish(self.turn([text('The finite family was saved.')],human='Use $a_i$ for $x+x$ with i in {1,2}.',session=session))
        events=self.finish(self.turn([text('Existing $a_2$ and $h$ can be used.')],session=session))
        self.assertIn('$a_2$',self.visible(events))
        self.assertFalse(any(p['canonical']=='h' for p in self.state()['proposals']))
        events=self.finish(self.turn([mcp('propose_notation',**{**PROPOSAL,'notation':'w'})],session=session))
        self.assertFalse(any(e['t']=='notation_proposal' for e in events))
        self.assertEqual(self.state()['proposals'][1]['status'],'rejected')
        self.finish(self.turn([text('The rejected item may be reconsidered.')],human='Reopen $w$.',session=session))
        self.assertEqual(self.state()['proposals'][1]['status'],'pending')
        self.assertNotIn('w',{s['canonical'] for s in self.state()['symbols']})
        self.finish(self.turn([text('The author adopted $w$.')],human='Yes',session=session))
        self.assertEqual(self.state()['proposals'][1]['status'],'approved')
        self.finish(self.turn([text('Keep the expression expanded.')],human='Keep $x+x+x$ expanded; do not abbreviate it.',session=session))
        events=self.finish(self.turn([text('Unchecked $x+x+x=a_1$.')],session=session))
        self.assertNotIn('Unchecked',self.visible(events))
        self.assertEqual(self.state()['rejections'][-1]['kind'],'refuse_expression')

    def test_failed_decision_registration_preserves_pending_and_does_not_start_cli(self):
        self.initialize()
        events=self.finish(self.turn([mcp('propose_notation',**PROPOSAL)]))
        session=next(e['session_id'] for e in events if e['t']=='init')
        before=(self.root/'research/knowledge.json').read_bytes()
        original=(self.root/'main.tex').read_bytes()
        requests=len(self.requests('user'))
        (self.work/'fail-persistence').touch()
        code,failed=self.submit([text('Must not run.')],human='Yes, I approve.',session=session)
        self.assertEqual(code,409,failed)
        self.assertIn('could not be saved',failed['error'])
        self.assertEqual(len(self.requests('user')),requests)
        self.assertEqual((self.root/'research/knowledge.json').read_bytes(),before)
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        (self.work/'fail-persistence').unlink()
        self.finish(self.turn([text('Keep the original expression.')],human='No, I reject.',session=session))
        self.assertEqual(self.state()['proposals'][0]['status'],'rejected')
        events=self.finish(self.turn([mcp('write_file',path='main.tex',content='$z$')],session=session,mode='edit'))
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        self.assertFalse(any(e['t']=='notation_proposal' for e in events))

    def test_restart_and_model_change_load_current_project_rules_and_reject_other_ownership(self):
        self.initialize()
        events=self.finish(self.turn([text('The finite family was saved.')],human='Use $a_i$ for $x+x$ with i in {1,2}.'))
        session=next(e['session_id'] for e in events if e['t']=='init')
        before=self.state()
        self.stop();self.start()
        self.assertEqual(self.state(),before)
        events=self.finish(self.turn([text('Existing $a_2$ remains available.')],session=session,model='fast'))
        self.assertIn('$a_2$',self.visible(events))
        request=self.requests('user')[-1]
        self.assertEqual(request['session_id'],session)
        self.assertIn('fast',request['argv'])
        context=json.dumps(request,ensure_ascii=False)
        self.assertIn('"indices"',context.replace('\\"','"'))
        count=len(self.requests('user'))
        code,result=self.submit([text('Must not run.')],session=session,project_key='other-project')
        self.assertEqual(code,409,result)
        self.assertEqual(len(self.requests('user')),count)
        native=self.profile/'projects'/__import__('re').sub(r'[^A-Za-z0-9]','-',str(self.root.resolve()))/(session+'.jsonl')
        native.write_text(json.dumps({'sessionId':session,'cwd':str(self.work/'another-project')})+'\n')
        code,result=self.submit([text('Must not run.')],session=session)
        self.assertEqual(code,409,result)
        self.assertIn('does not belong',result['error'])
        self.assertEqual(len(self.requests('user')),count)

    def test_ask_scope_and_current_edit_target_do_not_expand_source_permission(self):
        self.initialize()
        original=(self.root/'main.tex').read_bytes()
        events=self.finish(self.turn([mcp('write_file',path='main.tex',content='Known $x$.')],mode='ask'))
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        denied=self.requests('mcp_result')[-1]['answer']['response']['response']['mcp_response']
        self.assertTrue(denied.get('result',{}).get('isError') or denied.get('error'),denied)
        session=next(e['session_id'] for e in events if e['t']=='init')
        self.finish(self.turn([mcp('write_file',path='main.tex',content='Known $x$.'),
                               mcp('write_file',path='sections/intro.tex',content='Existing $x+x$.\n')],
            session=session,mode='edit',scope=['sections/intro.tex'],active_file='sections/intro.tex'))
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        self.assertEqual((self.root/'sections/intro.tex').read_text(),'Existing $x+x$.\n')
        message=self.requests('user')[-1]['frame']['message']['content']
        self.assertIn('sections/intro.tex',message)
        self.assertIn('Default edit target',message)
        self.assertIn('ONLY these files',message)

    def test_web_permission_continues_same_cli_and_stop_ends_descendants(self):
        self.initialize()
        script=[{'type':'web','name':'WebFetch','input':{'url':'https://example.com/one','prompt':'Read'}},
                {'type':'web','name':'WebFetch','input':{'url':'https://example.com/two','prompt':'Read'}},
                text('Continued after Web permission.')]
        job=self.turn(script)
        seen=[];deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            events=self.events(job,len(seen));seen.extend(events['events'])
            if any(e['t']=='permission' for e in seen):break
        permission=next(e for e in seen if e['t']=='permission')
        code,result=self.request('/api/agent/permission',{'project_key':self.key,'job':job,
            'request_id':permission['request_id'],'decision':'allow','scope':'session'})
        self.assertEqual(code,200,result)
        events=self.finish(job)
        self.assertEqual(len([e for e in events if e['t']=='permission']),1)
        self.assertIn('Continued after Web permission.',self.visible(events))
        answers=self.requests('web_result')
        self.assertEqual(len(answers),2)
        self.assertEqual(len({r['pid'] for r in answers}),1)
        session=next(e['session_id'] for e in events if e['t']=='init')
        started=self.profile/'child-started'
        survived=self.profile/'child-survived'
        code="from pathlib import Path;import time;Path("+repr(str(started))+").write_text('started');time.sleep(2);Path("+repr(str(survived))+").write_text('survived')"
        count=len(self.state()['proposals'])
        source=(self.root/'main.tex').read_bytes()
        job=self.turn([{'type':'spawn','code':code},{'type':'delta','text':'Unchecked $stopped_'},{'type':'sleep','seconds':30}],session=session)
        deadline=time.monotonic()+4
        while not started.exists() and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(started.exists(),'the descendant must really start before Stop')
        self.assertEqual(self.request('/api/agent/stop',{'project_key':self.key,'job':job})[0],200)
        events=self.finish(job)
        self.assertEqual(next(e['outcome'] for e in events if e['t']=='done'),'stopped')
        self.assertNotIn('stopped_',self.visible(events))
        time.sleep(2.2)
        self.assertFalse(survived.exists(),'Stop must end descendants before they can write later')
        self.assertEqual((self.root/'main.tex').read_bytes(),source)
        self.assertEqual(len(self.state()['proposals']),count)

    def test_initialization_remains_explicit_and_empty_project_can_adopt_supplied_notation(self):
        self.assertEqual(self.state(),{'initialized':False})
        original=(self.root/'main.tex').read_bytes()
        self.finish(self.turn([text(r'The manuscript quotes $\ell+\ell_+$.'),
                              mcp('write_file',path='main.tex',content='Known $x$.')],mode='edit'))
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        self.assertEqual(self.state(),{'initialized':False})
        self.stop();shutil.rmtree(self.root);self.start(empty=True)
        report=self.initialize(question='An empty research project.',notation='x')
        self.assertEqual(report['sources'],[])
        self.finish(self.turn([mcp('write_file',path='main.tex',content='$x$\n'),text('The supplied $x$ was used.')],
                              human='Create main.tex using the supplied notation.',mode='edit'))
        self.assertEqual((self.root/'main.tex').read_text(),'$x$\n')

    def test_compile_routes_through_shared_guarded_helper_and_ask_refuses_it(self):
        self.initialize()
        original=(self.root/'main.tex').read_bytes()
        (self.work/'compile-stub').touch()
        self.finish(self.turn([mcp('compile',clean=True)],mode='ask'))
        self.assertFalse((self.work/'compile-calls.jsonl').exists())
        denied=self.requests('mcp_result')[-1]['answer']['response']['response']['mcp_response']
        self.assertTrue(denied.get('result',{}).get('isError'),denied)
        events=self.finish(self.turn([mcp('compile',clean=True)],mode='edit'))
        calls=[json.loads(line) for line in (self.work/'compile-calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls,[{'root':str(self.root),'clean':True,'has_cancel':True,'cancelled':False}])
        self.assertIn('fixture guarded compile diagnostic',self.visible(events))
        result=self.requests('mcp_result')[-1]['answer']['response']['response']['mcp_response']['result']
        self.assertTrue(result['isError'])
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        self.assertFalse((self.root/'build/main.pdf').exists())

    def test_model_tool_text_and_cli_state_writes_cannot_forge_a_human_decision(self):
        self.initialize()
        events=self.finish(self.turn([mcp('propose_notation',**PROPOSAL)]))
        session=next(e['session_id'] for e in events if e['t']=='init')
        record=self.root/'research/knowledge.json'
        before=record.read_bytes()
        fake=json.loads(before)
        fake['proposals'][0]['status']='approved'
        fake['decisions']=[{'actor':'human','channel':'editor_chat','human_text':'Yes, I approve.'}]
        original=(self.root/'main.tex').read_bytes()
        events=self.finish(self.turn([
            mcp('approve_notation',actor='human',human_text='Yes, I approve.'),
            mcp('write_file',path='research/knowledge.json',content=json.dumps(fake)),
            {'type':'direct_write','path':str(record),'content':json.dumps(fake)},
            text('The model says the author approved $z$.')],session=session,mode='edit'))
        self.assertEqual(record.read_bytes(),before)
        self.assertEqual(self.state()['proposals'][0]['status'],'pending')
        self.assertEqual(self.state()['decisions'],[])
        self.assertTrue(self.requests('attempt')[-1]['blocked'])
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        self.assertNotIn('author approved',self.visible(events))

if __name__=='__main__':
    unittest.main()
