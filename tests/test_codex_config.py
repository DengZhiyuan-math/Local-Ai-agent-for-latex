"""Configuration isolation before a Codex thread can start."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'prism_local'))


class CodexConfig(unittest.TestCase):
    def snapshot(self, root, servers=None):
        return {
            'config': {'features': dict.fromkeys(('shell_tool','hooks','apps','plugins','remote_plugin','multi_agent','skill_mcp_dependency_install'), False),
                'agents': {'enabled':False}, 'approval_policy':'never', 'web_search':'disabled', 'sandbox_mode':'read-only',
                'projects':{str(root):{'trust_level':'untrusted'}}, 'mcp_servers':copy.deepcopy(servers or {})},
            'layers':[{'name':{'type':'user','file':str(root.parent/'profile/config.toml')},'config':{'mcp_servers':servers or {}},'version':'1'},
                {'name':{'type':'project','dotCodexFolder':str(root/'.codex')},'config':{},'version':'1','disabledReason':'Project is untrusted.'}],
        }

    def test_dotted_server_names_keep_transport_and_disable_before_thread(self):
        from codex_config import prepare, verify
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()/'project'
            first=self.snapshot(root, {'vendor.service':{'command':'fixture-mcp','args':['never-log-this-token'],'enabled':True}})
            plan=prepare(first, root)
            parsed=tomllib.loads('\n'.join(plan['overrides']))
            self.assertEqual(parsed['mcp_servers'],{'vendor.service':{'command':'/usr/bin/false','enabled':False}})
            self.assertEqual(parsed['projects'],{str(root):{'trust_level':'untrusted'}})
            self.assertNotIn('never-log-this-token', str(plan))
            final=copy.deepcopy(first);final['config']['mcp_servers']['vendor.service']['enabled']=False
            verify(final, root, plan)

    def test_active_mcp_or_changed_source_blocks_thread_start(self):
        from codex_config import prepare, verify
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()/'project'
            first=self.snapshot(root, {'remote':{'url':'https://fixture.invalid/mcp?secret=never-print','enabled':True}})
            plan=prepare(first, root)
            parsed=tomllib.loads('\n'.join(plan['overrides']))
            self.assertEqual(parsed['mcp_servers'],{'remote':{'url':'http://127.0.0.1:1/','enabled':False}})
            self.assertNotIn('never-print',str(plan))
            with self.assertRaisesRegex(ValueError,'not disabled'):
                verify(first,root,plan)
            changed=copy.deepcopy(first);changed['config']['mcp_servers']['remote']['enabled']=False
            changed['layers'][0]['config']['mcp_servers']['new-server']={'command':'unexpected'}
            with self.assertRaisesRegex(ValueError,'changed'):
                verify(changed,root,plan)

    def test_unknown_and_malformed_layers_fail_closed(self):
        from codex_config import prepare, verify
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()/'project';first=self.snapshot(root);plan=prepare(first,root)
            malformed=[{}, {'config':{},'layers':'bad'}, {'config':{},'layers':[{'name':{'type':'unknown'},'config':{}}]},
                {**first,'config':{**first['config'],'mcp_servers':{'bad':'invalid'}}}]
            for value in malformed:
                with self.subTest(value=value),self.assertRaises(ValueError):
                    prepare(value,root)
                with self.subTest(value=value),self.assertRaises(ValueError):
                    verify(value,root,plan)
            valid=self.snapshot(root,{'same-name':{'command':'fixture','enabled':True}})
            plan=prepare(valid,root);invalid=copy.deepcopy(valid)
            invalid['config']['mcp_servers']['same-name']='malformed'
            with self.assertRaises(ValueError):
                verify(invalid,root,plan)

    def test_only_exact_parent_relay_can_remain_enabled(self):
        from codex_config import prepare, verify
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()/'project';first=self.snapshot(root)
            relay={'prism_parent':{'command':'/trusted/python','args':['/trusted/relay.py','--request-pipe','/scratch/request','--response-pipe','/scratch/response'], 'enabled':True,'required':True,'default_tools_approval_mode':'approve','tool_timeout_sec':1200}}
            plan=prepare(first,root,trusted_mcp=relay)
            final=copy.deepcopy(first);final['config']['mcp_servers']=copy.deepcopy(relay)
            verify(final,root,plan)
            self.assertEqual(plan['config']['mcp_servers']['prism_parent'],relay['prism_parent'])
            for field,value in (('args',['/untrusted.py']),('env',{'TOKEN':'untrusted'}),('environment_id','remote')):
                tampered=copy.deepcopy(final);tampered['config']['mcp_servers']['prism_parent'][field]=value
                with self.subTest(field=field),self.assertRaises(ValueError):
                    verify(tampered,root,plan)

    def test_parent_relay_approval_mode_is_explicit_and_verified(self):
        from codex_config import prepare, verify
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()/'project';first=self.snapshot(root)
            relay={'prism_parent':{'command':'/trusted/python','args':['/trusted/relay.py'],
                'enabled':True,'required':True,'default_tools_approval_mode':'approve','tool_timeout_sec':1200}}
            plan=prepare(first,root,trusted_mcp=relay)
            parsed=tomllib.loads('\n'.join(plan['overrides']))
            self.assertEqual(parsed['mcp_servers']['prism_parent']['default_tools_approval_mode'],'approve')
            final=copy.deepcopy(first);final['config']['mcp_servers']=copy.deepcopy(relay)
            verify(final,root,plan)
            final['config']['mcp_servers']['prism_parent']['default_tools_approval_mode']='prompt'
            with self.assertRaisesRegex(ValueError,'exact parent relay'):
                verify(final,root,plan)

    def test_parent_relay_keeps_compile_budget_and_rejects_shortened_timeout(self):
        from codex_config import prepare, verify
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()/'project';first=self.snapshot(root)
            relay={'prism_parent':{'command':'/trusted/python','args':['/trusted/relay.py'],
                'enabled':True,'required':True,'default_tools_approval_mode':'approve','tool_timeout_sec':1200}}
            plan=prepare(first,root,trusted_mcp=relay)
            parsed=tomllib.loads('\n'.join(plan['overrides']))
            self.assertEqual(parsed['mcp_servers']['prism_parent']['tool_timeout_sec'],1200)
            final=copy.deepcopy(first);final['config']['mcp_servers']=copy.deepcopy(relay)
            verify(final,root,plan)
            final['config']['mcp_servers']['prism_parent']['tool_timeout_sec']=30
            with self.assertRaisesRegex(ValueError,'exact parent relay'):
                verify(final,root,plan)
