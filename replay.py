"""
replay.py — reconstruct what an agent knew, and diff it against now.

    python replay.py list
    python replay.py show <decision_id>
    python replay.py diff <decision_id>

This is the headline feature. "Why did the bot offer that discount?" is
answered not with the stored rationale — any database can keep a text column —
but by reading the corpus, the order and the prior decisions AS THEY WERE at
the moment of the decision, and showing what has moved since.

Three properties are deliberate:

  * NO BEDROCK IMPORT. The historical query vector is stored on the decision,
    so replay re-runs the exact query rather than re-embedding it. Replay is
    therefore deterministic, free, unaffected by a later model swap, and
    runnable from a deployed page with no AWS credentials.

  * ONE SNAPSHOT FOR THE WHOLE DIFF. The reconstruction touches four tables
    and they must agree, so it runs inside BEGIN AS OF SYSTEM TIME rather
    than issuing four independently-timestamped statements.

  * NO DIFF LOGIC IN THE UI. The web page renders exactly what diff() returns.
    That way the terminal evidence in the README and the deployed demo are
    provably the same code path.

The timestamp used is txn.read_snapshot(decision_hlc), one logical tick before
the decision committed — see txn.read_snapshot for why reading at decision_hlc
itself returns the world *after* the decision.
"""

import json
import sys

import db
import txn

