"""
race.py — two agents, one refund. Capability 2.

Jess Ellis is charged £34.98 twice for ORD-4502 and reports it on email AND
on WhatsApp, minutes apart, because that is what people do. Two agents pick
it up. Both read the order, both see no refund yet, both conclude "refund
£34.98".

Under SERIALIZABLE exactly one of them can commit. The other gets 40001,
retries, re-reads the order, sees the refund already happened, and DECLINES —
and that declined decision is still written, so the audit trail records that a
second agent considered the refund and refused it.

    python race.py                          the demo
    python race.py --isolation read_committed   the control

The control matters more than it looks. It runs the identical code one
isolation level down and shows the business paying twice while the ledger
claims once. That is what turns "we used a serializable database" into a
measured account of what you lose without one.

WHAT THIS ASSERTS, AND WHY NOT THE OBVIOUS THING

The order update is an absolute assignment computed in Python, so a lost
update and a correct abort leave the SAME final refunded_minor. The number
cannot tell them apart. What can:

  - whether a 40001 was actually observed
  - how many rows landed in `actions`  (money moved twice?)
  - whether the second decision declined or refunded again

So those are asserted, and the final amount is only reported.
"""

import argparse
import sys
import threading

import agent
import db

ORDER_ID = "ORD-4502"
CASE_A = ("agent-email", "email",
          "I've been charged 34.98 twice for order 4502. I only ordered once "
          "- can you refund the duplicate?")
CASE_B = ("agent-whatsapp", "whatsapp",
          "hi ive been billed twice for the mugs. 34.98 twice on my card. "
          "can you sort it")


def reset(cur):
    """Put ORD-4502 back to un-refunded so the demo is repeatable."""
    cur.execute("DELETE FROM actions WHERE order_id = %s", (ORDER_ID,))
    cur.execute("DELETE FROM decisions WHERE order_id = %s", (ORDER_ID,))
    cur.execute("UPDATE orders SET refunded_minor = 0 WHERE order_id = %s",
                (ORDER_ID,))


def case_ids(cur):
    """The two real hero cases, so decisions link to actual customer messages."""
    cur.execute(
        "SELECT channel, case_id FROM cases "
        "WHERE customer_ref = 'ELLIS-J' AND channel IN ('email','whatsapp') "
        "ORDER BY channel")
    return {ch: cid for ch, cid in cur.fetchall()}


