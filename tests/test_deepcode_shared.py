"""Deep Code reuse of the checked tools and native runtime gate."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'prism_local'))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class DeepCodeShared(unittest.TestCase):
    def test_unverified_runtime_reports_deep_code_unavailable(self):
        import claude_sandbox
        claude_sandbox.availability.cache_clear()
        self.addCleanup(claude_sandbox.availability.cache_clear)
        with mock.patch('claude_sandbox.sys.platform', 'unsupported'):
            result = claude_sandbox.availability('deepcode')
        self.assertFalse(result['supported'])
        self.assertIn('Deep Code', result['reason'])

    def test_checked_diagnostic_names_deep_code_and_does_not_leak_math(self):
        import test_claude_research_protocol as fixtures
        from claude_research import Controller
        case = fixtures.ClaudeResearchProtocol()
        case.setUp()
        try:
            guard = Controller(case.job, provider='deepcode')
            result = {'error': 'Process error at $new_name$.'}
            guard.sanitize_result(result, 'cropped $unknown')
            self.assertIn('Deep Code failed', result['error'])
            self.assertNotIn('$new_name$', result['error'])
            self.assertIn('Deep Code diagnostic', result['stderr'])
            item = {'notation':'z', 'definition':'The existing sum.', 'scope':'This proof.',
                    'reason':'Discuss a possible shorthand.', 'source_expression':'x+x'}
            answer = guard.answer(case.call('propose_notation', item))
            self.assertFalse(answer['result']['isError'])
            self.assertEqual(case.knowledge.status()['proposals'][0]['origin']['channel'], 'deepcode_proposal')
            self.assertEqual((case.project.root/'main.tex').read_text(), '$x$')
        finally:
            case.doCleanups()


@unittest.skipUnless(sys.platform == 'darwin', 'verified native macOS boundary')
class DeepCodeRuntimePaths(unittest.TestCase):
    def test_installed_entry_can_read_its_runtime_but_cannot_write_source_or_evidence(self):
        import json
        import subprocess
        import claude_sandbox
        with tempfile.TemporaryDirectory(prefix='prism-deepcode-runtime-') as directory:
            base = Path(directory).resolve()
            root, profile, scratch, runtime, state = [base/n for n in ('project','profile','scratch','runtime','state')]
            for folder in (root, profile, scratch, runtime, state):folder.mkdir()
            source, private = root/'main.tex', state/'human-witness'
            source.write_text('original'); private.write_text('private')
            entry=runtime/'entry.py'
            entry.write_text('''import json,pathlib,sys
root,profile,private,entry=map(pathlib.Path,sys.argv[1:])
checks={}
for name,operation in [('source',lambda:(root/'main.tex').write_text('bad')),('evidence',lambda:private.read_text()),('runtime',lambda:entry.write_text('bad'))]:
 try:operation()
 except PermissionError:checks[name]=True
 else:checks[name]=False
(profile/'ran').write_text('saved')
print(json.dumps(checks))
''')
            command=[sys.executable,'-I','-B',str(entry),*map(str,(root,profile,private,entry))]
            with mock.patch.dict(os.environ,{'PRISM_STATE_DIR':str(state)}):
                wrapped=claude_sandbox.wrap(command,root,profile,scratch,provider='deepcode',runtime_paths=[runtime])
                result=subprocess.run(wrapped,cwd=root,env=claude_sandbox.environment(dict(os.environ),scratch),capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),{'source':True,'evidence':True,'runtime':True})
            self.assertEqual(source.read_text(),'original')
            self.assertEqual(private.read_text(),'private')
            self.assertEqual((profile/'ran').read_text(),'saved')
