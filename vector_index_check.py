"""
vector_index_check.py — will this cluster actually index VECTOR(1024)?

Storing a VECTOR(1024) column is not the question; the cluster already does
that. The question is whether the distributed (C-SPANN) vector index accepts
1024 dimensions and whether the planner will actually use it for a nearest
neighbour scan. Both have to be true before seeding, because changing the
dimension after seeding means re-embedding the whole corpus.

Works on a throwaway table and drops it again. Touches nothing in the schema.

Usage:  python vector_index_check.py
"""

import os
import sys

# EXPLAIN output is drawn with box characters; the default Windows console
# codepage cannot encode them.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import psycopg
except ImportError:
    sys.exit("psycopg not installed. Run:  pip install \"psycopg[binary]\"")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("(python-dotenv not installed — relying on shell env vars)\n")

PROBE = "_vector_probe_1024"
IDX = "_vector_probe_idx"
DIMS = 1024


def dsn():
    url = os.getenv("COCKROACH_URL")
    if not url:
        sys.exit("COCKROACH_URL not set in .env or environment.")
    # verify-full needs the CA explicitly; the URL in .env does not carry it.
    if "sslrootcert=" not in url:
        ca = os.path.join(os.environ.get("APPDATA", ""), "postgresql", "root.crt")
        if not os.path.exists(ca):
            sys.exit(f"CA cert not found at {ca}")
        url += ("&" if "?" in url else "?") + "sslrootcert=" + ca.replace("\\", "/")
    return url


def step(label, fn):
    """Run one probe step. Returns (ok, result_or_error)."""
    try:
        result = fn()
        print(f"[ OK ]   {label}")
        if isinstance(result, str):
            print(f"         -> {result}")
        return True, result
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL]   {label}")
        print(f"         -> {type(e).__name__}: {str(e).strip().splitlines()[0]}")
        return False, e


def main():
    with psycopg.connect(dsn(), autocommit=True) as conn:
        cur = conn.cursor()

        cur.execute("SELECT version()")
        print(f"Cluster:  {cur.fetchone()[0].split(' (')[0]}")
        cur.execute("SELECT current_database(), current_user")
        db, user = cur.fetchone()
        print(f"Database: {db}   User: {user}")
        print("-" * 68)

        # Clean slate in case a previous run died mid-probe.
        cur.execute(f"DROP TABLE IF EXISTS {PROBE}")

        ok, _ = step(
            f"CREATE TABLE with VECTOR({DIMS}) column",
            lambda: cur.execute(
                f"CREATE TABLE {PROBE} (id INT PRIMARY KEY, embedding VECTOR({DIMS}))"
            ),
        )
        if not ok:
            sys.exit("Cannot even create the column. Stop here.")

        # The index syntax has moved between CockroachDB versions. Try the
        # documented forms in order and record which one this cluster takes.
        variants = [
            ("CREATE VECTOR INDEX ... (embedding)",
             f"CREATE VECTOR INDEX {IDX} ON {PROBE} (embedding)"),
            ("CREATE INDEX ... USING cspann (embedding)",
             f"CREATE INDEX {IDX} ON {PROBE} USING cspann (embedding)"),
            ("CREATE INDEX ... USING cspann (embedding vector_cosine_ops)",
             f"CREATE INDEX {IDX} ON {PROBE} USING cspann (embedding vector_cosine_ops)"),
            ("CREATE INDEX ... USING cspann (embedding vector_l2_ops)",
             f"CREATE INDEX {IDX} ON {PROBE} USING cspann (embedding vector_l2_ops)"),
        ]

        print("-" * 68)
        print(f"Index acceptance at {DIMS} dimensions:")
        working = None
        for label, sql in variants:
            ok, _ = step(label, lambda s=sql: cur.execute(s))
            if ok:
                working = (label, sql)
                break

        if not working:
            print("-" * 68)
            print(f"NO INDEX FORM ACCEPTED {DIMS} DIMENSIONS.")
            print("Re-run this probe with DIMS = 512 before writing the schema.")
            cur.execute(f"DROP TABLE IF EXISTS {PROBE}")
            sys.exit(1)

        # Creating the index is necessary but not sufficient — the planner has
        # to choose it. Put enough rows in to make a scan worth planning.
        print("-" * 68)
        step(
            "INSERT 500 rows of real-width vectors",
            lambda: cur.execute(
                f"INSERT INTO {PROBE} (id, embedding) "
                f"SELECT g, ARRAY(SELECT random() FROM generate_series(1, {DIMS}))::VECTOR({DIMS}) "
                f"FROM generate_series(1, 500) AS g"
            ),
        )

        probe_vec = "[" + ",".join(["0.01"] * DIMS) + "]"

        ok, _ = step(
            "Nearest-neighbour query returns rows",
            lambda: cur.execute(
                f"SELECT id FROM {PROBE} ORDER BY embedding <-> %s::VECTOR({DIMS}) LIMIT 5",
                (probe_vec,),
            ),
        )
        if ok:
            print(f"         -> ids {[r[0] for r in cur.fetchall()]}")

        # The decisive check: does EXPLAIN name the index, or fall back to a
        # full scan + sort? Matching the word "vector" is not enough — it also
        # appears in the column type and the ORDER BY expression.
        cur.execute(
            f"EXPLAIN SELECT id FROM {PROBE} ORDER BY embedding <-> %s::VECTOR({DIMS}) LIMIT 5",
            (probe_vec,),
        )
        plan = "\n".join(r[0] for r in cur.fetchall())
        low = plan.lower()
        named_index = IDX in low
        full_scan = "full scan" in low
        used_index = named_index and not full_scan
        print("-" * 68)
        print(f"[{' OK ' if used_index else 'WARN'}]   Planner uses the vector index "
              f"(index named: {named_index}, full scan: {full_scan})")
        for line in plan.splitlines():
            if line.strip():
                print(f"         | {line}")

        cur.execute(f"DROP TABLE IF EXISTS {PROBE}")

        print("-" * 68)
        print(f"VERDICT: VECTOR({DIMS}) is indexable on this cluster.")
        print(f"Working index syntax:\n  {working[1].replace(PROBE, '<table>')}")
        if not used_index:
            print("\nCaveat: the index was CREATED but the planner did not use it")
            print("on 500 rows. That is not automatically a problem — small tables")
            print("plan as full scans — but it means this run has NOT proven the")
            print("index serves queries. Re-check the plan once real data lands.")


if __name__ == "__main__":
    main()
