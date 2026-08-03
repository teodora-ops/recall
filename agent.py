"""
agent.py — one support agent: read a message, recall, decide, act.

The shape matters more than the code:

  PHASE A — outside any transaction, network allowed
      embed the message            (cached)
      recall similar past cases    (vector index)
      ask Nova Pro for a proposal  (Converse API)

  PHASE B — inside ONE serializable transaction, no network at all
      read the order's current refund state     <- the conflicting read
      apply a pure policy gate to the proposal  <- the actual authorisation
      write the decision
      write the action it authorises
      update the order

Two rules make the demo work, and both are about Phase B:

1. **No model call inside the transaction.** On a 40001 retry the whole of
   Phase B re-runs. A model call in there would make the second attempt
   disagree with the first, hold the conflict open for seconds, and cost
   money per retry. Instead the retry re-reads state and re-decides
   deterministically — which is why the retry can legitimately produce a
   *different* decision.

2. **The update is an absolute assignment**, `SET refunded_minor = <value
   computed in Python>`, never `= refunded_minor + x`. The SQL-side increment
   is atomic and would survive READ COMMITTED, which destroys the
   demonstration. Application-side read-modify-write is also what an agent
   genuinely does: read context, reason, write.

The LLM proposes and supplies the rationale. It does not authorise. The
policy gate does, and it is a pure function so both the race retry and the
replay counterfactual are reproducible.
"""

import json
import os
import sys
from dataclasses import dataclass, asdict
from typing import Callable

import boto3

import db
import embeddings
import recall
import txn

RECALL_K = 5


# --------------------------------------------------------------------------
# The proposal — what the model suggests, before any authority is applied.
# --------------------------------------------------------------------------

@dataclass
class Proposal:
    kind: str            # refund_full | refund_partial | escalate | ...
    amount_minor: int
    rationale: str
    model: str


@dataclass
class Decision:
    kind: str
    amount_minor: int
    rationale: str


# --------------------------------------------------------------------------
# PHASE A — recall and propose. Network is fine here.
# --------------------------------------------------------------------------

def recall_context(message: str, k: int = RECALL_K, conn=None) -> list[dict]:
    """The memory read. Deliberately unfiltered: a WhatsApp message must be
    able to reach an email resolution, which is the whole point of a shared
    store."""
    return recall.search(message, k=k, conn=conn)


PROPOSAL_PROMPT = """You are a customer support agent for a small homeware shop.

The customer has written:
---
{message}
---

Here are the closest past cases from the shop's shared memory, with how they \
were resolved:

{precedent}

Order on file: {item}, £{amount:.2f}. Already refunded so far: £{refunded:.2f}.

Propose how to resolve this, consistent with how similar cases were handled \
before. Reply with STRICT JSON only:

{{"kind":"refund_full|refund_partial|escalate|information",
  "amount_minor":<integer pence, 0 if not a refund>,
  "rationale":"<one or two sentences, referring to the precedent>"}}
"""


