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

# Captured at import, before anything can overwrite it.
#
# use_reader() points COCKROACH_URL at the read-only credential, and a warm
# serverless container reuses the process — so that mutation survives into the
# NEXT request. The write path then read the reader URL as its "writer" and was
# refused by the database. Holding the original here means the two credentials
# cannot leak into each other however many requests a container serves.
ADMIN_URL = os.environ.get("COCKROACH_URL")


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


def env_report() -> dict:
    """Which configuration the runtime can actually see — names and lengths
    only, never values.

    Worth having: `read_only: false` alone cannot distinguish "the variable is
    missing" from "the variable is set but something else went wrong", and on
    a hosted platform you cannot just look at the environment.
    """
    interesting = ["COCKROACH_URL", "COCKROACH_READER_URL", "COCKROACH_CA",
                   "AWS_REGION", "S3_BUCKET", "BEDROCK_CHAT_MODEL",
                   "BEDROCK_EMBED_MODEL"]
    return {k: (f"set ({len(os.environ[k])} chars)" if os.environ.get(k)
                else "not set") for k in interesting}


def route_health() -> dict:
    import db
    body: dict = {"ok": False}
    body["env"] = env_report()
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

        body["ops"] = ops_report(cur)
    body["ok"] = True
    return body


def ops_report(cur) -> dict:
    """What an operator would actually want to know about this system.

    Deliberately not a uptime ping. Recall has two failure modes that return
    HTTP 200 while the product is broken:

      * the vector index exists but the planner ignores it, and recall silently
        degrades to a full scan that still returns rows;
      * the configured MVCC retention looks right while replay is already
        unreachable, because the binding constraint is the system descriptor
        ranges rather than the tables.

    Both are checked here against the live cluster rather than inferred from
    configuration, because configuration is exactly what lies in both cases.
    All of it is read-only, so the public reader can serve it.
    """
    out: dict = {}

    t = time.time()
    cur.execute("SELECT 1")
    out["probe_ms"] = round((time.time() - t) * 1000, 1)

    # Does the planner USE the index, or merely have one? Borrow a real stored
    # query vector rather than synthesising one, so the plan is the plan a
    # genuine recall produces.
    try:
        cur.execute("""SELECT query_embedding::STRING FROM decisions
                       WHERE query_embedding IS NOT NULL
                       ORDER BY decision_hlc DESC LIMIT 1""")
        row = cur.fetchone()
        if row:
            t = time.time()
            cur.execute("EXPLAIN SELECT case_id FROM cases "
                        "ORDER BY embedding <-> %s LIMIT 5", (row[0],))
            plan = "\n".join(r[0] for r in cur.fetchall())
            out["explain_ms"] = round((time.time() - t) * 1000, 1)
            out["vector_index_used"] = ("vector search" in plan
                                        and "cases_embedding_idx" in plan)
            t = time.time()
            cur.execute("SELECT case_id FROM cases "
                        "ORDER BY embedding <-> %s LIMIT 5", (row[0],))
            cur.fetchall()
            out["recall_ms"] = round((time.time() - t) * 1000, 1)
    except Exception as e:  # noqa: BLE001
        out["vector_index_used"] = None
        out["vector_probe_error"] = f"{type(e).__name__}"

    # Can replay actually reach the newest decision right now? This is the
    # headline feature answering for itself.
    try:
        cur.execute("SELECT decision_hlc FROM decisions "
                    "ORDER BY decision_hlc DESC LIMIT 1")
        row = cur.fetchone()
        if row:
            # Interpolated because AS OF SYSTEM TIME rejects a placeholder, so
            # it goes through the same validator every other replay path uses.
            import txn
            hlc = txn.validate_hlc(row[0])
            t = time.time()
            cur.execute(f"SELECT count(*) FROM decisions "  # noqa: S608
                        f"AS OF SYSTEM TIME '{hlc}'")
            cur.fetchone()
            out["replay_probe_ms"] = round((time.time() - t) * 1000, 1)
            out["replay_reachable"] = True
    except Exception as e:  # noqa: BLE001
        out["replay_reachable"] = False
        out["replay_probe_error"] = str(e).splitlines()[0][:160]

    # How often serializable actually bites. A zero here on a busy cluster
    # would mean the race demo is not exercising what it claims to.
    cur.execute("SELECT count(*) FROM decisions WHERE abort_sqlstate IS NOT NULL")
    out["serialization_retries"] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM actions")
    out["actions"] = cur.fetchone()[0]
    return out


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
    try:
        return 200, {"ok": True, **replay.diff(str(matches[0][0]))}
    except replay.OutsideReplayWindow as e:
        # Not a failure of the code — the history genuinely no longer exists.
        # Say so precisely rather than surfacing a descriptor error.
        return 200, {"ok": False, "outside_replay_window": True,
                     "error": str(e)}


def route_run(mode: str = "single") -> tuple[int, dict]:
    """Run one agent turn live, on a throwaway order.

    `mode="race"` instead runs two agents at the same throwaway order, which is
    what rebuilds the hero once the seeded race has aged out of the replay
    window. Same credential, same guarantee that ORD-4502 is never touched.

    Writes, so it needs a write credential — COCKROACH_WRITER_URL if one is
    configured, otherwise COCKROACH_URL. Reads elsewhere still go through the
    reader; only this route escalates, and only to write rows it created.

    It never touches the hero order: each run mints its own SANDBOX-xxxxxxxx.
    A judge in September must not be able to exhaust the refund on ORD-4502
    and stop the recorded demo reproducing.
    """
    # ADMIN_URL, not os.getenv("COCKROACH_URL") — by the time a POST arrives on
    # a warm container, a previous GET may have repointed that at the reader.
    writer = os.getenv("COCKROACH_WRITER_URL") or ADMIN_URL
    if not writer:
        return 503, {"ok": False, "error": "no write credential configured"}
    prev = os.environ.get("COCKROACH_URL")
    os.environ["COCKROACH_URL"] = writer
    try:
        import sandbox
        work = sandbox.run_race if mode == "race" else sandbox.run
        return 200, {"ok": True, "mode": mode, **work()}
    finally:
        # Restore whatever the previous request left, so a following read still
        # goes through the reader.
        if prev is not None:
            os.environ["COCKROACH_URL"] = prev


class handler(BaseHTTPRequestHandler):
    def _send(self, status, body, started):
        body["ms"] = int((time.time() - started) * 1000)
        payload = json.dumps(body, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        started = time.time()
        route = (parse_qs(urlparse(self.path).query).get("route") or [""])[0] \
            or urlparse(self.path).path.rstrip("/").rsplit("/", 1)[-1]
        try:
            if route == "run":
                mode = (parse_qs(urlparse(self.path).query).get("mode")
                        or ["single"])[0]
                status, body = route_run("race" if mode == "race" else "single")
            else:
                status, body = 404, {"ok": False, "error": f"no route {route!r}"}
        except Exception as e:  # noqa: BLE001
            status, body = 500, {"ok": False,
                                 "error": f"{type(e).__name__}: "
                                          f"{str(e).splitlines()[0][:300]}"}
        self._send(status, body, started)

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
