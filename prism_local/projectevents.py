"""Home's server-sent invalidations. Waiting and keepalives do no filesystem I/O."""
from __future__ import annotations

import diskprojects


def serve(handler):
    events = diskprojects.notifications()
    version = events.stamp()
    handler.send_response(200)
    handler.send_header('Content-Type', 'text/event-stream')
    handler.send_header('Cache-Control', 'no-store')
    handler.end_headers()
    handler.close_connection = True
    try:
        # Every connection refreshes once, including reconnections after missed events.
        handler.wfile.write(b'retry: 1000\nevent: changed\ndata: ready\n\n')
        handler.wfile.flush()
        while True:
            changed = events.wait(version)
            handler.wfile.write(b'event: changed\ndata: invalidate\n\n' if changed != version else b': keepalive\n\n')
            handler.wfile.flush()
            version = changed
    except (OSError, ValueError):
        return
