"""
Vercel serverless function: /api/health

Deliberately the smallest thing that proves the whole connection path — env
vars reach the runtime, the repo-shipped CA is found on Linux, TLS verifies
against CockroachDB Cloud, and the region is reachable. If this returns a row
count, the deployment path is real and the replay UI has somewhere to land.

Kept read-only. Nothing here writes.
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler

# The repo root is one level up from web/; import db.py rather than
# duplicating the connection logic in the serverless runtime.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        started = time.time()
        body = {"ok": False}
        try:
            import db  # noqa: PLC0415 — deferred so import errors surface as JSON

            body["ca_resolved"] = db.ca_path()
            with db.connect() as conn:
                cur = conn.cursor()
                cur.execute("SELECT version()")
                body["cluster"] = cur.fetchone()[0].split(" (")[0]
                cur.execute("SELECT count(*) FROM cases")
                body["cases"] = cur.fetchone()[0]
                # Proves the headline mechanism is reachable from the
                # deployed environment, not just from a laptop.
                cur.execute("SELECT cluster_logical_timestamp()")
                body["hlc"] = str(cur.fetchone()[0])
            body["ok"] = True
        except Exception as e:  # noqa: BLE001
            body["error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"

        body["ms"] = int((time.time() - started) * 1000)
        payload = json.dumps(body, indent=2).encode()

        self.send_response(200 if body["ok"] else 500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)
