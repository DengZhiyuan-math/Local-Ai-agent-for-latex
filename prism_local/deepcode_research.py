"""Deep Code's protected exec/MCP path, with editor-owned source operations."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
import tempfile
import uuid

from backends import cli_process, kill_tree
from claude_research import Controller as SharedController, MAX_TEXT
from codex_mcp import Relay
from research_access import NotationStop
import claude_sandbox

MAX_LOG = 2 * MAX_TEXT


def _object(path):
    from backend_deepcode import read_settings
    return read_settings(path,strict=True,nofollow=True)


def settings(backend, job, scratch, relay):
    """Replace project input without changing original settings or the profile."""
    from backend_deepcode import injected_git
    from fsutil import project_path
    user_path = Path.home()/'.deepcode/settings.json'
    project_path_ = project_path(job.root, '.deepcode/settings.json')
    user, project = _object(user_path), _object(project_path_)
    for path, value in ((user_path, user), (project_path_, project)):
        if injected_git(path, value):
            raise ValueError('Deep Code settings inject repository variables.')
    inherited = user.get('mcpServers') or {}
    if not isinstance(inherited, dict):
        raise ValueError('Deep Code MCP settings could not be verified.')
    # --settings replaces project input; an empty user command is skipped by the
    # native MCP merge, so no user server is copied or executed.
    servers = {name:{'command':''} for name in inherited if isinstance(name, str)}
    if len(servers) != len(inherited):
        raise ValueError('Deep Code MCP server names could not be verified.')
    servers[relay.name] = {key:relay.config[key] for key in ('command','args')}
    previous = project.get('permissions') or {}
    if not isinstance(previous, dict) or not isinstance(previous.get('deny', []), list):
        raise ValueError('Deep Code permission settings could not be verified.')
    deny = ['write-in-cwd','write-out-cwd','write-in-tmp','delete-in-cwd','delete-out-cwd',
            'mutate-git-log','network']
    value = dict(project)
    value.update(mcpServers=servers, notify='/usr/bin/true', webSearchTool='/usr/bin/false',
        telemetryEnabled=False, debugLogEnabled=False, statusline={'enabled':False},
        permissions={**previous, 'deny':list(dict.fromkeys(previous.get('deny', [])+deny))})
    path = scratch/'settings.json'
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False)+'\n')
    path.chmod(0o600)
    return path


class Controller(SharedController):
    def __init__(self, job, **kwargs):
        super().__init__(job, **kwargs)
        self.initialized = self.ready = False

    def answer(self, message):
        if isinstance(message,dict) and message.get('method')=='tools/call' and not self.ready:
            return {'jsonrpc':'2.0','id':message.get('id'),'result':{'content':[{'type':'text',
                'text':'The protected project tools have not initialized.'}], 'isError':True}}
        answer = super().answer(message)
        if isinstance(message,dict) and isinstance(answer,dict) and 'result' in answer:
            if message.get('method')=='initialize':
                self.initialized = True
            elif message.get('method')=='tools/list' and self.initialized:
                self.ready = True
        return answer

    def halt(self, stop):
        super().halt(stop)
        # Deep Code's next model request follows a tool response immediately.
        # End the requesting process before returning that response, not on a poll.
        if self.halted and self.job.proc is not None:
            kill_tree(self.job.proc)


class Log:
    """Only this Turn's appended native messages, with bounded input and state."""
    def __init__(self, path, resumed):
        self.path = path
        self.offset = path.stat().st_size if resumed and path.is_file() else 0
        self.inode = path.stat().st_ino if path.is_file() else None
        self.pending = b''
        self.messages = []
        self.total = 0

    def read(self):
        if not self.path.exists():
            return self.messages
        descriptor = os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(descriptor,'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size < self.offset or self.inode not in (None, info.st_ino):
                raise ValueError('The current native progress log changed unexpectedly.')
            self.inode = info.st_ino
            stream.seek(self.offset)
            data = stream.read(MAX_TEXT+1)
        if len(data)+len(self.pending)>MAX_TEXT or self.total+len(data)>MAX_LOG:
            raise ValueError('The current native progress log exceeded the checking limit.')
        self.offset += len(data)
        self.total += len(data)
        data = self.pending+data
        end = data.rfind(b'\n')+1
        self.pending = data[end:]
        for line in data[:end].splitlines():
            value = json.loads(line)
            if not isinstance(value,dict):
                raise ValueError('Invalid native progress message.')
            self.messages.append(value)
        if len(self.messages)>4096:
            raise ValueError('The current native progress log exceeded its message limit.')
        return self.messages


def message(backend, job):
    instructions = backend.system_text().replace(backend.file_tool_rule,
        '- Use the editor MCP project tools to read, search, write and compile.\n'
        '  Native writes cannot change the manuscript.\n'
        '  A saved notation proposal ends this Turn; await a later human reply.\n')
    return ('[Instructions from the editor]\nThese replace earlier editor instructions.\n'
            + instructions+'\n[Message]\n'+job.prompt+'\n\n'+job.research_access.context())


def run(backend, job):
    from backend_deepcode import node_entry, sessions_dir
    from fsutil import is_metadata
    original_writable = job.writable
    job.writable = lambda rel: original_writable(rel) and not is_metadata(Path(rel)) \
        and not any(part.lower()=='.deepcode' for part in Path(rel).parts)
    runtime = claude_sandbox.availability('deepcode')
    if not runtime['supported']:
        raise ValueError(runtime['reason'])
    profile = Path.home()/'.deepcode'
    profile.mkdir(parents=True, exist_ok=True)
    guard = Controller(job, provider='deepcode')
    sid = job.session_id or str(uuid.uuid4())
    state = {'billing':'api','session_id':sid}
    raw = []
    raw_size = 0
    progress_log = Log(sessions_dir(job.root)/(sid+'.jsonl'), bool(job.session_id))
    seen, results = set(), set()
    with tempfile.TemporaryDirectory(prefix='prism-deepcode-') as temporary:
        scratch = Path(temporary)
        with Relay(guard, scratch) as relay:
            path = settings(backend, job, scratch, relay)
            entry = node_entry(backend.bin())
            cmd = entry + ['--exec','--json','--settings',str(path),'--prompt',
                           'Do what the message in the <stdin> block asks.']
            cmd += ['--resume',sid] if job.session_id else ['--session-id',sid]
            runtime_paths = [Path(entry[1]).resolve().parent] if len(entry)==2 and Path(entry[1]).name=='cli.js' else []
            cmd = claude_sandbox.wrap(cmd, job.root, profile, scratch, provider='deepcode', runtime_paths=runtime_paths)
            env = claude_sandbox.environment(backend.env(job), scratch)
            env.update(DEEPCODE_NOTIFY='/usr/bin/true', DEEPCODE_WEB_SEARCH_TOOL='/usr/bin/false',
                       DEEPCODE_TELEMETRY_ENABLED='false', DEEPCODE_DEBUG_LOG_ENABLED='false')
            relay.active.set()
            def progress():
                if not guard.halted and not job.cancel.is_set() and progress_log.path.exists():
                    if backend.check_session(job.root,sid):
                        return
                    backend._replay_tools(guard.proxy,sid,seen=seen,results=results,log=progress_log)
            def idle():
                if relay.error and not guard.halted:
                    guard.halt(NotationStop(error='The protected Deep Code transport failed.'))
                try:
                    progress()
                except (OSError,ValueError,TypeError,AttributeError,KeyError,RecursionError):
                    guard.halt(NotationStop(error='Deep Code progress could not be checked.'))
            with cli_process(job, cmd, message(backend,job), env, on_idle=idle) as (proc, lines, errors):
                if proc is None:
                    return state
                try:
                    for line in lines:
                        raw.append(line)
                        raw_size += len(line)
                        if raw_size > MAX_TEXT:
                            guard.halt(NotationStop(error='The protected reply exceeded the checking limit.'))
                            break
                    proc.wait()
                finally:
                    relay.close()
                state['exit'] = proc.returncode
                if relay.forced_cancel:
                    state['cancelled_by_backend'] = True
                if relay.error:
                    state.update(is_error=True,error='The protected Deep Code transport failed.',exit=1)
                if not guard.ready and not job.cancel.is_set():
                    state.update(is_error=True,error='Deep Code did not initialize the required protected tools.',exit=1)
                if not job.cancel.is_set() and not guard.halted and not state.get('is_error'):
                    if proc.returncode:
                        state.update(is_error=True,error='Deep Code failed before completing the Turn.')
                    else:
                        try:
                            progress()
                            if backend.check_session(job.root,sid):
                                raise ValueError('The native project session could not be verified.')
                            result = json.loads(''.join(raw))
                            if not isinstance(result,dict) or result.get('role')!='assistant' or result.get('sessionId')!=sid or not isinstance(result.get('content',''),str):
                                raise ValueError('Invalid complete assistant response')
                            params = result.get('messageParams',{})
                            if not isinstance(params,dict):
                                raise ValueError('Invalid assistant metadata')
                            guard.event({'t':'message_start'})
                            guard.event({'t':'text','text':result.get('content','')})
                            if isinstance(params.get('reasoning_content'),str):
                                guard.event({'t':'thinking','text':params['reasoning_content']})
                            guard.flush()
                        except NotationStop as stop:
                            guard.halt(stop)
                        except (OSError,ValueError,TypeError,AttributeError,KeyError,RecursionError):
                            state.update(is_error=True,error='Deep Code did not return a complete checked reply.',exit=1)
                guard.sanitize_result(state, ''.join(errors))
                if relay.error:
                    state.update(is_error=True,error='The protected Deep Code transport failed.',exit=1)
    bad = backend.check_session(job.root,sid)
    if bad and not job.cancel.is_set():
        state.update(is_error=True,error='Deep Code did not save a verifiable project session.',exit=1)
    elif not bad:
        job.emit({'t':'init','session_id':sid,'model':job.model})
    return state
