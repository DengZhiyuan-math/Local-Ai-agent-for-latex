"""Real HTTP -> controlled DeepCode CLI protocol -> observed project behavior."""
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
                     'Controlled native Deep Code HTTP fixtures require macOS sandbox-exec.')
class DeepCodeResearchHTTP(unittest.TestCase):
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
            out = Path(__file__).resolve().parents[1] / 'docs/audits/2026-10-29-deepcode-controlled/validation'
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
        fixture=Path(__file__).parent/'browser/deepcode_research_fixture.py'
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
        return self.request('/api/agent',{'project_key':self.key,'provider':'deepcode',
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
        self.assertEqual(request['model'],'fast')
        self.assertIn('"indices"',request['message'])
        count=len(self.requests('user'))
        code,result=self.submit([text('Must not run.')],session=session,project_key='other-project')
        self.assertEqual(code,409,result)
        self.assertEqual(len(self.requests('user')),count)
        native=Path(request['session_path'])
        header=json.loads(native.read_text().splitlines()[0])
        header['content']='# Local Workspace Environment\n\n```json\n'+json.dumps({'root path':str(self.work/'another-project'),'pwd':str(self.work/'another-project')})+'\n```'
        native.write_text(json.dumps(header)+'\n')
        code,result=self.submit([text('Must not run.')],session=session)
        self.assertEqual(code,409,result)
        self.assertIn('does not belong',result['error'])
        self.assertEqual(len(self.requests('user')),count)

    def test_ask_scope_and_current_edit_target_do_not_expand_source_permission(self):
        self.initialize()
        original=(self.root/'main.tex').read_bytes()
        events=self.finish(self.turn([mcp('write_file',path='main.tex',content='Known $x$.'),text('The attempted edit was checked.')],mode='ask'))
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        denied=self.requests('mcp_result')[-1]['answer']['result']
        self.assertTrue(denied['isError'],denied)
        session=next(e['session_id'] for e in events if e['t']=='init')
        self.finish(self.turn([mcp('write_file',path='main.tex',content='Known $x$.'),
                               mcp('write_file',path='sections/intro.tex',content='Existing $x+x$.\n'),
                               text('The scoped edit was checked.')],
            session=session,mode='edit',scope=['sections/intro.tex'],active_file='sections/intro.tex'))
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        self.assertEqual((self.root/'sections/intro.tex').read_text(),'Existing $x+x$.\n')
        message=self.requests('user')[-1]['message']
        self.assertIn('sections/intro.tex',message)
        self.assertIn('Default edit target',message)
        self.assertIn('ONLY these files',message)

    def test_concurrent_human_settings_and_out_of_scope_edits_survive_finish_and_undo(self):
        self.initialize()
        settings=self.root/'.deepcode/settings.json'
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({'note':'Original human settings.'})+'\n')
        intro=self.root/'sections/intro.tex'
        main=self.root/'main.tex'
        original_main=main.read_bytes()
        started=self.profile/'human-edit-window'
        job=self.turn([{'type':'mark','path':str(started)},{'type':'sleep','seconds':.5},
                       mcp('write_file',path='main.tex',content='Known $x$.\n'),
                       text('The requested edit was checked.')],mode='edit',scope=['main.tex'])
        deadline=time.monotonic()+4
        while not started.exists() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(started.exists(),'the CLI must be active while the human edit occurs')
        human_settings=b'{"note":"The human changed settings during this Turn."}\n'
        human_intro=b'Human text saved while the agent works.\n'
        settings.write_bytes(human_settings)
        intro.write_bytes(human_intro)
        events=self.finish(job)
        self.assertEqual(settings.read_bytes(),human_settings)
        self.assertEqual(intro.read_bytes(),human_intro)
        self.assertEqual(main.read_text(),'Known $x$.\n')
        done=next(e for e in events if e['t']=='done')
        self.assertEqual(done['outcome'],'ok',done)
        self.assertEqual(done['reverted'],[])
        self.assertEqual([row['path'] for row in done['changed']],['main.tex'])
        code,result=self.request('/api/agent/undo',{'project_key':self.key,'turn':done['turn']})
        self.assertEqual(code,200,result)
        self.assertEqual(main.read_bytes(),original_main)
        self.assertEqual(settings.read_bytes(),human_settings)
        self.assertEqual(intro.read_bytes(),human_intro)

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

    def test_stdout_fragments_final_reasoning_tool_logs_and_stderr_are_checked_before_display(self):
        self.initialize()
        original=(self.root/'main.tex').read_bytes()
        def native_tool(value,cid):
            return {'type':'session','message':{'role':'tool','content':value,
                'messageParams':{'tool_call_id':cid},
                'meta':{'function':{'name':'read_file','arguments':json.dumps({'path':'main.tex'})}}}}
        positive=self.finish(self.turn([native_tool('Known preview $x$.','known-preview'),
            {'type':'sleep','seconds':.6},text('The existing $x$ remains available.')]))
        self.assertTrue(any(e['t']=='tool_result' and 'Known preview $x$' in str(e.get('preview','')) for e in positive),positive)
        scripts={
            'fragments':[{'type':'delta','text':'Unchecked $q_'},{'type':'delta','text':'E$.'}],
            'final':[text('Unchecked final $q_E$.')],
            'reasoning':[{'type':'final','message':{'messageParams':{'reasoning_content':'Unsafe reasoning $q_E$.'}}},text('The manuscript remains unchanged.')],
            'tool':[native_tool('Unsafe tool preview $q_E$.','unsafe-preview'),{'type':'sleep','seconds':.6},text('The manuscript remains unchanged.')],
            'stderr':[{'type':'stderr','text':'Unsafe stderr $q_E$.'},text('The manuscript remains unchanged.')],
        }
        for channel,script in scripts.items():
            with self.subTest(channel=channel):
                events=self.finish(self.turn(script))
                self.assertNotIn('q_E',self.visible(events))
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
        self.assertTrue(denied['isError'],denied)
        events=self.finish(self.turn([mcp('compile',clean=True)],mode='edit'))
        calls=[json.loads(line) for line in (self.work/'compile-calls.jsonl').read_text().splitlines()]
        self.assertEqual(calls,[{'root':str(self.root),'clean':True,'has_cancel':True,'cancelled':False}])
        self.assertIn('fixture guarded compile diagnostic',self.visible(events))
        result=self.requests('mcp_result')[-1]['answer']['result']
        self.assertTrue(result['isError'])
        self.assertEqual((self.root/'main.tex').read_bytes(),original)
        self.assertFalse((self.root/'build/main.pdf').exists())

    def test_restored_startup_does_not_replay_previous_turn_tool_records(self):
        self.initialize()
        old_tool={'type':'session','message':{'role':'tool','content':'Previous tool preview $x$.',
            'messageParams':{'tool_call_id':'previous-tool'},
            'meta':{'function':{'name':'read_file','arguments':json.dumps({'path':'main.tex'})}}}}
        first=self.finish(self.turn([old_tool,{'type':'sleep','seconds':.6},text('The first Turn is complete.')]))
        self.assertTrue(any(e['t']=='tool_result' and 'Previous tool preview' in str(e.get('preview','')) for e in first),first)
        session=next(e['session_id'] for e in first if e['t']=='init')
        (self.profile/'startup-delay').write_text('.7')
        events=self.finish(self.turn([text('The current Turn is complete.')],session=session))
        self.assertNotIn('Previous tool preview',self.visible(events))
        self.assertIn('The current Turn is complete.',self.visible(events))

    def test_inherited_services_and_high_priority_scripts_are_neutralized_without_moving_home(self):
        import os
        import shlex
        import uuid
        self.initialize()
        marker_script=self.profile/'harmless-marker.py'
        marker_script.write_text('''from pathlib import Path
import json,sys
Path(sys.argv[1]).write_text('started')
if len(sys.argv)>2:
 for line in sys.stdin:
  request=json.loads(line)
  if 'id' not in request:continue
  result={'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'fixture-marker','version':'1'}} if request.get('method')=='initialize' else {'tools':[]}
  print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
''')
        mcp_marker,notify_marker,web_marker=[self.profile/name for name in ('inherited-mcp-started','notify-started','web-started')]
        notify=shlex.join([sys.executable,str(marker_script),str(notify_marker)])
        web=shlex.join([sys.executable,str(marker_script),str(web_marker)])
        user_settings=self.profile/'settings.json'
        user_settings.write_text(json.dumps({'mcpServers':{'inherited':{'command':sys.executable,'args':[str(marker_script),str(mcp_marker),'mcp']}},'notify':notify,'webSearchTool':web}))
        original=user_settings.read_bytes()
        (self.work/'runtime-env.json').write_text(json.dumps({'DEEPCODE_NOTIFY':notify,'DEEPCODE_WEB_SEARCH_TOOL':web}))
        self.stop();self.start()
        (self.profile/'cli-script.json').write_text(json.dumps([{'type':'websearch'},text('The existing $x$ remains available.')]))
        explicit=self.profile/'positive-control-settings.json';explicit.write_text('{}')
        control_env=dict(os.environ,HOME=str(self.profile.parent),DEEPCODE_NOTIFY=notify,DEEPCODE_WEB_SEARCH_TOOL=web)
        control=subprocess.run([sys.executable,str(self.profile/'deepcode-fixture.py'),'--exec','--json','--settings',str(explicit),'--session-id',str(uuid.uuid4())],
            cwd=self.root,env=control_env,input='Fixture local positive control.',capture_output=True,text=True,timeout=5)
        self.assertEqual(control.returncode,0,control.stderr)
        self.assertTrue(all(path.exists() for path in (mcp_marker,notify_marker,web_marker)), 'the fixture must actually execute these configured scripts in its positive control')
        for path in (mcp_marker,notify_marker,web_marker):path.unlink()
        events=self.finish(self.turn([{'type':'websearch'},text('The existing $x$ remains available.')]))
        self.assertEqual(next(e['outcome'] for e in events if e['t']=='done'),'ok',events)
        self.assertTrue(all(not path.exists() for path in (mcp_marker,notify_marker,web_marker)))
        self.assertEqual(user_settings.read_bytes(),original)
        self.assertEqual(self.requests('user')[-1]['home'],str(self.profile.parent))
        self.assertNotEqual(self.requests('relay_start')[-1]['server'],'inherited')
        scripts=self.requests('script')[-2:]
        self.assertEqual([(row['operation'],row['command']) for row in scripts],[('websearch','/usr/bin/false'),('notify','/usr/bin/true')])

    def test_stale_whole_file_write_keeps_same_target_human_changes_until_a_fresh_read(self):
        self.initialize()
        source=self.root/'main.tex'
        original=source.read_text()
        read_done,release=self.profile/'read-complete',self.profile/'release-stale-write'
        stale=original.replace('finite covering property','checked covering property')
        job=self.turn([mcp('read_file',path='main.tex'),{'type':'mark','path':str(read_done)},
            {'type':'wait','path':str(release)},mcp('write_file',path='main.tex',content=stale),
            text('The requested whole-file update was checked.')],mode='edit',scope=['main.tex'])
        deadline=time.monotonic()+4
        while not read_done.exists() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(read_done.exists(),'the model must have received the earlier file before the human saves')
        human=original+'\nA human paragraph saved after the agent read this file.\n'
        source.write_text(human)
        release.write_text('continue')
        events=self.finish(job)
        self.assertEqual(source.read_text(),human,'the stale model snapshot must not erase the human paragraph')
        denied=self.requests('mcp_result')[-1]['answer']['result']
        self.assertTrue(denied['isError'],denied)
        session=next(e['session_id'] for e in events if e['t']=='init')
        done=next(e for e in events if e['t']=='done')
        code,result=self.request('/api/agent/undo',{'project_key':self.key,'turn':done['turn']})
        self.assertIn(code,(200,409),result)
        self.assertEqual(source.read_text(),human,"Undo must not treat the rejected write human version as an Agent edit")
        self.assertEqual(done['changed'],[])
        fresh=human.replace('finite covering property','checked covering property')
        events=self.finish(self.turn([mcp('read_file',path='main.tex'),mcp('write_file',path='main.tex',content=fresh),
            text('The refreshed draft keeps the human paragraph.')],session=session,mode='edit',scope=['main.tex']))
        self.assertEqual(source.read_text(),fresh)
        self.assertFalse(self.requests('mcp_result')[-1]['answer']['result'].get('isError'))
        self.assertEqual(next(e['outcome'] for e in events if e['t']=='done'),'ok',events)

    def test_precise_edit_keeps_human_content_in_both_result_and_undo(self):
        self.initialize()
        source=self.root/'main.tex'
        original=source.read_text()
        read_done,release=self.profile/'precise-read-complete',self.profile/'release-precise-edit'
        job=self.turn([mcp('read_file',path='main.tex'),{'type':'mark','path':str(read_done)},
            {'type':'wait','path':str(release)},
            mcp('edit_file',path='main.tex',old_string='finite covering property',new_string='checked covering property'),
            text('The precise edit keeps the current manuscript.')],mode='edit',scope=['main.tex'])
        deadline=time.monotonic()+4
        while not read_done.exists() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(read_done.exists())
        human=original+'\nA human paragraph saved before the precise edit.\n'
        source.write_text(human)
        release.write_text('continue')
        events=self.finish(job)
        self.assertEqual(source.read_text(),human.replace('finite covering property','checked covering property'))
        self.assertFalse(self.requests('mcp_result')[-1]['answer']['result'].get('isError'))
        done=next(e for e in events if e['t']=='done')
        self.assertEqual(done['outcome'],'ok',done)
        code,result=self.request('/api/agent/undo',{'project_key':self.key,'turn':done['turn']})
        self.assertEqual(code,200,result)
        self.assertEqual(source.read_text(),human,'Undo must remove only the precise Agent edit and retain the human paragraph')
