"""Unittest discovery with isolated state and explicit acceptance boundaries."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

repo=Path(__file__).resolve().parents[4]
v=Path(__file__).parent
baseline=json.loads((v/'baseline.json').read_text())
initial_test_files=({str(p.relative_to(repo)) for p in (repo/'tests').glob('test*.py')} if '--final' in sys.argv else {name for name in baseline['files'] if name.startswith('tests/test') and name.endswith('.py')})
selected_modules=next((arg.split('=',1)[1].split(',') for arg in sys.argv if arg.startswith('--modules=')),None)
def source_snapshot():
    paths=[p for root in ('prism_local','tests') for p in (repo/root).rglob('*')
           if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc','.pyo') and p.name!='.DS_Store' and not p.name.startswith('._')]
    paths += [repo/name for name in ('README.md','CONTEXT.md','AGENTS.md','pyproject.toml') if (repo/name).is_file()]
    files={str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    return {'files':files,'sha256':hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()}
source_before=source_snapshot()
original_which=shutil.which
restricted_names=('claude','codex','deepcode','gh','pdflatex','xelatex','lualatex','latex','tectonic','latexmk','kpsewhich','biber','bibtex','makeindex','dvipdfmx','osascript','osacompile','codesign','open')
restricted={str(Path(p).resolve()) for name in restricted_names if (p:=original_which(name))}
tex_names={'pdflatex','xelatex','lualatex','latex','tectonic','latexmk','kpsewhich','biber','bibtex','makeindex','dvipdfmx'}
blocked_processes=[]
blocked_connections=[]


def audit(event,args):
    if event=='socket.connect':
        address=args[1]
        if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
            blocked_connections.append(str(address[0]))
            raise AssertionError('External service connection forbidden in integration fixtures')
    elif event=='subprocess.Popen':
        executable,command,cwd,environment=args
        environment=environment or os.environ
        candidates=[executable]+(list(command) if isinstance(command,(list,tuple)) else [])
        for item in candidates:
            if not isinstance(item,(str,bytes,os.PathLike)):
                continue
            item=os.fsdecode(item)
            if len(item)>4096 or '\n' in item:
                continue
            try:
                found=original_which(item,path=environment.get('PATH')) if '/' not in item else item
                resolved=str(Path(found).resolve()) if found else ''
            except (OSError,ValueError):
                continue
            if resolved in restricted:
                blocked_processes.append({'executable':str(executable),'program':item})
                raise AssertionError('Real provider, TeX, native app or account CLI forbidden: '+item)


def safe_which(name,*args,**kwargs):
    found=original_which(name,*args,**kwargs)
    if found and str(Path(found).resolve()) in restricted:
        return None
    return found


class SnapshotLoader(unittest.TestLoader):
    def _match_path(self,path,full_path,pattern):
        relative=str(Path(full_path).relative_to(repo))
        return relative in initial_test_files and super()._match_path(path,full_path,pattern)


def flatten(suite):
    for case in suite:
        if isinstance(case,unittest.TestSuite):yield from flatten(case)
        else:yield case


with tempfile.TemporaryDirectory(prefix='prism-integration-full-',dir='/private/tmp') as temporary:
    work=Path(temporary)
    for name in ('state','config','data','xdg-state','cache','home','trash'):(work/name).mkdir()
    (work/'agents.json').write_text('{}\n')
    (work/'empty-git-config').write_text('')
    clean=dict(os.environ)
    for name in list(clean):
        if name.startswith(('OPENAI_','ANTHROPIC_','DEEPCODE_','CODEX_','CLAUDE_','AZURE_OPENAI_','GH_','GITHUB_')) or name in ('PRISM_NATIVE_CODEX','PYTHONHOME','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy','GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_OBJECT_DIRECTORY','GIT_ALTERNATE_OBJECT_DIRECTORIES','GIT_SSH_COMMAND','GIT_CONFIG_PARAMETERS') or name.startswith(('GIT_CONFIG_KEY_','GIT_CONFIG_VALUE_')):
            clean.pop(name,None)
    clean.update(PRISM_STATE_DIR=str(work/'state'),PRISM_AGENTS=str(work/'agents.json'),PRISM_SNIPPETS=str(work/'snippets.js'),
        PRISM_TRASH_DIR=str(work/'trash'),PRISM_CLIPBOARD_FILE=str(work/'clipboard.json'),PRISM_AUTO_UPDATE='0',PRISM_SKIP_TEX='1',
        XDG_CONFIG_HOME=str(work/'config'),XDG_DATA_HOME=str(work/'data'),XDG_STATE_HOME=str(work/'xdg-state'),XDG_CACHE_HOME=str(work/'cache'),
        GIT_CONFIG_GLOBAL=str(work/'empty-git-config'),GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_COUNT='0',GIT_TERMINAL_PROMPT='0',GIT_ALLOW_PROTOCOL='file',
        NO_PROXY='127.0.0.1,localhost,::1',no_proxy='127.0.0.1,localhost,::1',PYTHONDONTWRITEBYTECODE='1')
    original_home=Path.home
    original_expanduser=os.path.expanduser
    host_home=os.environ.get('HOME')
    def isolated_home(cls):
        return original_home() if os.environ.get('HOME') and os.environ.get('HOME')!=host_home else work/'home'
    def isolated_expanduser(path):
        if isinstance(path,str) and (path=='~' or path.startswith('~/')) and (not os.environ.get('HOME') or os.environ.get('HOME')==host_home):
            return str(work/'home')+path[1:]
        return original_expanduser(path)
    sys.path[:0]=[str(repo/'tests'),str(repo/'prism_local')]
    sys.addaudithook(audit)
    launch=subprocess.Popen
    def local_git_process(command,*args,**kwargs):
        parts=list(command) if isinstance(command,(list,tuple)) else []
        if parts and Path(str(parts[0])).name=='git' and any(str(item).startswith('http://127.0.0.1:') for item in parts):
            kwargs['env']={**(kwargs.get('env') or os.environ),'GIT_ALLOW_PROTOCOL':'file:http'}
        return launch(command,*args,**kwargs)
    started=time.monotonic()
    with mock.patch.dict(os.environ,clean,clear=True),mock.patch.object(Path,'home',classmethod(isolated_home)),mock.patch.object(os.path,'expanduser',side_effect=isolated_expanduser),mock.patch.object(shutil,'which',side_effect=safe_which),mock.patch.object(subprocess,'Popen',side_effect=local_git_process):
        import github as github_module
        offline_status=mock.patch.object(github_module.GITHUB,'status',return_value={'gh':None,'logged_in':False,'account':None,'error':'Offline integration fixture; connect through explicit test adapters only.'})
        offline_status.start()
        loader=SnapshotLoader()
        suite=(loader.loadTestsFromNames(selected_modules) if selected_modules else loader.discover(str(repo/'tests'),pattern='test*.py'))
        native_app_boundary={
            'test_macos_app.MacApp.test_browser_selection_scripts_compile_natively',
            'test_macos_app.MacApp.test_bundle_runs_launcher_with_quoted_paths',
            'test_macos_app.MacApp.test_installer_refuses_an_unrelated_existing_app'}
        cases=list(flatten(suite))
        # This pre-existing test class explicitly exercises Windows winget and
        # Windows known-install locations; simulate its selectors locally.
        tools_tests=sys.modules.get('test_tools')
        if tools_tests is not None:
            real_tools_os=tools_tests.tools.os
            class FixtureWindowsOS:
                name='nt'
                def __getattr__(self,name):return getattr(real_tools_os,name)
            initial_setup=tools_tests.Tools.setUp
            def windows_fixture_setup(case):
                initial_setup(case)
                case.enterContext(mock.patch.object(tools_tests.tools,'os',FixtureWindowsOS()))
                case.enterContext(mock.patch.object(tools_tests.tools,'_system',return_value='windows'))
            tools_tests.Tools.setUp=windows_fixture_setup
        for case in cases:
            if case.id() in native_app_boundary:
                function=getattr(type(case),case._testMethodName)
                setattr(type(case),case._testMethodName,unittest.skip('Outside current local fixture acceptance: native app build/sign/install/AppleScript')(function))
        (v/'full-discovered.json').write_text(json.dumps({'tests':[case.id() for case in cases],'discovered_modules':len(initial_test_files),'explicit_boundary_skips':sorted(native_app_boundary),'discovery_mode':'current_all_modules' if '--final' in sys.argv else 'initial_snapshot_modules','source_before':source_before},indent=2)+'\n')
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        offline_status.stop()
    source_after=source_snapshot()
    source_drift=sorted(name for name in source_before['files'].keys()|source_after['files'].keys() if source_before['files'].get(name)!=source_after['files'].get(name))
    summary={'tests':result.testsRun,'seconds':round(time.monotonic()-started,3),'failures':[{'test':test.id(),'traceback':detail} for test,detail in result.failures],'errors':[{'test':test.id(),'traceback':detail} for test,detail in result.errors],'skipped':[{'test':test.id(),'reason':reason} for test,reason in result.skipped],'blocked_processes':blocked_processes,'blocked_connections':blocked_connections,'discovered_modules':len(initial_test_files),'scope':'Full current unittest test*.py discovery, including joined integration modules' if '--final' in sys.argv else 'Initial frozen test*.py modules','source_before':source_before,'source_after':source_after,'source_drift':source_drift,'isolation':'temporary PRISM/XDG/user-path defaults; HOME/CODEX_HOME not repurposed; local-only Git protocol; Python process/network audit guards; no claim of a global OS sandbox for native descendants'}
    output=next((arg.split('=',1)[1] for arg in sys.argv if arg.startswith('--output=')),'full-regression')
    summary['selected_modules']=selected_modules
    summary['windows_winget_fixture']='Scoped tools.os and _system selectors; no global os.name change'
    (v/(output+'.json')).write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({key:value for key,value in summary.items() if key not in ('failures','errors','skipped','source_before','source_after')},indent=2))
    print('failure_count',len(result.failures),'error_count',len(result.errors),'skipped_count',len(result.skipped))
sys.exit(0 if result.wasSuccessful() and not blocked_processes and not blocked_connections and not source_drift else 1)
