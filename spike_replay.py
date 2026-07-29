"""
spike_replay.py — four questions that must be answered before schema.sql grows.

Each has a plan B that changes the schema, so discovering any of them late
costs the headline feature. Run once, read the verdicts, then write the DDL.

  A. Is cluster_logical_timestamp() legal as a column DEFAULT?
  B. What literal form does AS OF SYSTEM TIME accept — does it take a
     placeholder, or must the decimal be interpolated?
  C. Can the C-SPANN vector index serve a search inside AS OF SYSTEM TIME?
  D. What is the session isolation level, and can two connections actually
     produce a 40001?

Self-cleaning: every object it creates is dropped again.

Usage:  python spike_replay.py
"""

import sys
import threading

import psycopg

import db
import embeddings

T = "_spike_decisions"
C = "_spike_cases"
IDX = "_spike_cases_idx"
DIMS = 1024

results = {}


def verdict(key, ok, detail=""):
    results[key] = (ok, detail)
    print(f"[{' OK ' if ok else 'FAIL'}]   {key}")
    for line in str(detail).splitlines():
        if line.strip():
            print(f"         {line}")


def cleanup(cur):
    for obj in (T, C):
        cur.execute(f"DROP TABLE IF EXISTS {obj}")


# ----------------------------------------------------------------- A
def spike_a(cur):
    """cluster_logical_timestamp() as a column DEFAULT."""
    cur.execute(f"DROP TABLE IF EXISTS {T}")
    try:
        cur.execute(
            f"""CREATE TABLE {T} (
                    id INT PRIMARY KEY,
                    hlc DECIMAL NOT NULL DEFAULT cluster_logical_timestamp()
                )"""
        )
    except Exception as e:
        verdict("A. cluster_logical_timestamp() as DEFAULT", False,
                f"{str(e).strip().splitlines()[0]}\n"
                "PLAN B: write it explicitly in the INSERT column list.")
        return None

    cur.execute(f"INSERT INTO {T} (id) VALUES (1) RETURNING hlc")
    hlc = cur.fetchone()[0]
    verdict("A. cluster_logical_timestamp() as DEFAULT", True,
            f"accepted; sample value {hlc}")
    return hlc


# ----------------------------------------------------------------- B
def spike_b(cur, hlc):
    """Does AS OF SYSTEM TIME take a placeholder, or only a literal?"""
    placeholder_ok = False
    try:
        cur.execute(f"SELECT count(*) FROM {T} AS OF SYSTEM TIME %s", (hlc,))
        cur.fetchone()
        placeholder_ok = True
    except Exception as e:
        placeholder_detail = str(e).strip().splitlines()[0]

    literal_ok = False
    try:
        cur.execute(f"SELECT count(*) FROM {T} AS OF SYSTEM TIME {hlc}")
        cur.fetchone()
        literal_ok = True
    except Exception as e:
        literal_detail = str(e).strip().splitlines()[0]

    if placeholder_ok:
        verdict("B. AS OF SYSTEM TIME accepts a placeholder", True,
                "parameterised AOST works — no string interpolation needed.")
    else:
        verdict("B. AS OF SYSTEM TIME accepts a placeholder", False,
                f"{placeholder_detail}\n"
                f"literal form works: {literal_ok}\n"
                "PLAN B: interpolate the decimal, guarded by a strict "
                "Decimal(str) round-trip validator in replay.py.")
    return placeholder_ok, literal_ok


# ----------------------------------------------------------------- C
def spike_c(cur):
    """Can the vector index serve a search under AS OF SYSTEM TIME?

    This is the highest-value spike. If historical vector reads fail or
    silently full-scan, the 'same query as it was then' diff is dead and
    replay must instead reconstruct from stored case_ids.
    """
    cur.execute(f"DROP TABLE IF EXISTS {C}")
    cur.execute(f"CREATE TABLE {C} (id INT PRIMARY KEY, embedding VECTOR({DIMS}))")
    cur.execute(f"CREATE VECTOR INDEX {IDX} ON {C} (embedding)")
    cur.execute(
        f"INSERT INTO {C} (id, embedding) "
        f"SELECT g, ARRAY(SELECT random() FROM generate_series(1,{DIMS}))::VECTOR({DIMS}) "
        f"FROM generate_series(1,500) AS g"
    )
    cur.execute("SELECT cluster_logical_timestamp()")
    hlc = cur.fetchone()[0]

    # Mutate after the snapshot so 'then' and 'now' genuinely differ.
    cur.execute(f"DELETE FROM {C} WHERE id <= 250")

    probe = "[" + ",".join(["0.01"] * DIMS) + "]"

    cur.execute(f"SELECT count(*) FROM {C}")
    now_rows = cur.fetchone()[0]

    try:
        cur.execute(
            f"SELECT count(*) FROM {C} AS OF SYSTEM TIME {hlc}"
        )
        then_rows = cur.fetchone()[0]
    except Exception as e:
        verdict("C. vector search under AOST", False,
                f"historical read failed: {str(e).strip().splitlines()[0]}")
        return

    # The real question: does the index serve the historical vector query?
    try:
        cur.execute(
            f"SELECT id FROM {C} AS OF SYSTEM TIME {hlc} "
            f"ORDER BY embedding <-> '{probe}'::VECTOR({DIMS}) LIMIT 5"
        )
        ids = [r[0] for r in cur.fetchall()]
    except Exception as e:
        verdict("C. vector search under AOST", False,
                f"{str(e).strip().splitlines()[0]}\n"
                "PLAN B: reconstruct the recalled set from stored case_ids "
                "and run only the present-day query live.")
        return

    cur.execute(
        f"EXPLAIN SELECT id FROM {C} AS OF SYSTEM TIME {hlc} "
        f"ORDER BY embedding <-> '{probe}'::VECTOR({DIMS}) LIMIT 5"
    )
    plan = "\n".join(r[0] for r in cur.fetchall())
    low = plan.lower()
    uses_index = IDX in low and "full scan" not in low

    detail = (f"rows then={then_rows} now={now_rows} (history is visible)\n"
              f"historical NN query returned ids {ids}\n"
              f"plan names {IDX}: {IDX in low}   full scan: {'full scan' in low}")
    verdict("C. vector search under AOST uses the index", uses_index, detail)
    if not uses_index:
        print("         PLAN B: reconstruct recalled set from stored case_ids.")
    for line in plan.splitlines():
        if line.strip():
            print(f"         | {line}")