def control(isolation: str) -> None:
    """The isolation control: the same read-modify-write, one level apart.

    This deliberately does NOT write decision rows, because it cannot —
    cluster_logical_timestamp() is unsupported under READ COMMITTED, so the
    replay anchor does not exist there at all. That is a finding in its own
    right and is reported below rather than worked around.

    What remains is the part that moves money: read refunded_minor, decide in
    application code, write an absolute value back. Exactly what both agents
    do, stripped of everything else.
    """
    import secrets

    import psycopg

    # The control gets its OWN order. It used to contend on ORD-4502 and reset
    # it afterwards, which silently deleted the race decisions the hero screen
    # and the replay demo depend on — running the control after the race wiped
    # the evidence the race had just produced.
    control_order = f"CONTROL-{secrets.token_hex(4)}"
    amount = 3498

    with db.connect() as c:
        cur = c.cursor()
        cur.execute(
            "INSERT INTO customers (customer_ref, display_name) "
            "VALUES ('CONTROL-DEMO','Control Demo') "
            "ON CONFLICT (customer_ref) DO NOTHING")
        cur.execute(
            "INSERT INTO orders (order_id, customer_ref, item, amount_minor) "
            "VALUES (%s,'CONTROL-DEMO','Set of four stoneware mugs',%s)",
            (control_order, amount))

    both_read = threading.Barrier(2)
    committed, aborted = [], []

    def worker(name):
        try:
            with psycopg.connect(db.dsn(), autocommit=False) as w:
                cur = w.cursor()
                cur.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
                cur.execute("SELECT refunded_minor FROM orders "
                            "WHERE order_id = %s", (control_order,))
                seen = cur.fetchone()[0]
                both_read.wait(timeout=30)          # both have read 0
                # Application-side read-modify-write, absolute assignment.
                cur.execute("UPDATE orders SET refunded_minor = %s "
                            "WHERE order_id = %s", (seen + amount, control_order))
                w.commit()
                committed.append(name)
        except psycopg.errors.SerializationFailure:
            aborted.append(name)
        except Exception as e:  # noqa: BLE001
            aborted.append(f"{name}:{type(e).__name__}")

    ts = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)

    with db.connect() as c:
        cur = c.cursor()
        cur.execute("SELECT refunded_minor FROM orders WHERE order_id = %s",
                    (control_order,))
        final = cur.fetchone()[0]

    print(f"isolation          : {isolation}")
    print(f"order              : {control_order} (its own — never ORD-4502)")
    print(f"refunds authorised : {len(committed)}  (committed: {committed})")
    print(f"aborted            : {len(aborted)}  {aborted}")
    print(f"order amount       : £{amount / 100:.2f}")
    print(f"order says refunded: £{final / 100:.2f}")
    print("-" * 70)
    if len(committed) == 2:
        print(f"LOST UPDATE. Two agents each authorised a £{amount / 100:.2f} "
              f"refund and both committed.")
        print(f"The customer is owed £{amount / 100:.2f} and has been "
              f"refunded £{2 * amount / 100:.2f}, while the order row still "
              f"claims £{final / 100:.2f}.")
        print("The money and the ledger disagree, and nothing errored.")
    elif len(committed) == 1:
        print("One refund authorised, one transaction aborted. The database "
              "refused the second write.")
    else:
        print("Neither committed — inconclusive, re-run.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--isolation", default="serializable",
                    choices=["serializable", "read committed", "read_committed"])
    ap.add_argument("--control", action="store_true",
                    help="isolation control: the bare read-modify-write only")
    args = ap.parse_args()
    isolation = args.isolation.replace("_", " ")

    if args.control:
        db.safe_console()
        control(isolation)
        return

    db.safe_console()

    with db.connect() as c:
        cur = c.cursor()
        reset(cur)
        ids = case_ids(cur)
        cur.execute("SELECT item, amount_minor, refunded_minor FROM orders "
                    "WHERE order_id = %s", (ORDER_ID,))
        item, amount, refunded = cur.fetchone()

    print(f"order      : {ORDER_ID} — {item}")
    print(f"amount     : £{amount / 100:.2f}   already refunded: "
          f"£{refunded / 100:.2f}")
    print(f"isolation  : {isolation}")
    print("-" * 70)

    # Force the interleave. A race that fires sixty percent of the time is not
    # a demo — both agents must be made to read before either writes.
    both_read = threading.Barrier(2)
    first_committed = threading.Event()
    results: dict[str, object] = {}
    errors: dict[str, str] = {}

    def run(name, agent_id, channel, message, goes_first):
        def hook(phase, attempt):
            # Retries run unimpeded. Holding a retry at a barrier the other
            # worker has already passed would deadlock it.
            if attempt > 1:
                return
            if phase == "after_read":
                # Both agents have now read refunded_minor = 0. This is the
                # stale read the loser will be aborted for.
                both_read.wait(timeout=60)
                if not goes_first:
                    # Let the winner COMMIT before the loser writes anything.
                    # Waiting at before_commit instead lets both write
                    # concurrently, and then they abort each other — the
                    # outcome is still correct but the story is muddled, with
                    # two conflicts and three attempts to explain.
                    first_committed.wait(timeout=60)

        try:
            out = agent.handle(message, ORDER_ID, agent_id,
                               case_id=ids.get(channel), barrier=hook,
                               isolation=isolation)
            out.result["agent_id"] = agent_id
            results[name] = out
        except Exception as e:  # noqa: BLE001
            errors[name] = f"{type(e).__name__}: {str(e).splitlines()[0]}"
            both_read.abort()
        finally:
            if goes_first:
                first_committed.set()

    ta = threading.Thread(target=run, args=("A", *CASE_A, True))
    tb = threading.Thread(target=run, args=("B", *CASE_B, False))
    ta.start(); tb.start(); ta.join(180); tb.join(180)

    for name in ("A", "B"):
        if name in errors:
            print(f"{name}: ERROR {errors[name]}")
            continue
        out = results[name]
        r = out.result
        print(f"{name}  {r['agent_id']:<16} {out}")
        print(f"   decision : {r['kind']}  £{r['amount_minor'] / 100:.2f}")
        if r.get("abort_sqlstate"):
            print(f"   aborted  : SQLSTATE {r['abort_sqlstate']} on attempt "
                  f"{r['attempt'] - 1}, re-decided on attempt {r['attempt']}")
        if r.get("conflicts_with"):
            print(f"   conflicts: {r['conflicts_with'][:8]}")
        print(f"   rationale: {r['rationale'][:88]}")

    # ---- post-conditions ------------------------------------------------
    print("-" * 70)
    with db.connect() as c:
        cur = c.cursor()
        cur.execute("SELECT refunded_minor FROM orders WHERE order_id = %s",
                    (ORDER_ID,))
        final = cur.fetchone()[0]
        cur.execute("SELECT count(*), coalesce(sum(amount_minor),0) "
                    "FROM actions WHERE order_id = %s", (ORDER_ID,))
        n_actions, paid = cur.fetchone()
        cur.execute("SELECT agent_id, decision_kind, amount_minor, attempt, "
                    "abort_sqlstate FROM decisions WHERE order_id = %s "
                    "ORDER BY decision_hlc", (ORDER_ID,))
        decisions = cur.fetchall()

    print(f"decisions written : {len(decisions)}")
    for a, k, amt, att, sq in decisions:
        print(f"   {a:<16} {k:<26} £{amt / 100:>6.2f}  attempt={att}"
              + (f"  aborted={sq}" if sq else ""))
    print(f"actions written   : {n_actions}")
    print(f"money actually out: £{paid / 100:.2f}")
    print(f"order says refunded: £{final / 100:.2f}")

    saw_40001 = any(
        getattr(out, "saw_serialization_failure", False)
        for out in results.values()
    )
    print("-" * 70)

    checks = [
        ("exactly one refund action", n_actions == 1),
        ("money out equals order value", paid == amount),
        ("ledger agrees with money out", final == paid),
        ("both agents recorded a decision", len(decisions) == 2),
        ("a serialization failure was observed", saw_40001),
        ("the loser declined rather than refunded",
         any(k == "decline_already_refunded" for _, k, _, _, _ in decisions)),
    ]
    failed = 0
    for label, ok in checks:
        print(f"[{' OK ' if ok else 'FAIL'}]  {label}")
        failed += not ok

    print("-" * 70)
    if isolation == "serializable":
        if failed:
            print(f"{failed} CHECK(S) FAILED — capability 2 is not demonstrated.")
            sys.exit(1)
        print("Two agents, one refund, paid exactly once.")
    else:
        # Under READ COMMITTED the failures ARE the finding.
        if n_actions > 1 or paid > amount:
            print(f"DOUBLE PAY: {n_actions} refund actions totalling "
                  f"£{paid / 100:.2f} against a £{amount / 100:.2f} order, "
                  f"while the order row claims £{final / 100:.2f}.")
            print("The ledger and the money disagree. This is what "
                  "SERIALIZABLE prevents.")
        else:
            print("No double pay observed on this run. READ COMMITTED does "
                  "not guarantee one either way — re-run to see it vary.")


if __name__ == "__main__":
    main()
