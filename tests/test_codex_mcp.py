"""Legacy Codex uses a real stdio MCP child and the same parent tools."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'prism_local'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_claude_research_protocol as fixture


@unittest.skipIf(os.name == 'nt', 'POSIX FIFO transport; guarded Codex requires macOS')
class CodexMCP(unittest.TestCase):
    def setUp(self):
        self.case = fixture.ClaudeResearchProtocol()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.temporary = self.enterContext(tempfile.TemporaryDirectory(prefix='prism-codex-mcp-'))
        self.scratch = Path(self.temporary).resolve()
        from claude_research import Controller
        self.guard = Controller(self.case.job, provider='codex')

    def relay(self):
        from codex_mcp import Relay
        relay = self.enterContext(Relay(self.guard, self.scratch))
        return relay

    def client(self, relay, command=None, env=None):
        cfg = relay.config
        process = subprocess.Popen(command or [cfg['command'], *cfg['args']],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=3)
        self.addCleanup(cleanup)
        return process

    def call(self, process, method, params=None, mid=1):
        frame = {'jsonrpc':'2.0', 'id':mid, 'method':method, 'params':params or {}}
        process.stdin.write(json.dumps(frame).encode()+b'\n')
        process.stdin.flush()
        self.assertTrue(select.select([process.stdout], [], [], 3)[0], 'MCP child did not answer')
        line = process.stdout.readline()
        self.assertTrue(line, 'MCP child ended without a reply')
        return json.loads(line)

    def test_stdio_child_writes_known_source_only_through_parent_checks(self):
        relay = self.relay()
        relay.active.set()
        client = self.client(relay)
        result = self.call(client, 'tools/call', {'name':'write_file','arguments':{'path':'main.tex','content':'$x+x$'}})
        self.assertFalse(result['result']['isError'])
        self.assertEqual((self.case.project.root/'main.tex').read_text(), '$x+x$')

    def test_closed_parent_does_not_leave_the_stdio_child_waiting(self):
        relay = self.relay()
        relay.close()
        cfg = relay.config
        frame = {'jsonrpc':'2.0','id':1,'method':'tools/list'}
        result = subprocess.run([cfg['command'], *cfg['args']], input=json.dumps(frame)+'\n',
            text=True, capture_output=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertNotIn('tools/list', result.stderr)

    def test_unknown_draft_is_pending_and_later_calls_cannot_write(self):
        relay = self.relay(); relay.active.set()
        child = self.client(relay)
        result = self.call(child, 'tools/call', {'name':'write_file','arguments':{'path':'main.tex','content':'$z$'}})
        self.assertFalse(result['result']['isError'])
        self.assertTrue(self.guard.halted)
        self.assertEqual((self.case.project.root/'main.tex').read_text(), '$x$')
        self.assertEqual(self.case.knowledge.status()['proposals'][0]['status'], 'pending')
        later = self.call(child, 'tools/call', {'name':'write_file','arguments':{'path':'main.tex','content':'$x+x$'}}, mid=2)
        self.assertTrue(later['result']['isError'])
        self.assertFalse(self.case.job.cancel.is_set())
        self.assertEqual((self.case.project.root/'main.tex').read_text(), '$x$')

    def test_initialization_is_available_but_early_ask_and_forged_approval_cannot_write(self):
        self.case.job.mode = 'ask'
        relay = self.relay()
        child = self.client(relay)
        initial = self.call(child, 'initialize', {'protocolVersion':'2025-06-18'})
        self.assertEqual(initial['result']['protocolVersion'], '2025-06-18')
        offered = self.call(child, 'tools/list', mid=2)
        names = {t['name'] for t in offered['result']['tools']}
        self.assertIn('propose_notation', names)
        self.assertNotIn('write_file', names)
        denied = self.call(child, 'tools/call', {'name':'write_file','arguments':{'path':'main.tex','content':'$x+x$'}}, mid=3)
        self.assertTrue(denied['result']['isError'])
        relay.active.set()
        denied = self.call(child, 'tools/call', {'name':'write_file','arguments':{'path':'main.tex','content':'$x+x$'}}, mid=4)
        self.assertTrue(denied['result']['isError'])
        denied = self.call(child, 'tools/call', {'name':'approve_notation','arguments':{'notation':'z','approved':True}}, mid=5)
        self.assertTrue(denied['result']['isError'])
        self.assertEqual((self.case.project.root/'main.tex').read_text(), '$x$')
        self.assertFalse(self.case.knowledge.status()['adoptions'])

    def test_replacing_fifo_paths_cannot_redirect_parent_responses_to_source_or_private_state(self):
        relay = self.relay(); relay.active.set()
        child = self.client(relay)
        self.call(child, 'initialize')
        source = self.case.project.root/'main.tex'
        private = self.scratch/'private-witness'
        private.write_text('private')
        relay.request_pipe.unlink(); relay.request_pipe.symlink_to(source)
        relay.response_pipe.unlink(); relay.response_pipe.symlink_to(private)
        reply = self.call(child, 'ping', mid=2)
        self.assertEqual(reply['result'], {})
        self.assertEqual(source.read_text(), '$x$')
        self.assertEqual(private.read_text(), 'private')
        relay.close()
        self.assertFalse(relay.thread.is_alive())

    def test_duplicate_request_id_cannot_apply_a_second_source_change(self):
        relay = self.relay(); relay.active.set()
        child = self.client(relay)
        self.call(child, 'tools/call', {'name':'write_file','arguments':{'path':'main.tex','content':'$x+x$'}})
        frame = {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'write_file','arguments':{'path':'main.tex','content':'$x+x+x$'}}}
        stdout, stderr = child.communicate(json.dumps(frame).encode()+b'\n', timeout=2)
        self.assertNotEqual(child.returncode, 0)
        self.assertEqual(stdout, b'')
        self.assertNotIn(b'x+x+x', stderr)
        self.assertEqual((self.case.project.root/'main.tex').read_text(), '$x+x$')

    def test_invalid_and_oversized_stdio_frames_fail_without_echoing_payload(self):
        from codex_mcp import MAX_FRAME
        for payload in (b'{"jsonrpc":"2.0","id":1,"method":[]}\n', b'UNTRUSTED'*(MAX_FRAME//9 + 2)):
            with self.subTest(size=len(payload)):
                relay = self.relay(); relay.active.set()
                cfg = relay.config
                child = subprocess.run([cfg['command'], *cfg['args']], input=payload,
                    capture_output=True, timeout=3)
                self.assertNotEqual(child.returncode, 0)
                self.assertEqual(child.stdout, b'')
                self.assertNotIn(b'UNTRUSTED', child.stderr)
                self.assertEqual((self.case.project.root/'main.tex').read_text(), '$x$')
                relay.close()
                self.assertFalse(relay.thread.is_alive())

    def test_stop_unblocks_an_unfinished_parent_operation_and_child(self):
        from unittest import mock
        relay = self.relay(); relay.active.set()
        child = self.client(relay)
        def waiting(_message):
            self.case.job.cancel.wait(3)
            return {'jsonrpc':'2.0','id':1,'result':{}}
        with mock.patch.object(self.guard, 'answer', side_effect=waiting):
            child.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'); child.stdin.flush()
            self.assertTrue(relay.busy.wait(1))
            self.case.job.cancel.set()
            start = time.monotonic(); relay.close()
            self.assertLess(time.monotonic()-start, 1.5)
            self.assertFalse(relay.thread.is_alive())
            self.assertFalse(relay.forced_cancel)
            child.stdin.close(); child.stdin = None
            stdout, _stderr = child.communicate(timeout=2)
            self.assertEqual(stdout, b'')
            self.assertEqual((self.case.project.root/'main.tex').read_text(), '$x$')

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS sandbox')
    def test_sandboxed_mcp_child_uses_fifo_without_original_or_private_write_authority(self):
        import claude_sandbox
        relay = self.relay(); relay.active.set()
        profile = self.scratch/'profile'; profile.mkdir()
        private = Path(self.case.state)/'private-witness'; private.write_text('private')
        source = self.case.project.root/'main.tex'
        cfg = relay.config
        program = r'''
import json,pathlib,runpy,sys
source,private=map(pathlib.Path,sys.argv[1:3]);checks={}
for name,action in [('source_write',lambda:source.write_text('bad')),('private_read',lambda:private.read_text()),('private_write',lambda:private.write_text('bad'))]:
 try:action()
 except PermissionError:checks[name]=True
 else:checks[name]=False
print(json.dumps(checks),flush=True)
module=sys.argv[3];sys.argv=sys.argv[3:];runpy.run_path(module,run_name='__main__')
'''
        command = [sys.executable, '-I', '-B', '-c', program, str(source), str(private), *cfg['args']]
        wrapped = claude_sandbox.wrap(command, self.case.project.root, profile, self.scratch, provider='codex')
        env = claude_sandbox.environment(dict(os.environ), self.scratch)
        child = self.client(relay, command=wrapped, env=env)
        self.assertTrue(select.select([child.stdout], [], [], 3)[0])
        probe = json.loads(child.stdout.readline())
        self.assertEqual(probe, {'source_write':True, 'private_read':True, 'private_write':True})
        reply = self.call(child, 'tools/call', {'name':'write_file','arguments':{'path':'main.tex','content':'$x+x$'}})
        self.assertFalse(reply['result']['isError'])
        self.assertEqual(source.read_text(), '$x+x$')
        self.assertEqual(private.read_text(), 'private')

    def test_transport_close_labels_backend_cancellation_separately_from_human_stop(self):
        from unittest import mock
        relay = self.relay(); relay.active.set()
        child = self.client(relay)
        def waiting(_message):
            self.guard.job.cancel.wait(3)
            return {'jsonrpc':'2.0','id':1,'result':{}}
        with mock.patch.object(self.guard, 'answer', side_effect=waiting):
            child.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'); child.stdin.flush()
            self.assertTrue(relay.busy.wait(1))
            relay.close()
            self.assertFalse(relay.thread.is_alive())
            self.assertTrue(self.case.job.cancel.is_set())
            self.assertTrue(relay.forced_cancel)
            self.assertIsNotNone(relay.error)
            self.assertEqual((self.case.project.root/'main.tex').read_text(), '$x$')

    def test_trusted_transport_registration_does_not_approve_the_proposed_notation(self):
        relay = self.relay(); relay.active.set()
        self.assertEqual(relay.config['default_tools_approval_mode'], 'approve')
        child = self.client(relay)
        item = {'notation':'z','definition':'The existing sum.','scope':'This estimate.',
                'reason':'Discuss a possible name.','source_expression':'x+x'}
        result = self.call(child, 'tools/call', {'name':'propose_notation','arguments':item})
        self.assertFalse(result['result']['isError'])
        self.assertEqual(self.case.knowledge.status()['proposals'][0]['status'], 'pending')
        self.assertFalse(self.case.knowledge.status()['adoptions'])
        self.assertEqual((self.case.project.root/'main.tex').read_text(), '$x$')

    def test_legal_tool_work_can_outlast_the_short_transport_frame_budget(self):
        from unittest import mock
        relay = self.relay(); relay.active.set()
        module = Path(__file__).resolve().parents[1]/'prism_local'
        program = r'''
import sys
sys.path.insert(0,sys.argv[1])
import codex_mcp
codex_mcp.IO_TIMEOUT=.05
codex_mcp.TOOL_TIMEOUT=1.5
raise SystemExit(codex_mcp.serve(sys.argv[2],sys.argv[3]))
'''
        command = [sys.executable,'-c',program,str(module),str(relay.request_pipe),str(relay.response_pipe)]
        child = self.client(relay, command=command)
        original = self.guard.answer
        def slow(message):
            time.sleep(.2)
            return original(message)
        with mock.patch.object(self.guard, 'answer', side_effect=slow):
            result = self.call(child, 'tools/call', {'name':'write_file','arguments':{'path':'main.tex','content':'$x+x$'}})
        self.assertFalse(result['result']['isError'])
        self.assertEqual((self.case.project.root/'main.tex').read_text(), '$x+x$')
        self.assertEqual(relay.config['tool_timeout_sec'], 1200)
