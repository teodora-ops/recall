"""
apply_schema.py — push schema.sql at the live cluster and show what landed.

Idempotent: every statement is IF NOT EXISTS or a zone config, so re-running
is safe. Prints the resulting table, indexes and GC window so the state is
visible rather than assumed.

Usage:  python apply_schema.py
"""

import re
import sys
from pathlib import Path

import db

SCHEMA = Path(__file__).parent / "schema.sql"


def statements(sql: str):
    """Split on semicolons at end of line, ignoring comment-only chunks."""
    for raw in re.split(r";\s*(?:\n|$)", sql):
        s = raw.strip()
        if not s:
            continue
        if all(line.strip().startswith("--") or not line.strip()
               for line in s.splitlines()):
            continue
        yield s


def main():
    db.safe_console()
    sql = SCHEMA.read_text(encoding="utf-8")

    with db.connect() as conn:
        cur = conn.cursor()
        for stmt in statements(sql):
            label = " ".join(stmt.split())[:72]
            try:
                cur.execute(stmt)
                print(f"[ OK ]   {label}")
            except Exception as e:  # noqa: BLE001
                print(f"[FAIL]   {label}")
                print(f"         -> {type(e).__name__}: "
                      f"{str(e).strip().splitlines()[0]}")
                sys.exit(1)

        print("-" * 68)
        cur.execute("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'cases'
            ORDER BY ordinal_position
        """)
        print("cases:")
        for name, typ, nullable in cur.fetchall():
            null = "" if nullable == "YES" else "  NOT NULL"
            print(f"  {name:<20} {typ}{null}")

        print("\nindexes:")
        cur.execute("SHOW INDEXES FROM cases")
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]
        seen = {}
        for r in rows:
            d = dict(zip(cols, r))
            seen.setdefault(d["index_name"], []).append(d["column_name"])
        for name, columns in seen.items():
            print(f"  {name:<28} ({', '.join(columns)})")

        cur.execute("SHOW ZONE CONFIGURATION FROM TABLE cases")
        zone = cur.fetchall()[0][1]
        ttl = re.search(r"gc\.ttlseconds = (\d+)", zone)
        if ttl:
            secs = int(ttl.group(1))
            print(f"\nMVCC retention: gc.ttlseconds = {secs} "
                  f"({secs / 86400:.1f} days of AS OF SYSTEM TIME)")


if __name__ == "__main__":
    main()
