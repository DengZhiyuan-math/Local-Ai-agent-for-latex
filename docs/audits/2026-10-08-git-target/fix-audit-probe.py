"""Offline diagnostics: temporary fixtures only; no network and no push."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'prism_local'))
import gitsync
import backend_deepcode


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True,
                          text=True, check=True).stdout.strip()


def run():
    sources = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
               for name in ('prism_local/gitsync.py', 'prism_local/backend_deepcode.py')}
    result = {'sources_before': sources}
    with tempfile.TemporaryDirectory(dir='/private/tmp', prefix='spec-') as name:
        base = Path(name)
        root = base / 'paper'
        root.mkdir()
        remote = base / 'remote.git'
        git(base, 'init', '-q', '--bare', str(remote))
        git(root, 'init', '-q', '-b', 'main')
        git(root, 'remote', 'add', 'origin', str(remote))
        sync = gitsync.GitSync(lambda: root, lambda: 'build')
        sync.bind_target()
        original = sync._settings()['sync_target']
        moved = base / 'renamed'
        root.rename(moved)
        renamed = gitsync.GitSync(lambda: moved, lambda: 'build')
        valid = renamed.validate_target()
        candidate = renamed.target
        result['rename_binding'] = {
            'accepted': valid,
            'same_remote_and_ref': all(original[k] == candidate[k] for k in
                                       ('remote', 'branch_ref', 'fetch_url', 'push_url', 'configuration_fingerprint')),
            'git_dir_changed': original['git_dir'] != candidate['git_dir'],
            'reason': renamed.blocked_reason,
        }
        foreign = base / 'foreign'
        foreign.mkdir()
        git(foreign, 'init', '-q')
        for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'),
                           ('commit.gpgsign', 'false')):
            git(foreign, 'config', key, value)
        (foreign / 'main.tex').write_text('FOREIGN_FIXTURE', encoding='utf-8')
        git(foreign, 'add', '.')
        git(foreign, 'commit', '-qm', 'fixture')
        oid = git(foreign, 'rev-parse', 'HEAD:main.tex')
        subprocess.run(['git', 'update-index', '--index-info'], cwd=foreign,
                       input=f'0 {"0" * 40}\tmain.tex\n100644 {oid} 2\tmain.tex\n',
                       capture_output=True, text=True, check=True)
        with mock.patch.dict(os.environ, {'GIT_DIR': str(foreign / '.git'), 'GIT_WORK_TREE': str(foreign)}):
            result['blob_environment'] = {
                'normal_git_uses_paper': renamed.git('rev-parse', '--show-toplevel').stdout.strip() == str(moved),
                'blob_reads_foreign': renamed._blob('HEAD:main.tex') == b'FOREIGN_FIXTURE',
                'conflict_stage_reads_foreign': renamed._blob(':2:main.tex') == b'FOREIGN_FIXTURE',
            }
        a, b = base / 'a-b/c', base / 'a/b-c'
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        with mock.patch.object(Path, 'home', return_value=base / 'home'):
            directory = backend_deepcode.sessions_dir(a)
            directory.mkdir(parents=True)
            (directory / 'sessions-index.json').write_text(json.dumps({'entries': [{'id': 'sid'}]}), encoding='utf-8')
            (directory / 'sid.jsonl').write_text('{}\n', encoding='utf-8')
            backend = backend_deepcode.DeepCode('deepcode')
            result['deepcode_collision'] = {
                'different_roots': a != b,
                'same_project_code': backend_deepcode.project_code(a) == backend_deepcode.project_code(b),
                'same_sessions_dir': backend_deepcode.sessions_dir(a) == backend_deepcode.sessions_dir(b),
                'local_session_accepted': backend.check_session(a, 'sid') is None,
                'foreign_session_accepted': backend.check_session(b, 'sid') is None,
            }
    after = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in sources}
    result['sources_after'] = after
    result['sources_unchanged_during_probe'] = sources == after
    return result



def creation_race():
    """Inject one remote change between creation validation and binding.

    gh repo create and push are simulated; target resolution/binding are real.
    """
    import contextlib
    import hub
    real_run = subprocess.run
    with tempfile.TemporaryDirectory(dir='/private/tmp', prefix='race-') as tmp:
        base = Path(tmp)
        root = base / 'paper'
        root.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=str(base / 'empty'), GIT_CONFIG_NOSYSTEM='1',
                   GIT_ALLOW_PROTOCOL='file')
        with mock.patch.dict(os.environ, env, clear=True):
            def local_git(*args):
                return real_run(['git', *args], cwd=root, capture_output=True,
                                text=True, check=True)
            local_git('init', '-q', '-b', 'main')
            for key, value in (('user.name', 'Fixture'), ('user.email', 'fixture@example.invalid'),
                               ('commit.gpgsign', 'false')):
                local_git('config', key, value)
            (root / 'main.tex').write_text('fixture')
            local_git('add', 'main.tex')
            local_git('commit', '-qm', 'fixture')
            sync = hub.project_sync(root)
            original_bind = sync.bind_target
            evidence = {}
            def fake_run(cmd, *args, **kwargs):
                if cmd[0] == '/fake/gh':
                    local_git('remote', 'add', 'origin', 'https://github.com/owner/paper.git')
                    return subprocess.CompletedProcess(cmd, 0, '', '')
                return real_run(cmd, *args, **kwargs)
            def raced_bind(expected=None):
                local_git('remote', 'set-url', 'origin', 'https://github.com/owner/tool.git')
                original_bind(expected)
            def simulated_push(**kwargs):
                evidence['would_pass_target_validation'] = sync.validate_target()
                evidence['bound_push_identity'] = sync.target['repository_identity']
                evidence['external_network_used'] = False
            with mock.patch.object(hub, 'gh_status', return_value={
                    'logged_in': True, 'gh': '/fake/gh', 'account': 'owner'}), \
                    mock.patch.object(hub, 'project_sync', return_value=sync), \
                    mock.patch.object(hub, 'protect_branch', return_value='fixture'), \
                    mock.patch.object(hub.subprocess, 'run', side_effect=fake_run), \
                    mock.patch.object(sync, 'bind_target', side_effect=raced_bind), \
                    mock.patch.object(sync, 'push', side_effect=simulated_push):
                result = hub.create_github_repo(root, 'owner', 'paper')
            evidence['reported_created_url'] = result.get('url')
            evidence['reported_error'] = result.get('error')
            return evidence


if __name__ == '__main__':
    import contextlib
    with contextlib.redirect_stdout(sys.stderr):
        result = run()
        result['creation_binding_race'] = creation_race()
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
