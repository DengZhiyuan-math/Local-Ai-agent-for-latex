"""Fail-closed configuration checks before starting a controlled Codex thread.

The app-server config/read response includes both effective configuration and raw
source layers. The latter discover server names; the former proves our overrides
won. No configuration, credential, auth file or native rollout is rewritten.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FEATURES = ('shell_tool', 'hooks', 'apps', 'plugins', 'remote_plugin',
            'multi_agent', 'skill_mcp_dependency_install')
LAYERS = {'packagedDefaults', 'mdm', 'system', 'enterpriseManaged', 'user', 'project', 'sessionFlags'}


def _toml(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return '[' + ','.join(_toml(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key) + '=' + _toml(item) for key, item in value.items()) + '}'
    raise ValueError('Unsupported controlled Codex setting.')


def _base_config(root):
    return {'features':dict.fromkeys(FEATURES, False), 'agents':{'enabled':False},
            'approval_policy':'never', 'sandbox_mode':'read-only', 'web_search':'disabled',
            'analytics':{'enabled':False}, 'check_for_update_on_startup':False,
            'projects':{str(root.resolve()):{'trust_level':'untrusted'}}}


def base_overrides(root: Path) -> list[str]:
    return [key + '=' + _toml(value) for key, value in _base_config(root).items()]


def _snapshot(result, root):
    if not isinstance(result, dict) or not isinstance(result.get('config'), dict) \
            or not isinstance(result.get('layers'), list):
        raise ValueError('Codex did not return complete configuration layers.')
    layers = result['layers']
    for layer in layers:
        if not isinstance(layer, dict) or not isinstance(layer.get('name'), dict) \
                or layer['name'].get('type') not in LAYERS or not isinstance(layer.get('config'), dict):
            raise ValueError('Codex returned an unsupported configuration layer.')
    # CLI overrides change the session layer, not the source files we discovered.
    sources = [x for x in layers if x['name']['type'] != 'sessionFlags']
    fingerprint = hashlib.sha256(json.dumps([str(root.resolve()), sources], sort_keys=True,
                                           separators=(',', ':')).encode()).hexdigest()
    servers = result['config'].get('mcp_servers', {})
    if not isinstance(servers, dict) or any(not isinstance(name, str) or not isinstance(value, dict)
                                            for name, value in servers.items()):
        raise ValueError('Codex returned an invalid MCP configuration.')
    return result['config'], sources, servers, fingerprint


def prepare(result: dict, root: Path, *, trusted_mcp=None) -> dict:
    config, sources, servers, fingerprint = _snapshot(result, root)
    names = set(servers)
    for layer in sources:
        entries = layer['config'].get('mcp_servers', {})
        if not isinstance(entries, dict):
            raise ValueError('Codex returned an invalid MCP source layer.')
        names.update(entries)
    disabled = {}
    for name in sorted(names):
        server = servers.get(name)
        # Disabled project sources need no transport copied from untrusted data.
        if server is None:
            continue
        if not isinstance(name, str) or not isinstance(server, dict):
            raise ValueError('Codex returned an invalid MCP server.')
        transport = 'command' if isinstance(server.get('command'), str) else 'url'
        value = server.get(transport)
        if not isinstance(value, str) or not value:
            raise ValueError('Codex returned an unsupported MCP transport.')
        inert = '/usr/bin/false' if transport == 'command' else 'http://127.0.0.1:1/'
        disabled[name] = {transport:inert, 'enabled':False}
    trusted = {}
    if trusted_mcp is not None:
        if not isinstance(trusted_mcp, dict) or len(trusted_mcp) != 1:
            raise ValueError('The parent relay configuration is invalid.')
        for name, entry in trusted_mcp.items():
            if not isinstance(name, str) or not name or name in names \
                    or not isinstance(entry, dict) or set(entry) != {'command','args','enabled','required','default_tools_approval_mode','tool_timeout_sec'} \
                    or not isinstance(entry['command'], str) or not entry['command'] \
                    or not isinstance(entry['args'], list) or not all(isinstance(a,str) for a in entry['args']) \
                    or entry['enabled'] is not True or entry['required'] is not True \
                    or entry['default_tools_approval_mode'] != 'approve' \
                    or type(entry['tool_timeout_sec']) is not int or entry['tool_timeout_sec'] != 1200:
                raise ValueError('The parent relay configuration is invalid or conflicts with a source.')
            trusted[name] = {**entry, 'args':list(entry['args'])}
    settings = _base_config(root)
    settings['mcp_servers'] = {**disabled, **trusted}
    return {'overrides':[key + '=' + _toml(value) for key, value in settings.items()],
            'config':settings, 'servers':sorted(settings['mcp_servers']),
            'trusted_mcp':trusted, 'fingerprint':fingerprint}


def verify(result: dict, root: Path, plan: dict) -> None:
    config, sources, servers, fingerprint = _snapshot(result, root)
    if fingerprint != plan['fingerprint']:
        raise ValueError('Codex configuration changed before the controlled thread started.')
    if set(servers) != set(plan['servers']):
        raise ValueError('Codex MCP configuration is not disabled.')
    for name, value in servers.items():
        expected = plan['trusted_mcp'].get(name)
        if expected is None:
            if value.get('enabled') is not False:
                raise ValueError('Codex MCP configuration is not disabled.')
            continue
        actual = dict(value)
        for key, default in (('environment_id','local'), ('tool_timeout_sec',None)):
            if actual.get(key) == default:
                actual.pop(key, None)
        if actual != expected:
            raise ValueError('Codex did not preserve the exact parent relay configuration.')
    features = config.get('features') or {}
    if any(features.get(name) is not False for name in FEATURES) \
            or (config.get('agents') or {}).get('enabled') is not False \
            or config.get('approval_policy') != 'never' or config.get('web_search') != 'disabled' \
            or config.get('sandbox_mode') != 'read-only':
        raise ValueError('Codex did not apply the controlled runtime settings.')
    if (config.get('projects') or {}).get(str(root.resolve()), {}).get('trust_level') != 'untrusted' \
            or any(not layer.get('disabledReason') for layer in sources if layer['name']['type'] == 'project'):
        raise ValueError('Codex project configuration is not disabled.')
