"""
handler.py — the agent turn, running on AWS Lambda.

This is the piece the brief asks to be deployed on AWS. One agent turn —
recall, the policy gate, and a decision plus its action written in a single
serializable transaction — executes here, behind a Lambda Function URL. The
Vercel page is the front end; the agentic work happens in Lambda.

Deliberately the same code as everywhere else: it imports sandbox.py, which
imports agent.py, which is what race.py and the CLI drive. There is no
Lambda-specific fork of the decision logic, so what runs in production is what
the evidence in the README was produced with.

Environment it needs (set on the function, not baked in):
    COCKROACH_URL   a credential that can write   (required)

It does NOT need AWS credentials: Lambda supplies its own role, and this path
makes no Bedrock call — the query vector is read from a previous decision and
the proposal is fixed. See sandbox.py.
"""

import json
import os
import sys
import time

# The zip is flat: modules and certs/ sit next to this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CORS = {
    # The Vercel page calls this cross-origin.
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
}


def _reply(status: int, body: dict, started: float):
    body["ms"] = int((time.time() - started) * 1000)
    body["runtime"] = "aws-lambda"
    body["region"] = os.getenv("AWS_REGION", "unknown")
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json",
                    "Cache-Control": "no-store", **CORS},
        "body": json.dumps(body, indent=2, default=str),
    }


def lambda_handler(event, context):
    started = time.time()

    method = (event.get("requestContext", {})
                   .get("http", {})
                   .get("method", "POST")).upper()
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": ""}

    if method == "GET":
        # A cheap liveness probe that proves the function can reach the
        # cluster, without writing anything.
        try:
            import db
            with db.connect() as conn:
                cur = conn.cursor()
                cur.execute("SELECT version()")
                version = cur.fetchone()[0].split(" (")[0]
                cur.execute("SELECT current_user")
                user = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM cases")
                cases = cur.fetchone()[0]
            return _reply(200, {"ok": True, "cluster": version,
                                "sql_user": user, "cases": cases}, started)
        except Exception as e:  # noqa: BLE001
            return _reply(500, {"ok": False,
                                "error": f"{type(e).__name__}: "
                                         f"{str(e).splitlines()[0][:300]}"},
                          started)

    # mode=race runs two agents at one throwaway order instead of one agent at
    # its own. The page asks for it when the seeded race has aged out of the
    # replay window and the hero would otherwise have nothing to show.
    mode = "single"
    try:
        raw = event.get("body") or ""
        if event.get("isBase64Encoded") and raw:
            import base64
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        if raw.strip():
            mode = (json.loads(raw).get("mode") or "single")
    except Exception:  # noqa: BLE001
        # A malformed body is not a reason to fail the request; the default
        # mode is the safe one.
        mode = "single"

    try:
        import sandbox
        work = sandbox.run_race if mode == "race" else sandbox.run
        return _reply(200, {"ok": True, "mode": mode, **work()}, started)
    except Exception as e:  # noqa: BLE001
        return _reply(500, {"ok": False,
                            "error": f"{type(e).__name__}: "
                                     f"{str(e).splitlines()[0][:300]}"},
                      started)
