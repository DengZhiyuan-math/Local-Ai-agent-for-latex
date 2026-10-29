"""Codex's shared native boundary, with no model or CLI invocation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'prism_local'))
import claude_sandbox


@unittest.skipUnless(sys.platform == 'darwin', 'verified native macOS boundary')
class CodexSandbox(unittest.TestCase):
    def test_codex_profile_works_without_reading_claude_global_config(self):
        with tempfile.TemporaryDirectory(prefix='prism-codex-boundary-') as directory:
            base = Path(directory).resolve()
            root, profile, scratch, home, state = [base / n for n in ('project', 'profile', 'scratch', 'home', 'state')]
            for folder in (root, profile, scratch, home, state):
                folder.mkdir()
            source = root / 'main.tex'
            source.write_text('original')
            private = state / 'decision'
            private.write_text('private')
            global_config = home / '.claude.json'
            global_config.write_text('unrelated credentials')
            program = r'''
import json, pathlib, sys
root, profile, scratch, private, unrelated = map(pathlib.Path,sys.argv[1:])
checks={}
for name, action in [('source',lambda:(root/'main.tex').write_text('bad')),('private',lambda:private.read_text()),('unrelated_claude',lambda:unrelated.read_text())]:
 try:action()
 except PermissionError:checks[name]=True
 else:checks[name]=False
(profile/'session').write_text('saved')
print(json.dumps(checks))
'''
            command = [sys.executable, '-I', '-B', '-c', program, *map(str, (root, profile, scratch, private, global_config))]
            with mock.patch.dict(os.environ, {'HOME': str(home), 'PRISM_STATE_DIR': str(state)}):
                wrapped = claude_sandbox.wrap(command, root, profile, scratch, provider='codex')
                result = subprocess.run(wrapped, cwd=root, env=claude_sandbox.environment(dict(os.environ), scratch), text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'source': True, 'private': True, 'unrelated_claude': True})
            self.assertEqual(source.read_text(), 'original')
            self.assertEqual((profile/'session').read_text(), 'saved')


class SharedCodexTools(unittest.TestCase):
    def test_codex_profile_rejection_uses_shared_cli_terminology(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(ValueError, 'controlled CLI profile'):
                claude_sandbox.wrap([sys.executable], root, root/'profile', root/'scratch', provider='codex')

    def test_resumable_contract_is_stable_but_ask_has_no_write_authority(self):
        import test_claude_research_protocol as fixture
        from claude_research import Controller
        case = fixture.ClaudeResearchProtocol()
        case.setUp()
        try:
            case.job.mode = 'ask'
            guard = Controller(case.job, provider='codex')
            offered = {d['name'] for d in guard.definitions(all_tools=True)}
            self.assertIn('write_file', offered)
            self.assertIn('compile', offered)
            self.assertNotIn('write_file', guard.names())
            denied = guard.answer(case.call('write_file', {'path':'main.tex','content':'$x+x$'}))
            self.assertTrue(denied['result']['isError'])
            self.assertEqual((case.project.root/'main.tex').read_text(), '$x$')
            proposal = {'notation':'z','definition':'The existing sum.','scope':'This proof.','reason':'Review a possible shorthand.','source_expression':'x+x'}
            accepted = guard.answer(case.call('propose_notation', proposal))
            self.assertFalse(accepted['result']['isError'])
            item = case.knowledge.status()['proposals'][0]
            self.assertEqual(item['origin']['channel'], 'codex_proposal')
        finally:
            case.doCleanups()