# Kinds the policy gate can return. Imported lazily in counterfactual() so
# that importing replay.py never pulls in boto3.
GATE_MODULE = "agent"


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def list_decisions(limit: int = 20, conn=None) -> list[dict]:
    """The decision timeline — what there is to replay."""
    owns = conn is None
    conn = conn or db.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT d.decision_id, d.agent_id, d.order_id, d.decision_kind,
                      d.amount_minor, d.attempt, d.abort_sqlstate,
                      d.decided_at, d.decision_hlc, c.display_name
               FROM decisions d
               LEFT JOIN customers c ON c.customer_ref = d.customer_ref
               ORDER BY d.decision_hlc DESC
               LIMIT %s""", (limit,))
        cols = [x.name for x in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        if owns:
            conn.close()


def _decision_row(cur, decision_id: str) -> dict:
    cur.execute(
        """SELECT decision_id, agent_id, case_id, order_id, customer_ref,
                  decision_kind, amount_minor, rationale, chat_model,
                  query_text, recalled_case_ids, recalled_distances,
                  decision_hlc, decided_at, attempt, abort_sqlstate,
                  conflicts_with, proposed_kind, proposed_amount_minor
           FROM decisions WHERE decision_id = %s""", (decision_id,))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"no decision {decision_id}")
    return dict(zip([c.name for c in cur.description], row))


def _world(cur, decision: dict, k: int = 5) -> dict:
    """Read the order, the case and the recalled set with whatever snapshot
    the cursor is currently on. Called once inside an AS OF SYSTEM TIME
    transaction, and once at present time — same code, two worlds."""
    out: dict = {"order": None, "case": None, "recalled": [], "nearest": []}

    if decision["order_id"]:
        cur.execute(
            "SELECT order_id, item, amount_minor, refunded_minor, currency "
            "FROM orders WHERE order_id = %s", (decision["order_id"],))
        r = cur.fetchone()
        if r:
            out["order"] = dict(zip([c.name for c in cur.description], r))

    if decision["case_id"]:
        cur.execute(
            "SELECT case_id, channel, subject, body, resolution, outcome, "
            "resolved_at FROM cases WHERE case_id = %s", (decision["case_id"],))
        r = cur.fetchone()
        if r:
            out["case"] = dict(zip([c.name for c in cur.description], r))

    # The cases the agent actually recalled, as they stood.
    ids = decision["recalled_case_ids"] or []
    if ids:
        cur.execute(
            "SELECT case_id, channel, subject, resolution, outcome "
            "FROM cases WHERE case_id = ANY(%s)", (ids,))
        cols = [c.name for c in cur.description]
        by_id = {str(r[0]): dict(zip(cols, r)) for r in cur.fetchall()}
        # Preserve recall order — position is rank.
        for i, cid in enumerate(ids):
            row = by_id.get(str(cid))
            if row:
                row["rank"] = i + 1
                dists = decision["recalled_distances"] or []
                row["distance"] = dists[i] if i < len(dists) else None
                out["recalled"].append(row)

    return out


def _nearest(cur, query_vec: str, k: int = 5) -> list[dict]:
    """Re-run the agent's exact query vector at whatever snapshot the cursor
    is on. No embedding call — the vector was stored on the decision.

    The vector is passed IN rather than read from the decisions table inside
    the query. At the historical snapshot the decision row does not exist yet
    — it committed one tick later — so a subquery against it returns NULL,
    every distance becomes NULL, and the "nearest" cases come back in
    arbitrary order. That failure is quiet and plausible-looking: you get five
    real cases that simply have nothing to do with the query.
    """
    cur.execute(
        """SELECT case_id, channel, subject, resolution,
                  embedding <-> %s::VECTOR(1024) AS distance
           FROM cases
           WHERE embedding IS NOT NULL
           ORDER BY distance
           LIMIT %s""", (query_vec, k))
    cols = [c.name for c in cur.description]
    out = []
    for i, r in enumerate(cur.fetchall()):
        d = dict(zip(cols, r))
        d["rank"] = i + 1
        out.append(d)
    return out


def reconstruct(decision_id: str, conn=None) -> dict:
    """What the agent knew, and what is true now."""
    owns = conn is None
    conn = conn or db.connect()
    try:
        cur = conn.cursor()
        decision = _decision_row(cur, decision_id)
        snapshot = txn.read_snapshot(decision["decision_hlc"])

        # Read the stored query vector at PRESENT time and carry it into the
        # past. It cannot be read from inside the historical snapshot — see
        # _nearest().
        cur.execute("SELECT query_embedding::STRING FROM decisions "
                    "WHERE decision_id = %s", (decision_id,))
        query_vec = cur.fetchone()[0]

        # THEN — one consistent snapshot across every table.
        cur.execute(f"BEGIN AS OF SYSTEM TIME {snapshot}")
        try:
            then = _world(cur, decision)
            then["nearest"] = _nearest(cur, query_vec) if query_vec else []
        finally:
            cur.execute("COMMIT")

        # NOW — same code, present time.
        now = _world(cur, decision)
        now["nearest"] = _nearest(cur, query_vec) if query_vec else []

        return {"decision": decision, "snapshot_hlc": snapshot,
                "then": then, "now": now}
    finally:
        if owns:
            conn.close()


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------

def _order_changes(then: dict, now: dict) -> list[dict]:
    a, b = then.get("order"), now.get("order")
    if not a or not b:
        return []
    out = []
    for f in ("refunded_minor", "amount_minor", "item"):
        if a.get(f) != b.get(f):
            out.append({"field": f, "then": a.get(f), "now": b.get(f)})
    return out


def _case_changes(then: dict, now: dict) -> list[dict]:
    """Field-level differences on every case the agent recalled."""
    a = {str(c["case_id"]): c for c in then.get("recalled", [])}
    b = {str(c["case_id"]): c for c in now.get("recalled", [])}
    out = []
    for cid, before in a.items():
        after = b.get(cid)
        if not after:
            out.append({"case_id": cid, "subject": before.get("subject"),
                        "change": "deleted"})
            continue
        for f in ("resolution", "outcome", "subject"):
            if before.get(f) != after.get(f):
                out.append({"case_id": cid, "subject": after.get("subject"),
                            "field": f, "then": before.get(f),
                            "now": after.get(f)})
    return out


def _recall_changes(then: dict, now: dict) -> dict:
    """How the same query ranks differently against today's corpus."""
    a = {str(c["case_id"]): c for c in then.get("nearest", [])}
    b = {str(c["case_id"]): c for c in now.get("nearest", [])}
    entered = [{"case_id": k, "subject": v.get("subject"), "rank": v["rank"]}
               for k, v in b.items() if k not in a]
    left = [{"case_id": k, "subject": v.get("subject"), "rank": v["rank"]}
            for k, v in a.items() if k not in b]
    moved = [{"case_id": k, "subject": b[k].get("subject"),
              "then_rank": a[k]["rank"], "now_rank": b[k]["rank"]}
             for k in a.keys() & b.keys() if a[k]["rank"] != b[k]["rank"]]
    return {"entered": entered, "left": left, "moved": moved}


