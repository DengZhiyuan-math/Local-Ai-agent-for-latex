"""A slow OpenAI-compatible chat API: streams one word every half second for 20 seconds.

    python slow_api.py <port> [<answer file>]

A search with the agent ([prism-search] in the request) is answered at once with the text
of the answer file, when there is one: the browser checks write there what the agent says."""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ANSWER = Path(sys.argv[2]) if len(sys.argv) > 2 else None


def chunk(delta, finish=None):
    return {"id": "x", "object": "chat.completion.chunk", "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if b"[prism-search]" in body and ANSWER and ANSWER.exists():
            words, pause = [ANSWER.read_text(encoding="utf-8")], 0.1
        else:
            words, pause = [f"word{i} " for i in range(40)], 0.5
        try:
            for w in words:
                self.wfile.write(f"data: {json.dumps(chunk({'content': w}))}\n\n".encode())
                self.wfile.flush()
                time.sleep(pause)
            self.wfile.write(f"data: {json.dumps(chunk({}, 'stop'))}\n\ndata: [DONE]\n\n".encode())
        except OSError:
            pass


srv = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H)
srv.serve_forever()
