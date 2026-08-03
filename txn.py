"""
txn.py — run a unit of work in one transaction, and retry it properly.

CockroachDB runs SERIALIZABLE by default. Under it, two transactions that
read the same row and then write based on what they read cannot both commit:
one gets SQLSTATE 40001 and must start over. That is not an error condition
to be logged and swallowed — it is the database refusing to let two agents
authorise the same refund, and retrying is how the application cooperates.

The helper returns *how* the work completed, not just the result. The race
demo asserts on the attempt count and the observed SQLSTATE, because with an
absolute-assignment update a lost update and a correct abort leave the same
final number — the retry is the only thing that distinguishes them.
"""

import random
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import psycopg

import db

SERIALIZATION_FAILURE = "40001"

_HLC = re.compile(r"^\d{1,30}(\.\d{1,30})?$")


def validate_hlc(value) -> str:
    """Return an HLC decimal safe to interpolate into SQL, or raise.

    This exists because AS OF SYSTEM TIME does not accept a placeholder —
    CockroachDB rejects it with "only constant expressions ... are allowed"
    (spike_replay.py, spike B). The timestamp therefore has to go into the
    statement as text, and anything reaching SQL as text needs a gate in front
    of it.

    Two independent checks, because either alone is weaker than it looks: a
    strict character pattern, and a Decimal round-trip that the value must
    survive unchanged. Nothing that fails both is ever interpolated.
    """
    s = str(value).strip()
    if not _HLC.match(s):
        raise ValueError(f"not a valid HLC timestamp: {value!r}")
    try:
        if str(Decimal(s)) != s:
            # Reject anything whose textual form is not canonical — '1e9',
            # '+5', '0005' and similar all fail here.
            raise ValueError(f"non-canonical HLC timestamp: {value!r}")
    except InvalidOperation as e:
        raise ValueError(f"not a decimal: {value!r}") from e
    return s


def read_snapshot(decision_hlc) -> str:
    """The timestamp that reconstructs what a decision READ, not what it wrote.

    cluster_logical_timestamp() returns the transaction's COMMIT timestamp,
    and AS OF SYSTEM TIME at that exact value is inclusive of the
    transaction's own writes — the refund already applied, the decision row
    already present. Measured on the live cluster:

        AS OF decision_hlc      -> refunded_minor = 3498, decision visible
        AS OF decision_hlc - 1  -> refunded_minor = 0,    decision absent

    One logical tick earlier is the last instant before this transaction's
    effects landed, which is precisely the world it observed. Every replay
    query goes through here rather than using decision_hlc directly.
    """
    return validate_hlc(Decimal(validate_hlc(decision_hlc)) - 1)


@dataclass
class Outcome:
    """What actually happened, including the parts a return value hides."""
    result: Any = None
    attempts: int = 0
    sqlstates: list[str] = field(default_factory=list)
    isolation: str | None = None
    committed: bool = False

    @property
    def retried(self) -> bool:
        return self.attempts > 1

    @property
    def saw_serialization_failure(self) -> bool:
        return SERIALIZATION_FAILURE in self.sqlstates

    def __str__(self) -> str:
        s = (f"attempts={self.attempts} committed={self.committed} "
             f"isolation={self.isolation}")
        if self.sqlstates:
            s += f" sqlstates={self.sqlstates}"
        return s


def run_serializable(
    work: Callable[[psycopg.Cursor, int, list[str]], Any],
    *,
    max_attempts: int = 5,
    isolation: str = "serializable",
    conn: psycopg.Connection | None = None,
) -> Outcome:
    """Run `work(cursor, attempt, prior_sqlstates)` in one transaction, retrying on 40001.

    `prior_sqlstates` is the list of failures already seen on this unit of
    work, so the body can record that it is a retry and why. A decision that
    knows it was forced to re-decide is the audit trail the race demo turns
    on — "retried and succeeded" is a database feature, "retried, re-read
    state and declined" is an agent one.

    `work` MUST be safe to run more than once, and MUST NOT make network
    calls. Both rules exist for the same reason: on a retry the whole body
    re-runs, so anything non-deterministic inside it makes the second attempt
    disagree with the first, and anything slow holds the conflict open. In
    this project that means the model call happens *before* the transaction
    and only the pure policy decision happens inside it.

    `isolation` is a parameter so the same code can be run under READ
    COMMITTED as a control. That comparison is the point of the demo: the
    identical work, one isolation level apart, double-pays or does not.
    """
    outcome = Outcome()
    owns = conn is None
    delay = 0.05

    for attempt in range(1, max_attempts + 1):
        outcome.attempts = attempt
        c = conn or psycopg.connect(db.dsn(), autocommit=False)
        try:
            cur = c.cursor()

            # Pin the level explicitly rather than trusting the session
            # default. An unpinned run that silently uses READ COMMITTED
            # passes every assertion about the final amount while quietly
            # double-paying — which is the exact failure this demo exists to
            # make visible.
            cur.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation}")
            cur.execute("SHOW transaction_isolation")
            outcome.isolation = cur.fetchone()[0]

            outcome.result = work(cur, attempt, list(outcome.sqlstates))
            c.commit()
            outcome.committed = True
            return outcome

        except psycopg.errors.SerializationFailure as e:
            outcome.sqlstates.append(e.sqlstate or SERIALIZATION_FAILURE)
            try:
                c.rollback()
            except Exception:  # noqa: BLE001
                pass
            if attempt == max_attempts:
                raise
            # Jittered backoff. Without jitter two contending workers retry in
            # lockstep and collide again.
            time.sleep(delay + random.random() * delay)
            delay *= 2

        except Exception:
            try:
                c.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise

        finally:
            if owns:
                c.close()

    return outcome
