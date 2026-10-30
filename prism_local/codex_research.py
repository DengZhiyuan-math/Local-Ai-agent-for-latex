"""Codex app-server transport for editor-owned, checked project operations."""
from __future__ import annotations

import json
import os
from pathlib import Path
from contextlib import nullcontext
import tempfile
import threading

from backends import agent_env, cli_process, kill_tree, attachment_type
from fsutil import project_path
from guarded_compile import _read_plain
from claude_research import Controller, MAX_TEXT
from research_access import NotationStop
import claude_sandbox
import sessionmeta
import codex_config

INITIALIZE_TIMEOUT = 15


def command(backend, overrides):
    """The server retains native history; it has no manuscript write authority."""
    cmd = [backend.bin(), 'app-server', '--listen', 'stdio://']
    for setting in overrides:
        cmd += ['-c', setting]
    return cmd


def inputs(job, prompt):
    """Native image inputs and referenced text never supply human authority."""
    result = [{'type':'text','text':prompt,'text_elements':[]}]
    for rel in job.attachments:
        path = project_path(job.root, rel)
        raw = _read_plain(job.root, rel, 25*1024*1024)
        if attachment_type(path) == 'image':
            result.append({'type':'localImage','path':str(path)})
        else:
            result.append({'type':'text','text':'[Attached project file '+rel+']\n'+raw.decode('utf-8'),
                           'text_elements':[]})
    return result


def has_dynamic_tools(profile, root, session_id, definitions):
    """A legacy rollout cannot acquire dynamic tools through thread/resume.

    This selects a transport, never notation authority. Unknown metadata uses the
    checked MCP relay instead of editing native history or trusting a mirror.
    """
    if not session_id:
        return True
    expected = {tool['name']:tool['inputSchema'] for tool in definitions}
    for directory in (profile/'sessions', profile/'archived_sessions'):
        for path in directory.rglob(f'rollout-*-{session_id}.jsonl'):
            if not sessionmeta.matches(path, root, session_id):
                continue
            try:
                with path.open(encoding='utf-8') as stream:
                    for _ in range(30):
                        line = stream.readline(1024*1024)
                        if not line:
                            break
                        record = json.loads(line)
                        if not isinstance(record, dict) or record.get('type') != 'session_meta':
                            continue
                        payload = record.get('payload')
                        if not isinstance(payload, dict) or payload.get('id') != session_id:
                            continue
                        tools = payload.get('dynamic_tools')
                        if not isinstance(tools, list):
                            return False
                        actual = {}
                        for tool in tools:
                            if not isinstance(tool, dict) or not isinstance(tool.get('name'), str):
                                return False
                            actual[tool['name']] = tool.get('inputSchema', tool.get('input_schema'))
                        return actual == expected and len(tools) == len(expected)
            except (OSError, ValueError, TypeError):
                return False
    return False


def instructions(backend):
    return backend.system_text().replace(backend.file_tool_rule,
        '- Use the editor-owned project tools for reading, searching, writing and compiling.\n'
        '  Native shell and file edits cannot change the manuscript.\n'
        '  A saved notation proposal ends this Turn; await a later human reply.\n')


