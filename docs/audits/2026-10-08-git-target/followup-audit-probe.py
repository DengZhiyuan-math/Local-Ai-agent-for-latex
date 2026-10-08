"""Offline native-format collision diagnostic; no CLI/model/network."""
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'prism_local'))
import backend_deepcode
import sessionmeta

with tempfile.TemporaryDirectory(prefix='dc-', dir='/private/tmp') as tmp:
    base = Path(tmp)
    a, b = base / 'a-b/c', base / 'a/b-c'
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    with mock.patch.object(Path, 'home', return_value=base / 'home'):
        directory = backend_deepcode.sessions_dir(a)
        directory.mkdir(parents=True)
        index = directory / 'sessions-index.json'
        index.write_text(json.dumps({'originalPath': str(a), 'entries': [{'id': 'sid-a'}]}))
        session = directory / 'sid-a.jsonl'
        session.write_text(json.dumps({'id': 'native-message-id', 'sessionId': 'sid-a', 'role': 'system',
                                      'content': '# Local Workspace Environment\n\n```json\n' +
                                      json.dumps({'root path': str(a), 'pwd': str(a)}) + '\n```'}) + '\n')
        backend = backend_deepcode.DeepCode('deepcode')
        result = {'same_directory': directory == backend_deepcode.sessions_dir(b),
                  'a_before_b_accepted': backend.check_session(a, 'sid-a') is None}
        # Proven native behavior when B saves a fresh turn: originalPath overwritten,
        # A's session remains in the same directory/index.
        index.write_text(json.dumps({'originalPath': str(b), 'entries': [{'id': 'sid-a'}]}))
        result.update({'session_metadata_still_matches_a': sessionmeta.matches(session, a, 'sid-a'),
                       'a_after_b_rejected': backend.check_session(a, 'sid-a') is not None,
                       'b_foreign_a_rejected': backend.check_session(b, 'sid-a') is not None})
    Path(__file__).with_suffix('.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
