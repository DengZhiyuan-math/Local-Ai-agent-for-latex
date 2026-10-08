"""Opt-in native CLI acceptance with a temporary project. Codex calls the signed-in model; compilation uses a local fixture."""
import json, sys, tempfile, threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'prism_local'))
sys.path.insert(0, str(REPO / 'tests'))
from backend_codex import Codex
from test_agent import manager, wait_done

class H(BaseHTTPRequestHandler):
    calls = []
    def log_message(self, *a): pass
    def do_POST(self):
        self.calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        data = json.dumps({'exit': 1, 'diagnostics': [{'severity': 'error', 'file': 'main.tex',
                  'line': 1, 'message': 'Acceptance fixture compile error'}]}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(data)

http = ThreadingHTTPServer(('127.0.0.1', 0), H)
threading.Thread(target=http.serve_forever, daemon=True).start()
try:
    with tempfile.TemporaryDirectory(prefix='prism-codex-live-') as tmp:
        root = Path(tmp).resolve()
        for name in ('main.tex', 'other.tex', 'figure.png'): (root/name).write_text('original\n')
        backend = Codex('codex')
        m = manager(root, lambda:['main.tex','other.tex'], codex=backend)
        m.server_url = f'http://127.0.0.1:{http.server_port}/'
        sid = None
        turns = [
          ('edit', ['main.tex'], 'Read main.tex. Use apply_patch to replace original with scoped. Then call prism compile once. The compile error is a synthetic test fixture: report it and stop; do not fix it.'),
          ('ask', None, 'Read main.tex and report its exact content. The current Ask mode must not modify any file or compile.'),
          ('edit', None, 'Read other.tex. Use apply_patch to replace original with unscoped. No compile is needed for this fixture.'),
          ('edit', ['main.tex'], 'Read main.tex. Use apply_patch to replace scoped with scoped-again. No compile is needed for this fixture.'),
        ]
        results = []
        for mode, scope, prompt in turns:
            r=m.start(prompt, sid, mode, effort='low', scope=scope)
            if 'error' in r: raise RuntimeError(r['error'])
            job=m.jobs[r['job']]
            done=wait_done(job, timeout=150)
            sid=done['session_id']
            record={'mode':mode,'scope':scope,'done':done,'events':job.events}
            results.append(record)
            print(json.dumps(record, ensure_ascii=False),flush=True)
            assert done['exit']==0 and not done.get('is_error'), done
        assert (root/'main.tex').read_text()=='scoped-again\n'
        assert (root/'other.tex').read_text()=='unscoped\n'
        assert (root/'figure.png').read_text()=='original\n'
        assert not results[1]['done']['changed']
        assert len(H.calls)==1, H.calls
        assert any(e.get('error') for e in results[0]['events'] if e['t']=='tool_result')
        print('CODEX_ACCEPTANCE_PASSED',flush=True)
finally: http.shutdown(); http.server_close()