def run(backend, job):
    from backend_codex import protected_scope
    original_writable = job.writable
    job.writable = lambda rel: original_writable(rel) and not protected_scope(job.root, rel)
    guard = Controller(job, provider='codex')
    state = {}
    profile = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    runtime = claude_sandbox.availability('codex')
    if not runtime['supported']:
        raise ValueError(runtime['reason'])
    initial = {'id':'prism-initialize', 'method':'initialize', 'params':{
        'clientInfo':{'name':'prism_local','version':'1'},
        'capabilities':{'experimentalApi':True}}}
    with tempfile.TemporaryDirectory(prefix='prism-codex-') as temporary:
        scratch = Path(temporary)
        from codex_mcp import Relay
        relay_context = nullcontext(None) if has_dynamic_tools(profile, job.root, job.session_id, guard.definitions(all_tools=True)) else Relay(guard, scratch)
        with relay_context as relay:
            plan = None
            for _attempt in range(2):
                cmd = claude_sandbox.wrap(command(backend, plan['overrides'] if plan else codex_config.base_overrides(job.root)), job.root, profile, scratch, provider='codex')
                env = claude_sandbox.environment(agent_env(root=job.root), scratch)
                with cli_process(job, cmd, json.dumps(initial)+'\n', env, keep_stdin_open=True, on_idle=lambda: check_idle(),
                                 bufsize=1) as (proc, lines, errors):
                    if proc is None:
                        return state
                    phase, thread_id, turn_id = 'initialize', None, None
                    initialization_expired = threading.Event()
                    interrupted = False
                    completed = False
                    discovery = False
                    drafts = {}
                    tool_results = {}
                    interrupt_timer = None

                    def expire():
                        initialization_expired.set()
                        kill_tree(proc)

                    timer = threading.Timer(INITIALIZE_TIMEOUT, expire)
                    timer.daemon = True
                    timer.start()

                    def send(frame):
                        if not job.cancel.is_set() and proc.poll() is None:
                            try:
                                proc.stdin.write(json.dumps(frame)+'\n')
                                proc.stdin.flush()
                            except (OSError, ValueError):
                                pass

                    def interrupt():
                        nonlocal interrupted, interrupt_timer
                        if guard.halted and not interrupted and thread_id and turn_id:
                            interrupted = True
                            send({'id':'prism-interrupt','method':'turn/interrupt',
                                  'params':{'threadId':thread_id,'turnId':turn_id}})
                            interrupt_timer = threading.Timer(1, kill_tree, args=(proc,))
                            interrupt_timer.daemon = True
                            interrupt_timer.start()

                    def check_idle():
                        if relay and relay.error and not guard.halted:
                            guard.halt(NotationStop(error='The protected project-tool relay failed.'))
                        interrupt()

                    def start_thread():
                        nonlocal phase
                        thread_params = {'cwd':str(job.root.resolve()), 'approvalPolicy':'never',
                            'sandbox':'read-only', 'baseInstructions':instructions(backend),
                            'config':plan['config']}
                        if job.model:
                            thread_params['model'] = job.model
                        if job.session_id:
                            thread_params['threadId'] = job.session_id
                        else:
                            thread_params['dynamicTools'] = [{'type':'function', **tool}
                                for tool in guard.definitions(all_tools=True)]
                        send({'id':'prism-thread','method':'thread/resume' if job.session_id else 'thread/start',
                              'params':thread_params})
                        phase = 'thread'

                    try:
                        for line in lines:
                            try:
                                frame = json.loads(line)
                            except ValueError:
                                continue
                            if not isinstance(frame, dict):
                                continue
                            mid, method = frame.get('id'), frame.get('method')
                            params = frame.get('params') or {}
                            if method and mid is not None:
                                result = {'contentItems':[{'type':'inputText','text':'This protected tool call is not permitted.'}], 'success':False}
                                if method == 'item/tool/call':
                                    if (phase == 'running' and isinstance(params, dict)
                                            and params.get('threadId') == thread_id
                                            and params.get('turnId') == turn_id
                                            and not params.get('namespace')):
                                        call_id = params.get('callId')
                                        signature = json.dumps([params.get('tool'), params.get('arguments')], sort_keys=True)
                                        if isinstance(call_id, str) and call_id:
                                            previous = tool_results.get(call_id)
                                            if previous is not None:
                                                if previous[0] == signature:
                                                    result = previous[1]
                                            else:
                                                answer = guard.answer({'jsonrpc':'2.0','id':call_id,
                                                    'method':'tools/call','params':{'name':params.get('tool'),
                                                    'arguments':params.get('arguments')}})
                                                if answer and 'result' in answer:
                                                    value = answer['result']
                                                    result = {'contentItems':[{'type':'inputText','text':part['text']}
                                                        for part in value.get('content', []) if part.get('type')=='text'],
                                                        'success':not value.get('isError', True) and not guard.halted}
                                                tool_results[call_id] = (signature, result)
                                    send({'id':mid,'result':result})
                                    interrupt()
                                else:
                                    send({'id':mid,'error':{'code':-32601,'message':'This protected client does not expose that operation.'}})
                                continue
                            if not method and mid is not None:
                                if frame.get('error'):
                                    raise ValueError('Codex control request failed.')
                                value = frame.get('result')
                                if not isinstance(value, dict):
                                    raise ValueError('Codex returned an invalid control response.')
                                if phase == 'initialize' and mid == 'prism-initialize':
                                    send({'method':'initialized'})
                                    send({'id':'prism-config','method':'config/read','params':{'includeLayers':True}})
                                    phase = 'config'
                                elif phase == 'config' and mid == 'prism-config':
                                    if plan is None:
                                        plan = codex_config.prepare(value, job.root, trusted_mcp={relay.name:relay.config}) if relay else codex_config.prepare(value, job.root)
                                        discovery = True
                                        break
                                    codex_config.verify(value, job.root, plan)
                                    start_thread()
                                elif phase == 'thread' and mid == 'prism-thread':
                                    thread = value.get('thread')
                                    if not isinstance(thread, dict) or not sessionmeta.valid_id(thread.get('id')):
                                        raise ValueError('Codex returned an invalid conversation.')
                                    thread_id = thread['id']
                                    if job.session_id and thread_id != job.session_id:
                                        raise ValueError('Codex restored another conversation.')
                                    if thread.get('cwd') and Path(thread['cwd']).resolve() != job.root.resolve():
                                        raise ValueError('Codex restored another project.')
                                    state['session_id'] = thread_id
                                    guard.event({'t':'init','session_id':thread_id,'model':job.model})
                                    prompt = job.prompt+'\n\n'+job.research_access.context()
                                    turn_params = {'threadId':thread_id,'cwd':str(job.root.resolve()),
                                        'approvalPolicy':'never','sandboxPolicy':{'type':'readOnly'},
                                        'input':inputs(job, prompt)}
                                    if job.model:
                                        turn_params['model'] = job.model
                                    if job.effort:
                                        turn_params['effort'] = job.effort
                                    send({'id':'prism-turn','method':'turn/start','params':turn_params})
                                    phase = 'turn'
                                elif phase == 'turn' and mid == 'prism-turn':
                                    turn = value.get('turn')
                                    if not isinstance(turn, dict) or not sessionmeta.valid_id(turn.get('id')):
                                        raise ValueError('Codex returned an invalid Turn.')
                                    turn_id = turn['id']
                                    phase = 'running'
                                    if relay:
                                        relay.active.set()
                                    timer.cancel()
                                continue
                            if phase != 'running' or not isinstance(params, dict):
                                continue
                            if params.get('threadId') != thread_id:
                                continue
                            if method and method.startswith('item/') and params.get('turnId') != turn_id:
                                continue
                            if params.get('turnId') is not None and params['turnId'] != turn_id:
                                continue
                            try:
                                if method == 'item/agentMessage/delta' and not guard.halted:
                                    iid = params.get('itemId')
                                    if isinstance(iid, str) and isinstance(params.get('delta'), str):
                                        drafts[iid] = drafts.get(iid, '') + params['delta']
                                        if len(drafts[iid]) > MAX_TEXT:
                                            raise NotationStop(error='The protected reply exceeded the checking limit.')
                                elif method == 'item/completed' and not guard.halted:
                                    item = params.get('item')
                                    if isinstance(item, dict) and item.get('type') == 'agentMessage':
                                        drafts.pop(item.get('id'), None)
                                        if isinstance(item.get('text'), str):
                                            guard.event({'t':'message_start'})
                                            guard.event({'t':'text','text':item['text']})
                                    elif isinstance(item, dict) and item.get('type') != 'dynamicToolCall':
                                        converted = dict(item)
                                        converted['type'] = {'commandExecution':'command_execution',
                                            'fileChange':'file_change', 'mcpToolCall':'mcp_tool_call',
                                            'webSearch':'web_search'}.get(item.get('type'), item.get('type'))
                                        for source, target in (('aggregatedOutput','aggregated_output'), ('exitCode','exit_code')):
                                            if source in item:
                                                converted[target] = item[source]
                                        if item.get('type') == 'reasoning':
                                            converted['text'] = '\n'.join(str(x) for x in item.get('summary', [])
                                                                          if isinstance(x, str))
                                        backend._item(converted, True, guard.proxy, state)
                                elif method == 'error':
                                    error = params.get('error')
                                    message = str(error.get('message') or 'Codex failed.') if isinstance(error, dict) else 'Codex failed.'
                                    retrying = params.get('willRetry') is True
                                    guard.event({'t':'error','notice':retrying,'message':message})
                                    if not retrying:
                                        state.update(is_error=True, error=message)
                                elif method in ('warning', 'configWarning'):
                                    guard.event({'t':'error','notice':True,
                                        'message':str(params.get('message') or params.get('summary') or 'Codex warning')})
                            except NotationStop as stop:
                                guard.halt(stop)
                            interrupt()
                            if method == 'turn/completed':
                                turn = params.get('turn') or {}
                                if turn.get('id') != turn_id:
                                    continue
                                completed = True
                                if not guard.halted and not job.cancel.is_set():
                                    guard.flush()
                                if turn.get('status') != 'completed' and not guard.halted:
                                    state.update(is_error=True, error='Codex Turn did not complete.')
                                break
                    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                        state.update(is_error=True, error=str(error))
                    finally:
                        if relay:
                            relay.active.clear()
                        timer.cancel()
                        if interrupt_timer:
                            interrupt_timer.cancel()
                    if initialization_expired.is_set() and not job.cancel.is_set():
                        state.update(is_error=True, error='Codex did not initialize the protected Turn in time.')
                if discovery:
                    continue
                if relay:
                    relay.close()
                    if relay.forced_cancel:
                        state['cancelled_by_backend'] = True
                if not completed and not job.cancel.is_set() and not guard.halted:
                    state.update(is_error=True, error=state.get('error') or 'Codex ended before completing the Turn.')
                state['exit'] = 0 if completed and not state.get('is_error') else proc.returncode or 1
                guard.sanitize_result(state, ''.join(errors))
                if relay and relay.error:
                    state.update(exit=1, is_error=True, subtype='tool relay failure',
                                 error='The protected project-tool relay failed.')
                return state