def propose(message: str, recalled: list[dict], order: dict,
            model: str | None = None) -> Proposal:
    """Ask the model what it would do. This is a suggestion, not a decision."""
    model = model or os.getenv("BEDROCK_CHAT_MODEL")
    if not model:
        sys.exit("BEDROCK_CHAT_MODEL not set in .env")

    precedent = "\n".join(
        f"- [{c['channel']}] {c['subject']}: "
        f"{c['resolution'] or '(still open)'}"
        for c in recalled
    ) or "- (no similar cases found)"

    prompt = PROPOSAL_PROMPT.format(
        message=message, precedent=precedent,
        item=order["item"],
        amount=order["amount_minor"] / 100,
        refunded=order["refunded_minor"] / 100,
    )

    rt = boto3.Session(
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_REGION", "eu-west-2"),
    ).client("bedrock-runtime")

    resp = rt.converse(
        modelId=model,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 512, "temperature": 0.2},
    )
    text = resp["output"]["message"]["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])

    return Proposal(
        kind=str(data.get("kind", "escalate")),
        amount_minor=int(data.get("amount_minor") or 0),
        rationale=str(data.get("rationale", "")).strip(),
        model=model,
    )


# --------------------------------------------------------------------------
# The policy gate — pure, deterministic, and the only thing with authority.
# --------------------------------------------------------------------------

def policy_gate(proposal: Proposal, order: dict) -> Decision:
    """Turn a suggestion into an authorised decision.

    Pure by design. No I/O, no clock, no randomness — given the same proposal
    and the same order state it always returns the same decision. That is what
    lets the race retry re-decide correctly, and what lets replay compute a
    counterfactual ("what would it decide against today's memory?") without
    re-running the model.
    """
    remaining = order["amount_minor"] - order["refunded_minor"]

    # Already settled. This is the branch the losing agent reaches on retry,
    # and the reason a retry can produce a *different* decision.
    if remaining <= 0:
        return Decision(
            "decline_already_refunded", 0,
            f"Order {order['order_id']} is already fully refunded "
            f"(£{order['refunded_minor'] / 100:.2f}). No further refund due.",
        )

    if proposal.kind == "refund_full":
        return Decision("refund_full", remaining, proposal.rationale)

    if proposal.kind == "refund_partial":
        amount = max(0, min(proposal.amount_minor, remaining))
        if amount == 0:
            return Decision("decline_policy", 0,
                            "Partial refund proposed with no amount.")
        return Decision("refund_partial", amount, proposal.rationale)

    if proposal.kind in ("escalate", "information"):
        return Decision("escalate", 0, proposal.rationale or "Escalated.")

    return Decision("decline_policy", 0,
                    f"Unrecognised proposal kind {proposal.kind!r}.")


# --------------------------------------------------------------------------
# PHASE B — one serializable transaction. No network.
# --------------------------------------------------------------------------

def handle(message: str, order_id: str, agent_id: str,
           case_id: str | None = None,
           proposal: Proposal | None = None,
           barrier: Callable[[str], None] | None = None,
           isolation: str = "serializable") -> txn.Outcome:
    """Run one full agent turn and return how the transaction went.

    `barrier` is a hook the race harness uses to force a deterministic
    interleave: it is called with 'after_read' once the order has been read
    inside the transaction, and with 'before_commit' just before committing.
    In normal use it is None and costs nothing.
    """
    # ---- PHASE A: recall + propose, outside the transaction ----
    with db.connect() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT order_id, customer_ref, item, amount_minor, refunded_minor "
            "FROM orders WHERE order_id = %s", (order_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"no such order {order_id}")
        cols = [d.name for d in cur.description]
        order_snapshot = dict(zip(cols, row))

    recalled = recall_context(message)
    if proposal is None:
        proposal = propose(message, recalled, order_snapshot)

    query_vec = embeddings.to_sql(embeddings.embed(message))
    recalled_ids = [str(c["case_id"]) for c in recalled]
    recalled_dist = [float(c["distance"]) for c in recalled]

    # ---- PHASE B: decide and write, inside one transaction ----
    def work(cur, attempt):
        # The conflicting read. Two agents both land here before either
        # writes, which is what SERIALIZABLE detects.
        cur.execute(
            "SELECT order_id, item, amount_minor, refunded_minor "
            "FROM orders WHERE order_id = %s", (order_id,))
        cols = [d.name for d in cur.description]
        order = dict(zip(cols, cur.fetchone()))

        if barrier:
            barrier("after_read")

        decision = policy_gate(proposal, order)

        cur.execute(
            """INSERT INTO decisions
                 (agent_id, case_id, order_id, customer_ref, decision_kind,
                  amount_minor, rationale, chat_model, query_text,
                  query_embedding, recalled_case_ids, recalled_distances,
                  attempt)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::VECTOR(1024),%s,%s,%s)
               RETURNING decision_id, decision_hlc""",
            (agent_id, case_id, order_id, order_snapshot["customer_ref"],
             decision.kind, decision.amount_minor, decision.rationale,
             proposal.model, message, query_vec,
             recalled_ids, recalled_dist, attempt),
        )
        decision_id, decision_hlc = cur.fetchone()

        acted = False
        if decision.kind in ("refund_full", "refund_partial") and decision.amount_minor > 0:
            cur.execute(
                """INSERT INTO actions
                     (decision_id, action_kind, order_id, amount_minor, external_ref)
                   VALUES (%s,'refund',%s,%s,%s)""",
                (decision_id, order_id, decision.amount_minor,
                 f"psp_{str(decision_id)[:8]}"),
            )
            # Absolute assignment, computed here rather than in SQL. See the
            # module docstring — an in-SQL increment would survive READ
            # COMMITTED and the demo would prove nothing.
            new_total = order["refunded_minor"] + decision.amount_minor
            cur.execute(
                "UPDATE orders SET refunded_minor = %s WHERE order_id = %s",
                (new_total, order_id))
            acted = True

        if barrier:
            barrier("before_commit")

        return {
            "decision_id": str(decision_id),
            "decision_hlc": str(decision_hlc),
            "kind": decision.kind,
            "amount_minor": decision.amount_minor,
            "rationale": decision.rationale,
            "acted": acted,
            "recalled": recalled_ids,
        }

    return txn.run_serializable(work, isolation=isolation)


def _cli(argv):
    db.safe_console()
    if len(argv) < 2:
        print(__doc__.strip())
        print('\nusage: python agent.py <order_id> "<customer message>" '
              '[agent_id]')
        return
    order_id, message = argv[0], argv[1]
    agent_id = argv[2] if len(argv) > 2 else "agent-cli"

    out = handle(message, order_id, agent_id)
    r = out.result
    print(f"agent      : {agent_id}")
    print(f"transaction: {out}")
    print(f"decision   : {r['kind']}  £{r['amount_minor'] / 100:.2f}")
    print(f"rationale  : {r['rationale']}")
    print(f"recalled   : {len(r['recalled'])} case(s)")
    print(f"hlc        : {r['decision_hlc']}")


if __name__ == "__main__":
    _cli(sys.argv[1:])
