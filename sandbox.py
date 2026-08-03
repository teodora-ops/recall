"""
sandbox.py — the live "run a new case" path behind the deployed UI.

A judge clicks a button; an agent decides a fresh case in a real serializable
transaction, and the decision is replayable seconds later. That is the demo
proving itself rather than showing a recording.

Two constraints shape this, and both are deliberate:

  * IT NEVER TOUCHES THE HERO ORDER. Every run mints its own SANDBOX-xxxxxxxx
    order. If it wrote to ORD-4502 a judge in September could exhaust the
    refund and the recorded demo would stop reproducing.

  * IT MAKES NO BEDROCK CALL. The query vector is copied from an existing
    decision and the proposal is fixed, so the public endpoint needs no AWS
    credentials and costs nothing per click. What it does NOT fake is the part
    that matters: the transaction, the policy gate, the decision row and the
    HLC anchor are all real, which is why the result is genuinely replayable.

The rationale text is pre-written rather than model-generated. That is the one
thing given up, and the UI says so rather than implying a model ran.
"""

import random
import secrets

import agent
import db

SANDBOX_CUSTOMER = "SANDBOX-DEMO"

SCENARIOS = [
    dict(item="Set of four stoneware mugs", amount=3498,
         message="I've been charged twice for my order — two payments of "
                 "£34.98 left my account for one set of mugs.",
         kind="refund_full",
         rationale="Duplicate charge confirmed against past cases resolved "
                   "the same way; refunding the second payment in full."),
    dict(item="Wool throw, oatmeal", amount=8500,
         message="The throw arrived with a snag in the weave near one corner. "
                 "I'd rather not post it back if I can avoid it.",
         kind="refund_partial", partial=0.20,
         rationale="Consistent with prior goodwill discounts on minor damage: "
                   "20% back, customer keeps the item, no return needed."),
    dict(item="Stoneware teapot, 1.2L", amount=4200,
         message="Teapot lid arrived chipped. Box was undamaged so I think it "
                 "was packed that way.",
         kind="refund_partial", partial=0.20,
         rationale="Matches earlier chipped-lid cases resolved with a partial "
                   "refund rather than a return and replacement."),
]


def ensure_customer(cur) -> None:
    cur.execute(
        "INSERT INTO customers (customer_ref, display_name, email) "
        "VALUES (%s, 'Sandbox Demo', 'sandbox@example.com') "
        "ON CONFLICT (customer_ref) DO NOTHING", (SANDBOX_CUSTOMER,))


def borrow_query_vector(cur) -> tuple[str | None, list[str], list[float]]:
    """Reuse a stored query vector and its recalled set.

    Not a shortcut for its own sake: it means the live path embeds nothing, so
    the deployed endpoint needs no Bedrock access. The vector is a real one
    produced by Titan for a real duplicate-charge query.
    """
    cur.execute(
        """SELECT query_embedding::STRING, recalled_case_ids, recalled_distances
           FROM decisions
           WHERE query_embedding IS NOT NULL AND decision_kind = 'refund_full'
           ORDER BY decision_hlc DESC LIMIT 1""")
    row = cur.fetchone()
    if not row:
        return None, [], []
    return row[0], [str(x) for x in (row[1] or [])], list(row[2] or [])


def run(scenario_index: int | None = None) -> dict:
    """Mint a sandbox order and decide it for real. Returns the decision."""
    rnd = random.Random(secrets.randbits(64))
    sc = SCENARIOS[scenario_index if scenario_index is not None
                   else rnd.randrange(len(SCENARIOS))]
    order_id = f"SANDBOX-{secrets.token_hex(4)}"

    with db.connect() as conn:
        cur = conn.cursor()
        ensure_customer(cur)
        cur.execute(
            "INSERT INTO orders (order_id, customer_ref, item, amount_minor) "
            "VALUES (%s,%s,%s,%s)",
            (order_id, SANDBOX_CUSTOMER, sc["item"], sc["amount"]))
        qvec, recalled_ids, dists = borrow_query_vector(cur)

        # Rehydrate the recalled cases so the decision records what it "saw".
        recalled: list[dict] = []
        if recalled_ids:
            cur.execute(
                "SELECT case_id, channel, subject, resolution, outcome "
                "FROM cases WHERE case_id = ANY(%s)", (recalled_ids,))
            cols = [c.name for c in cur.description]
            by_id = {str(r[0]): dict(zip(cols, r)) for r in cur.fetchall()}
            for i, cid in enumerate(recalled_ids):
                r = by_id.get(cid)
                if r:
                    r["distance"] = dists[i] if i < len(dists) else 0.0
                    recalled.append(r)

    amount = (sc["amount"] if sc["kind"] == "refund_full"
              else int(sc["amount"] * sc.get("partial", 0.2)))
    proposal = agent.Proposal(kind=sc["kind"], amount_minor=amount,
                              rationale=sc["rationale"],
                              model="pre-written (no model call)")

    out = agent.handle(sc["message"], order_id, "agent-webchat",
                       proposal=proposal, query_vec=qvec, recalled=recalled)

    r = out.result
    return {
        "order_id": order_id,
        "item": sc["item"],
        "amount_minor": sc["amount"],
        "message": sc["message"],
        "decision_id": r["decision_id"],
        "decision_kind": r["kind"],
        "decision_amount_minor": r["amount_minor"],
        "rationale": r["rationale"],
        "attempts": out.attempts,
        "isolation": out.isolation,
        "recalled": len(r["recalled"]),
        "model_called": False,
    }


if __name__ == "__main__":
    db.safe_console()
    d = run()
    print(f"order    : {d['order_id']} — {d['item']} "
          f"£{d['amount_minor'] / 100:.2f}")
    print(f"decision : {d['decision_kind']} "
          f"£{d['decision_amount_minor'] / 100:.2f}")
    print(f"txn      : {d['attempts']} attempt(s), {d['isolation']}")
    print(f"recalled : {d['recalled']} case(s), model called: {d['model_called']}")
    print(f"replay   : python replay.py diff {d['decision_id'][:8]}")