def _later_decisions(cur, decision: dict) -> list[dict]:
    if not decision["order_id"]:
        return []
    cur.execute(
        """SELECT decision_id, agent_id, decision_kind, amount_minor,
                  attempt, abort_sqlstate, decided_at
           FROM decisions
           WHERE order_id = %s AND decision_hlc > %s
           ORDER BY decision_hlc""",
        (decision["order_id"], decision["decision_hlc"]))
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def counterfactual(decision: dict, then: dict, now: dict) -> dict:
    """Would the same intent be authorised against today's state?

    The policy gate is a pure function, so this costs one function call and no
    model. It is the payoff of keeping authorisation deterministic: the
    question "what would it decide now?" is answerable exactly, not guessed.
    """
    import importlib
    agent = importlib.import_module(GATE_MODULE)

    kind = decision.get("proposed_kind") or decision["decision_kind"]
    amount = decision.get("proposed_amount_minor")
    if amount is None:
        amount = decision["amount_minor"]

    proposal = agent.Proposal(kind=kind, amount_minor=amount,
                              rationale="", model=decision.get("chat_model") or "")
    out = {"proposed": kind, "exact": decision.get("proposed_kind") is not None}
    for label, world in (("then", then), ("now", now)):
        order = world.get("order")
        out[label] = agent.policy_gate(proposal, order).kind if order else None
    out["flipped"] = out["then"] != out["now"]
    return out


def diff(decision_id: str, conn=None) -> dict:
    """The frozen contract the UI renders. Do not change shape casually —
    the web page and the README evidence both read this."""
    owns = conn is None
    conn = conn or db.connect()
    try:
        rec = reconstruct(decision_id, conn=conn)
        cur = conn.cursor()
        rec["diff"] = {
            "order_changes": _order_changes(rec["then"], rec["now"]),
            "case_changes": _case_changes(rec["then"], rec["now"]),
            "recall_changes": _recall_changes(rec["then"], rec["now"]),
            "later_decisions": _later_decisions(cur, rec["decision"]),
        }
        rec["counterfactual"] = counterfactual(
            rec["decision"], rec["then"], rec["now"])
        return rec
    finally:
        if owns:
            conn.close()


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def _money(p):
    return f"£{(p or 0) / 100:.2f}"


