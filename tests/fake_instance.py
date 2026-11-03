"""A small real server for instance startup/identity tests (no git, TeX or AI)."""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import instances
import httpbase


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("project", nargs="?", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--port-tries", type=int, default=1)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--exit-when-idle", action="store_true")
    parser.add_argument("--ready-file")
    parser.add_argument("--instance-token")
    parser.add_argument("--fixture-exit", action="store_true")
    parser.add_argument("--fixture-redirect", action="store_true")
    parser.add_argument("--fixture-hang", action="store_true")
    args, _ = parser.parse_known_args()
    if args.fixture_exit:
        return 2
    if args.fixture_hang:
        print(json.dumps({"hung_pid": os.getpid(), "started": instances.process_started(os.getpid())}), flush=True)
        time.sleep(60)
        return 0
    if args.fixture_redirect:
        argv = [arg for arg in sys.argv if arg != "--fixture-redirect"]
        with subprocess.Popen([sys.executable, *argv]) as child:
            return child.wait()
    with instances.serving(args.project, args.ready_file, args.instance_token) as life:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                info = {"app": "prism-local" if args.project else "prism-home",
                        "root": str(args.project.resolve()) if args.project else None,
                        "pid": os.getpid(), "token": life.token, "pages": 1}
                data = json.dumps(info).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        with httpbase.Server(("127.0.0.1", args.port), Handler) as server:
            life.publish(f"http://127.0.0.1:{server.server_port}/", server.server_port)
            server.serve_forever(poll_interval=0.05)
    return 0


if __name__ == "__main__":
    sys.exit(main())
