"""
explain_check.py — does the planner still use the vector index at real scale?

The earlier evidence was captured at 4 rows, where a full scan is a legitimate
plan, so it proved nothing durable. This re-runs it against the seeded corpus
and — more importantly — checks the FILTERED variants of recall.search(), which
have never been examined. Filtered vector queries commonly fall off an
approximate index, and search() currently claims in its docstring that they
do not.

Usage:  python explain_check.py
"""

import sys

import db
import embeddings
import recall

IDX = "cases_embedding_idx"

VARIANTS = [
    ("unfiltered",            "",                                   ()),
    ("channel = 'whatsapp'",  "WHERE channel = %s",                 ("whatsapp",)),
    ("resolved only",         "WHERE resolution IS NOT NULL",       ()),
    ("channel + resolved",    "WHERE channel = %s AND resolution IS NOT NULL",
                                                                    ("email",)),
]


def main():
    db.safe_console()
    vec = embeddings.to_sql(embeddings.embed("you took the money off my card twice"))

    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM cases WHERE embedding IS NOT NULL")
        n = cur.fetchone()[0]
        print(f"corpus: {n} embedded rows")
        print("=" * 70)

        results = []
        for label, clause, params in VARIANTS:
            sql = (f"SELECT case_id FROM cases {clause} "
                   f"ORDER BY embedding <-> %s::VECTOR(1024) LIMIT 5")
            args = (*params, vec) if clause else (vec,)
            # Placeholder order: the WHERE params come first in the string.
            cur.execute("EXPLAIN " + sql, args)
            plan = "\n".join(r[0] for r in cur.fetchall())
            low = plan.lower()
            uses = IDX in low and "full scan" not in low
            results.append((label, uses))
            print(f"[{' OK ' if uses else 'WARN'}]  {label}")
            print(f"        index named: {IDX in low}   full scan: "
                  f"{'full scan' in low}")
            for line in plan.splitlines():
                if line.strip():
                    print(f"        | {line}")
            print("-" * 70)

    print("=" * 70)
    for label, uses in results:
        print(f"  {'index ' if uses else 'SCAN  '}  {label}")

    bad = [l for l, u in results if not u]
    if bad:
        print(f"\n{len(bad)} variant(s) fall off the index: {', '.join(bad)}")
        print("Over-fetch and post-filter in Python rather than adding a prefix")
        print("column — a prefixed index re-silos retrieval per channel, which")
        print("is the thing this project exists to remove.")
        sys.exit(1)
    print("\nAll variants use the vector index.")


if __name__ == "__main__":
    main()