# ----------------------------------------------------------------- D
def spike_d(cur, conn):
    """Session isolation, and whether two connections really produce 40001."""
    cur.execute("SHOW default_transaction_isolation")
    default_iso = cur.fetchone()[0]
    cur.execute("SHOW transaction_isolation")
    session_iso = cur.fetchone()[0]
    verdict("D1. session isolation level",
            "serializable" in str(session_iso).lower(),
            f"default_transaction_isolation = {default_iso}\n"
            f"transaction_isolation         = {session_iso}")

    # Force a read-write conflict on one row from two connections.
    cur.execute(f"DROP TABLE IF EXISTS {T}")
    cur.execute(f"CREATE TABLE {T} (id INT PRIMARY KEY, amount INT NOT NULL)")
    cur.execute(f"INSERT INTO {T} VALUES (1, 0)")

    read_barrier = threading.Barrier(2)
    first_committed = threading.Event()
    outcomes = {}

    def worker(name, wait_for_other):
        try:
            with psycopg.connect(db.dsn(), autocommit=False) as w:
                c = w.cursor()
                c.execute(f"SELECT amount FROM {T} WHERE id = 1")
                amount = c.fetchone()[0]
                read_barrier.wait(timeout=20)
                if wait_for_other:
                    first_committed.wait(timeout=20)
                # Application-side read-modify-write, deliberately NOT
                # 'amount = amount + x' — the SQL increment is atomic and
                # would survive READ COMMITTED, hiding the conflict.
                c.execute(f"UPDATE {T} SET amount = %s WHERE id = 1", (amount + 100,))
                w.commit()
                outcomes[name] = "committed"
                if not wait_for_other:
                    first_committed.set()
        except psycopg.errors.SerializationFailure as e:
            outcomes[name] = f"40001 {e.sqlstate}"
            first_committed.set()
        except Exception as e:  # noqa: BLE001
            outcomes[name] = f"{type(e).__name__}: {str(e).splitlines()[0][:70]}"
            first_committed.set()

    ta = threading.Thread(target=worker, args=("A", False))
    tb = threading.Thread(target=worker, args=("B", True))
    ta.start(); tb.start(); ta.join(25); tb.join(25)

    cur.execute(f"SELECT amount FROM {T} WHERE id = 1")
    final = cur.fetchone()[0]

    saw_40001 = any("40001" in v for v in outcomes.values())
    lost_update = final == 100 and all("committed" == v for v in outcomes.values())

    verdict("D2. two connections produce a 40001", saw_40001,
            f"outcomes: {outcomes}\n"
            f"final amount: {final} (200 = both applied, 100 = lost update)\n"
            + ("" if saw_40001 else
               "NO SERIALIZATION ERROR OBSERVED. The race demo would run "
               "green and double-pay.\n"
               f"lost update detected: {lost_update}"))


def main():
    db.safe_console()
    print(f"embedding dims: {embeddings.DIMS}")
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT version()")
        print(f"cluster: {cur.fetchone()[0].split(' (')[0]}")
        print("-" * 70)
        try:
            hlc = spike_a(cur)
            print("-" * 70)
            if hlc is not None:
                spike_b(cur, hlc)
            print("-" * 70)
            spike_c(cur)
            print("-" * 70)
            spike_d(cur, conn)
        finally:
            cleanup(cur)
            print("-" * 70)
            print("cleanup: throwaway tables dropped")

    failed = [k for k, (ok, _) in results.items() if not ok]
    print("-" * 70)
    if failed:
        print("SPIKES NEEDING A PLAN B: " + ", ".join(failed))
        sys.exit(1)
    print("All four spikes clear. Schema can be written as designed.")


if __name__ == "__main__":
    main()
