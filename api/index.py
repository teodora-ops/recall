"""
Vercel serverless entrypoint — one handler, three routes.

    /api/health              deployment + cluster health
    /api/replay              the decision timeline
    /api/replay?id=<prefix>  the full diff for one decision

Vercel's Python runtime takes a single entrypoint, so routing happens here
rather than through one file per endpoint.

The replay routes return replay.diff() VERBATIM. There is deliberately no diff
logic in the web layer: the page renders exactly what the CLI prints, so the
Evidence in the README and the deployed demo are provably the same code path.

Reads go through recall_reader when COCKROACH_READER_URL is set. A public URL
should not carry a credential that can rewrite the history it reports on.
"""

import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def use_reader() -> bool:
    """Point db.py at the read-only credential if one is configured.

    Falls back to COCKROACH_URL so local development still works with only
    the admin URL set — but the deployed environment should always have the
    reader, and /api/health reports which one is in use so it is visible.
    """
    reader = os.getenv("COCKROACH_READER_URL")
    if reader:
        os.environ["COCKROACH_URL"] = reader
    return bool(reader)


def route_health() -> dict:
    import db
    body: dict = {"ok": False}
    body["ca_resolved"] = db.ca_path()
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT version()")
        body["cluster"] = cur.fetchone()[0].split(" (")[0]
        cur.execute("SELECT current_user")
        body["sql_user"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM cases")
        body["cases"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM decisions")
        body["decisions"] = cur.fetchone()[0]

        # The two things that fail silently rather than loudly.
        cur.execute("SHOW INDEXES FROM cases")
        body["vector_index"] = "cases_embedding_idx" in {r[1] for r in cur.fetchall()}
        cur.execute("SHOW ZONE CONFIGURATION FROM TABLE cases")
        m = re.search(r"gc\.ttlseconds = (\d+)", cur.fetchall()[0][1])
        secs = int(m.group(1)) if m else None
        body["retention_days"] = round(secs / 86400, 1) if secs else None
        body["replay_ok"] = bool(secs and secs >= 7776000)
    body["ok"] = True
    return body


def route_replay(qs: dict) -> tuple[int, dict]:
    import db
    import replay

    wanted = (qs.get("id") or [None])[0]
    if not wanted:
        return 200, {"ok": True, "decisions": replay.list_decisions(limit=25)}

    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT decision_id FROM decisions "
                    "WHERE decision_id::STRING LIKE %s", (wanted + "%",))
        matches = cur.fetchall()
    if len(matches) != 1:
        return 404, {"ok": False,
                     "error": f"{len(matches)} decisions match {wanted!r}"}
    return 200, {"ok": True, **replay.diff(str(matches[0][0]))}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        started = time.time()
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        # The rewrite in vercel.json sends /api/<name> to /api/index, so the
        # function never sees the path the caller asked for — it sees the
        # destination. The original name is passed through as ?route= instead;
        # the path is only a fallback for direct/local invocation.
        route = (qs.get("route") or [None])[0]
        if not route:
            route = parsed.path.rstrip("/").rsplit("/", 1)[-1] or "index"

        status, body = 200, {}
        try:
            read_only = use_reader()
            if route == "health":
                body = route_health()
            elif route == "replay":
                status, body = route_replay(qs)
            else:
                status, body = 404, {"ok": False,
                                     "error": f"no route {route!r}"}
            body["read_only"] = read_only
        except Exception as e:  # noqa: BLE001
            status = 500
            body = {"ok": False,
                    "error": f"{type(e).__name__}: "
                             f"{str(e).splitlines()[0][:300]}"}

        body["ms"] = int((time.time() - started) * 1000)
        payload = json.dumps(body, indent=2, default=str).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)
