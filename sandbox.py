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
import threading
from decimal import Decimal

import agent
import db

# Barrier waits inside a Lambda invocation, not a developer's terminal. Long
# enough that a slow round trip to London does not fake a deadlock, short
# enough that a genuinely stuck worker fails inside the function timeout.
BARRIER_TIMEOUT = 20

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


def rehydrate(cur, recalled_ids: list[str], dists: list[float]) -> list[dict]:
    """Turn borrowed case ids back into the rows the decision records seeing."""
    recalled: list[dict] = []
    if not recalled_ids:
        return recalled
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
    return recalled


def mint_order(cur, sc: dict) -> str:
    """A throwaway order of its own, never the hero's."""
    order_id = f"SANDBOX-{secrets.token_hex(4)}"
    ensure_customer(cur)
    cur.execute(
        "INSERT INTO orders (order_id, customer_ref, item, amount_minor) "
        "VALUES (%s,%s,%s,%s)",
        (order_id, SANDBOX_CUSTOMER, sc["item"], sc["amount"]))
    return order_id


def run(scenario_index: int | None = None) -> dict:
    """Mint a sandbox order and decide it for real. Returns the decision."""
    rnd = random.Random(secrets.randbits(64))
    sc = SCENARIOS[scenario_index if scenario_index is not None
                   else rnd.randrange(len(SCENARIOS))]

    with db.connect() as conn:
        cur = conn.cursor()
        order_id = mint_order(cur, sc)
        qvec, recalled_ids, dists = borrow_query_vector(cur)
        recalled = rehydrate(cur, recalled_ids, dists)

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


def run_race() -> dict:
    """Two agents, one throwaway order, raced for real.

    This exists because the recorded race ages out. `AS OF SYSTEM TIME` can
    only reach back as far as the cluster still retains, and on Cloud Basic the
    binding constraint is the system descriptor ranges, which a tenant cannot
    raise. Weeks into judging the seeded race is unreplayable and the hero —
    the two-card contrast the whole argument rests on — has nothing to show.

    `run()` cannot fill that gap: it produces one decision on one order, and
    the hero needs an order carrying two decisions with a serialization
    failure between them. So this races the same way race.py does, with the
    same barrier forcing both agents to read before either writes.

    What is real here is everything that matters: two connections, two
    serializable transactions, a genuine 40001, and a loser that re-reads and
    flips to a decline because `policy_gate` is pure and re-evaluated against
    the freshly read order. What is pre-written is the rationale text, exactly
    as in `run()`, so the endpoint needs no Bedrock credentials.
    """
    sc = SCENARIOS[0]  # the duplicate charge — the story the hero tells

    with db.connect() as conn:
        cur = conn.cursor()
        order_id = mint_order(cur, sc)
        qvec, recalled_ids, dists = borrow_query_vector(cur)
        recalled = rehydrate(cur, recalled_ids, dists)

    # Both agents propose the same full refund. Only the order state they each
    # read inside the transaction decides what that proposal is authorised as.
    proposal = agent.Proposal(kind="refund_full", amount_minor=sc["amount"],
                              rationale=sc["rationale"],
                              model="pre-written (no model call)")

    both_read = threading.Barrier(2)
    first_committed = threading.Event()
    results: dict[str, object] = {}
    errors: dict[str, str] = {}

    def go(name: str, agent_id: str, goes_first: bool) -> None:
        def hook(phase, attempt):
            # Retries must run unimpeded — holding one at a barrier the other
            # worker has already cleared would deadlock the invocation.
            if attempt > 1:
                return
            if phase == "after_read":
                both_read.wait(timeout=BARRIER_TIMEOUT)
                if not goes_first:
                    # Let the winner COMMIT before the loser writes, so there
                    # is one conflict to explain rather than two.
                    first_committed.wait(timeout=BARRIER_TIMEOUT)

        try:
            out = agent.handle(sc["message"], order_id, agent_id,
                               proposal=proposal, query_vec=qvec,
                               recalled=recalled, barrier=hook)
            out.result["agent_id"] = agent_id
            results[name] = out
        except Exception as e:  # noqa: BLE001
            errors[name] = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
            both_read.abort()  # release the peer rather than let it wait out
        finally:
            if goes_first:
                first_committed.set()

    # The channels are the story: the same customer chasing the same refund on
    # two channels is what a shared memory layer has to survive.
    ta = threading.Thread(target=go, args=("A", "agent-email", True))
    tb = threading.Thread(target=go, args=("B", "agent-whatsapp", False))
    ta.start(); tb.start()
    ta.join(BARRIER_TIMEOUT * 3); tb.join(BARRIER_TIMEOUT * 3)

    if errors:
        raise RuntimeError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    if len(results) != 2:
        raise RuntimeError("a racer did not finish inside the timeout")

    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT refunded_minor FROM orders WHERE order_id = %s",
                    (order_id,))
        final = cur.fetchone()[0]
        cur.execute("SELECT count(*), coalesce(sum(amount_minor),0) "
                    "FROM actions WHERE order_id = %s", (order_id,))
        n_actions, paid = cur.fetchone()

    # Chronological, so the caller can render winner then loser without
    # re-sorting: decision_hlc is assigned by the cluster, not by us.
    ordered = sorted(results.values(),
                     key=lambda o: Decimal(o.result["decision_hlc"]))

    return {
        "order_id": order_id,
        "item": sc["item"],
        "amount_minor": sc["amount"],
        "decision_ids": [o.result["decision_id"] for o in ordered],
        "kinds": [o.result["kind"] for o in ordered],
        "attempts": [o.attempts for o in ordered],
        "saw_serialization_failure": any(
            getattr(o, "saw_serialization_failure", False)
            for o in results.values()),
        "actions_written": n_actions,
        "paid_minor": paid,
        "order_refunded_minor": final,
        "paid_once": n_actions == 1 and paid == sc["amount"] == final,
        "isolation": ordered[0].isolation,
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