def _print_diff(r: dict) -> None:
    d, then, now, df = r["decision"], r["then"], r["now"], r["diff"]
    print(f"decision   : {d['decision_id']}")
    print(f"agent      : {d['agent_id']}   {d['decided_at']}")
    print(f"decided    : {d['decision_kind']}  {_money(d['amount_minor'])}")
    if d["abort_sqlstate"]:
        print(f"             (attempt {d['attempt']}, after "
              f"SQLSTATE {d['abort_sqlstate']})")
    print(f"rationale  : {d['rationale']}")
    print(f"snapshot   : {r['snapshot_hlc']}")
    print("=" * 74)

    print("WHAT IT SAW")
    if then.get("order"):
        o = then["order"]
        print(f"  order {o['order_id']}: {o['item']}, {_money(o['amount_minor'])}, "
              f"already refunded {_money(o['refunded_minor'])}")
    for c in then.get("recalled", []):
        print(f"  #{c['rank']} [{c['channel']:<8}] {(c['subject'] or '')[:52]}")
        print(f"      -> {(c['resolution'] or '(still open)')[:76]}")

    print("-" * 74)
    print("WHAT CHANGED SINCE")
    empty = True
    for ch in df["order_changes"]:
        empty = False
        v = (lambda x: _money(x)) if "minor" in ch["field"] else (lambda x: x)
        print(f"  order.{ch['field']}: {v(ch['then'])} -> {v(ch['now'])}")
    for ch in df["case_changes"]:
        empty = False
        if ch.get("change") == "deleted":
            print(f"  case deleted: {ch['subject']}")
        else:
            print(f"  case \"{(ch['subject'] or '')[:44]}\" {ch['field']}:")
            print(f"      then: {str(ch['then'])[:66]}")
            print(f"      now : {str(ch['now'])[:66]}")
    rc = df["recall_changes"]
    for e in rc["entered"]:
        empty = False
        print(f"  entered top-5 at #{e['rank']}: {(e['subject'] or '')[:48]}")
    for e in rc["left"]:
        empty = False
        print(f"  left top-5 (was #{e['rank']}): {(e['subject'] or '')[:48]}")
    for e in rc["moved"]:
        empty = False
        print(f"  moved #{e['then_rank']} -> #{e['now_rank']}: "
              f"{(e['subject'] or '')[:44]}")
    for l in df["later_decisions"]:
        empty = False
        print(f"  later: {l['agent_id']} {l['decision_kind']} "
              f"{_money(l['amount_minor'])}"
              + (f" (after {l['abort_sqlstate']})" if l["abort_sqlstate"] else ""))
    if empty:
        print("  (nothing has changed since this decision)")

    print("-" * 74)
    cf = r["counterfactual"]
    print(f"COUNTERFACTUAL — same intent ({cf['proposed']}), judged again")
    print(f"  against the world it saw : {cf['then']}")
    print(f"  against the world today  : {cf['now']}")
    if cf["flipped"]:
        print("  THE DECISION WOULD FLIP. Same agent, same intent, different "
              "memory.")
    if not cf["exact"]:
        print("  (proposal not recorded on this decision; intent inferred "
              "from the outcome)")


def main(argv):
    db.safe_console()
    if not argv:
        print(__doc__.strip())
        return
    cmd = argv[0]

    if cmd == "list":
        rows = list_decisions()
        if not rows:
            print("no decisions recorded yet — run race.py or agent.py")
            return
        for d in rows:
            flag = f"  <- retried after {d['abort_sqlstate']}" if d["abort_sqlstate"] else ""
            print(f"{str(d['decision_id'])[:8]}  {d['decided_at']:%Y-%m-%d %H:%M}  "
                  f"{d['agent_id']:<16} {d['order_id'] or '-':<10} "
                  f"{d['decision_kind']:<26} {_money(d['amount_minor'])}{flag}")
        return

    if len(argv) < 2:
        sys.exit(f"{cmd} needs a decision_id")
    target = argv[1]

    # Accept a short prefix, since nobody types a UUID.
    with db.connect() as c:
        cur = c.cursor()
        cur.execute("SELECT decision_id FROM decisions "
                    "WHERE decision_id::STRING LIKE %s", (target + "%",))
        rows = cur.fetchall()
    if not rows:
        sys.exit(f"no decision matching {target!r}")
    if len(rows) > 1:
        sys.exit(f"{target!r} matches {len(rows)} decisions — be more specific")
    decision_id = str(rows[0][0])

    if cmd == "show":
        print(json.dumps(reconstruct(decision_id), indent=2, default=str))
    elif cmd == "diff":
        _print_diff(diff(decision_id))
    elif cmd == "json":
        print(json.dumps(diff(decision_id), indent=2, default=str))
    else:
        sys.exit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
