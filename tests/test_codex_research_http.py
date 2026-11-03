"""Real HTTP -> controlled Codex CLI protocol -> observed project behavior."""
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
    """Describe an editor-owned dynamic call using the shared fixture action name."""
    return {'type':'mcp','name':name,'arguments':arguments}

def text(value):
    return {'type':'text','text':value}

@unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(),
                     'Controlled native Codex HTTP fixtures require macOS sandbox-exec.')
class CodexResearchHTTP(unittest.TestCase):
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
            out = Path(__file__).resolve().parents[1] / 'docs/audits/2026-10-28-codex-controlled/validation'
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

    def start(self,empty=False,legacy=False):
        (self.work/'ready.json').unlink(missing_ok=True)
        fixture=Path(__file__).parent/'browser/codex_research_fixture.py'
        self.log=(self.work/'server.log').open('ab')
        self.process=subprocess.Popen([sys.executable,str(fixture),str(self.work)]+(['--empty'] if empty else [])+(['--legacy'] if legacy else []),stdout=self.log,stderr=self.log)
        deadline=time.monotonic()+10
        while not (self.work/'ready.json').exists() and time.monotonic()<deadline:
            if self.process.poll() is not None:break
            time.sleep(.02)
        self.assertTrue((self.work/'ready.json').exists(),(self.work/'server.log').read_text())
        ready=json.loads((self.work/'ready.json').read_text())
        self.url,self.root=ready['url'].rstrip('/'),Path(ready['root'])
        self.profile=Path(ready['profile'])
        self.legacy_session=ready.get('legacy_session')
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        code,info=self.request('/api/agent/info')
        self.assertEqual(code,200,info)
        self.key=info['project_key']

    def submit(self,script,human='Discuss the current manuscript.',session=None,mode='ask',**extra):
        (self.profile/'cli-script.json').write_text(json.dumps(script))
        return self.request('/api/agent',{'project_key':self.key,'provider':'codex',
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


    def test_native_absolute_shell_patch_and_resumed_paths_never_touch_the_original(self):
        self.initialize()
        source=self.root/'main.tex'
        original=source.read_bytes()
        before=source.stat()
        private=self.work/'state/private-marker'
        private.parent.mkdir(exist_ok=True)
        private.write_text('fixture private evidence')
        observing=threading.Event()
        versions=[]
        def observe():
            while not observing.wait(.001):
                versions.append(source.read_bytes())
        watcher=threading.Thread(target=observe)
        watcher.start()
        try:
            events=self.finish(self.turn([
                {'type':'direct_write','path':str(source),'content':'Unchecked $z$.'},
                {'type':'shell_write','path':str(source),'content':'Unchecked $z$.'},
                {'type':'patch_write','path':str(source)},
                {'type':'direct_read','path':str(private)},
                {'type':'loopback','port':urllib.parse.urlsplit(self.url).port},
                {'type':'sleep','seconds':.05},text('The original expression remains.')],mode='edit'))
            session=next(e['session_id'] for e in events if e['t']=='init')
            self.finish(self.turn([
                {'type':'direct_write','path':str(source),'content':'Restored history must not write $z$.'},
                mcp('write_file',path=str(source),content='Unchecked $z$.'),
                text('The resumed original remains unchanged.')],session=session,mode='edit',
                scope=['sections/intro.tex'],active_file='sections/intro.tex'))
        finally:
            observing.set();watcher.join(2)
        self.assertTrue(versions,'the test must observe the source during the CLI attempts')
        self.assertTrue(all(value==original for value in versions))
        self.assertEqual(source.read_bytes(),original)
        after=source.stat()
        self.assertEqual((after.st_ino,after.st_mtime_ns,after.st_size),
                         (before.st_ino,before.st_mtime_ns,before.st_size))
        attempts=self.requests('attempt')
        self.assertEqual([r['operation'] for r in attempts],
                         ['direct_write','shell_write','patch_write','direct_read','loopback','direct_write'])
        self.assertTrue(all(r['blocked'] for r in attempts),attempts)
        for attempt in attempts:
            if attempt['operation'] in ('shell_write','patch_write'):
                self.assertRegex(attempt['detail'],r'(?i)operation not permitted|permission denied')
        self.assertEqual(self.state()['decisions'],[])
        self.assertTrue(all(Path(row['cwd']).resolve()==self.root.resolve() for row in self.requests('user')))

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
        self.assertEqual(request['frame']['params']['model'],'fast')
        context=json.dumps(request,ensure_ascii=False)
        self.assertIn('"indices"',context.replace('\\"','"'))
        count=len(self.requests('user'))
        code,result=self.submit([text('Must not run.')],session=session,project_key='other-project')
        self.assertEqual(code,409,result)
        self.assertEqual(len(self.requests('user')),count)
        native=self.profile/'sessions'/('rollout-fixture-'+session+'.jsonl')
        native.write_text(json.dumps({'type':'session_meta','payload':{'id':session,'cwd':str(self.work/'another-project')}})+'\n')
        code,result=self.submit([text('Must not run.')],session=session)
        self.assertEqual(code,409,result)
        self.assertIn('does not belong',result['error'])
        self.assertEqual(len(self.requests('user')),count)

    def test_ask_scope_and_current_edit_target_do_not_expand_source_permission(self):
        self.initialize()
        original=(self.root/'main.tex').read_bytes()
        events=self.finish(self.turn([mcp('write_file',path='main.tex',content='Known $x$.')],mode='ask'))
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        denied=self.requests('mcp_result')[-1]['answer']['result']
        self.assertFalse(denied['success'],denied)
        session=next(e['session_id'] for e in events if e['t']=='init')
        self.finish(self.turn([mcp('write_file',path='main.tex',content='Known $x$.'),
                               mcp('write_file',path='sections/intro.tex',content='Existing $x+x$.\n')],
            session=session,mode='edit',scope=['sections/intro.tex'],active_file='sections/intro.tex'))
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        self.assertEqual((self.root/'sections/intro.tex').read_text(),'Existing $x+x$.\n')
        message=self.requests('user')[-1]['frame']['params']['input'][0]['text']
        self.assertIn('sections/intro.tex',message)
        self.assertIn('Default edit target',message)
        self.assertIn('ONLY these files',message)

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

    def test_split_chat_reasoning_tool_preview_and_diagnostics_are_checked_before_display(self):
        self.initialize()
        original=(self.root/'main.tex').read_bytes()
        def emit(method, **params):return {'type':'emit','method':method,'params':params}
        scripts={
            'chat':[{'type':'delta','text':'Unchecked $q_'},{'type':'delta','text':'D$.'},text('Unchecked $q_D$.')],
            'reasoning':[emit('item/completed',item={'id':'reason','type':'reasoning','summary':['Hidden reasoning $q_D$.'],'content':[]})],
            'tool':[emit('item/completed',item={'id':'command','type':'commandExecution','command':'cat main.tex',
                'cwd':str(self.root),'status':'completed','commandActions':[],'aggregatedOutput':'Unsafe preview $q_D$.','exitCode':0,'durationMs':1})],
            'warning':[emit('warning',message='Unsafe warning $q_D$.')],
            'error':[{'type':'stderr','text':'Unsafe stderr $q_D$.'},emit('error',error={'message':'Unsafe error $q_D$.','additionalDetails':None},willRetry=False)]}
        for channel,script in scripts.items():
            with self.subTest(channel=channel):
                events=self.finish(self.turn(script))
                self.assertNotIn('q_D',self.visible(events))
                self.assertEqual((self.root/'main.tex').read_bytes(),original)
        events=self.finish(self.turn([text('The existing $x$ remains in the manuscript.')]))
        self.assertIn('The existing $x$ remains',self.visible(events))

    def test_stop_ends_cli_descendants_without_publishing_or_adopting_partial_draft(self):
        self.initialize()
        started,survived=self.profile/'child-started',self.profile/'child-survived'
        code='from pathlib import Path;import time;Path('+repr(str(started))+').write_text("started");time.sleep(2);Path('+repr(str(survived))+').write_text("survived")'
        count=len(self.state()['proposals'])
        original=(self.root/'main.tex').read_bytes()
        job=self.turn([{'type':'spawn','code':code},{'type':'delta','text':'Unchecked $stopped_'},{'type':'sleep','seconds':30}])
        deadline=time.monotonic()+4
        while not started.exists() and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(started.exists(),'the descendant must start before Stop is tested')
        self.assertEqual(self.request('/api/agent/stop',{'project_key':self.key,'job':job})[0],200)
        events=self.finish(job)
        self.assertEqual(next(e['outcome'] for e in events if e['t']=='done'),'stopped')
        self.assertNotIn('stopped_',self.visible(events))
        time.sleep(2.2)
        self.assertFalse(survived.exists(),'Stop must terminate descendants before a later write')
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        self.assertEqual(len(self.state()['proposals']),count)

    def test_compile_routes_through_shared_guarded_helper_and_ask_refuses_it(self):
        self.initialize()
        original=(self.root/'main.tex').read_bytes()
        (self.work/'compile-stub').touch()
        self.finish(self.turn([mcp('compile',clean=True)],mode='ask'))
        self.assertFalse((self.work/'compile-calls.jsonl').exists())
        denied=self.requests('mcp_result')[-1]['answer']['result']
        self.assertFalse(denied['success'],denied)
        events=self.finish(self.turn([mcp('compile',clean=True)],mode='edit'))
        calls=[json.loads(line) for line in (self.work/'compile-calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls,[{'root':str(self.root),'clean':True,'has_cancel':True,'cancelled':False}])
        self.assertIn('fixture guarded compile diagnostic',self.visible(events))
        result=self.requests('mcp_result')[-1]['answer']['result']
        self.assertFalse(result['success'])
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        self.assertFalse((self.root/'build/main.pdf').exists())

    def test_model_catalog_uses_native_metadata_protocol_without_starting_a_turn(self):
        code,catalog=self.request('/api/agent/models?provider=codex')
        self.assertEqual(code,200,catalog)
        self.assertEqual({m['id'] for m in catalog['models']},{'default','fast'})
        self.assertEqual(next(m['efforts'] for m in catalog['models'] if m['id']=='default'),['high'])
        self.assertTrue(any(row['frame'].get('method')=='model/list' for row in self.requests('input')))
        self.assertEqual(self.requests('user'),[])
        self.assertEqual(self.requests('thread'),[])

    def test_legacy_native_session_uses_actual_stdio_relay_and_keeps_scope_and_historical_paths(self):
        self.stop();self.start(legacy=True)
        self.initialize()
        session=self.legacy_session
        self.assertTrue(session)
        native=self.profile/'sessions'/('rollout-fixture-'+session+'.jsonl')
        before_meta=native.read_bytes()
        self.assertNotIn('dynamic_tools',json.loads(before_meta.splitlines()[0])['payload'])
        source=self.root/'main.tex'
        original=source.read_bytes()
        before=source.stat()
        stop=threading.Event();versions=[]
        def observe():
            while not stop.wait(.001):versions.append(source.read_bytes())
        watcher=threading.Thread(target=observe);watcher.start()
        try:
            events=self.finish(self.turn([
                {'type':'direct_write','path':str(source),'content':'Unapproved $z$.'},
                mcp('write_file',path='main.tex',content='Known $x$.'),
                mcp('propose_notation',**PROPOSAL)],session=session,mode='ask'))
        finally:stop.set();watcher.join(2)
        self.assertTrue(versions)
        self.assertTrue(all(value==original for value in versions))
        self.assertEqual((source.stat().st_ino,source.stat().st_mtime_ns),(before.st_ino,before.st_mtime_ns))
        self.assertEqual(next(e['session_id'] for e in events if e['t']=='init'),session)
        self.assertTrue(any(e['t']=='notation_proposal' for e in events),events)
        self.assertEqual(self.state()['proposals'][0]['status'],'pending')
        self.assertEqual(self.state()['decisions'],[])
        self.finish(self.turn([
            {'type':'direct_write','path':str(source),'content':'History must not write before the check.'},
            mcp('write_file',path='main.tex',content='Known $z$.'),
            mcp('write_file',path='sections/intro.tex',content='Adopted $z$.\n')],
            human='Yes, I approve.',session=session,mode='edit',scope=['sections/intro.tex'],active_file='sections/intro.tex'))
        self.assertEqual(source.read_bytes(),original)
        self.assertEqual((self.root/'sections/intro.tex').read_text(),'Adopted $z$.\n')
        self.assertEqual(self.state()['proposals'][0]['status'],'approved')
        changed=original.decode().replace(r'$\ell+\ell_+$','$z$')
        events=self.finish(self.turn([mcp('write_file',path='main.tex',content=changed),text('The author adopted $z$.')],
            human='Use the approved notation in main.tex.',session=session,mode='edit',scope=['main.tex']))
        self.assertEqual(source.read_text(),changed)
        self.assertIn('author adopted $z$',self.visible(events))
        self.assertTrue(all(row['transport']=='stdio_mcp' for row in self.requests('mcp_result')))
        self.assertTrue(self.requests('relay_start'))
        self.assertTrue(self.requests('relay_tools'))
        self.assertTrue(all(row['method']=='thread/resume' and row['session_id']==session for row in self.requests('thread')))
        self.assertTrue(all(row['blocked'] for row in self.requests('attempt')))
        self.assertTrue(native.read_bytes().startswith(before_meta),'restoration must not rewrite the native session metadata')

if __name__=='__main__':
    unittest.main()
