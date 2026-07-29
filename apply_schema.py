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
    """Split SQL on statement-terminating semicolons.

    Deliberately not a regex. A semicolon inside a line comment, a string
    literal or a dollar-quoted body does not end a statement, and treating it
    as one splits a CREATE TABLE in half and reports a syntax error at EOF
    that points nowhere near the actual text. This walker tracks just enough
    context to tell those apart:

      --  line comment      to end of line
      /* */ block comment   nestable in postgres, so depth-counted
      '...'                 with '' escaping
      $tag$...$tag$         dollar quoting, for future function bodies

    That is what makes it safe to put a `CREATE USER ...; GRANT ...;` block or
    a comment containing a semicolon into schema.sql without the applier
    quietly mangling it.
    """
    out, buf, i, n = [], [], 0, len(sql)
    line_comment = False
    block_depth = 0
    in_string = False
    dollar_tag = None

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_depth:
            if ch == "*" and nxt == "/":
                block_depth -= 1
                buf.append(ch); i += 1; ch = nxt
            elif ch == "/" and nxt == "*":
                block_depth += 1
                buf.append(ch); i += 1; ch = nxt
        elif dollar_tag:
            if sql.startswith(dollar_tag, i):
                buf.append(sql[i:i + len(dollar_tag)])
                i += len(dollar_tag)
                dollar_tag = None
                continue
        elif in_string:
            if ch == "'":
                in_string = False
        else:
            if ch == "-" and nxt == "-":
                line_comment = True
            elif ch == "/" and nxt == "*":
                block_depth = 1
                buf.append(ch); i += 1; ch = nxt
            elif ch == "'":
                in_string = True
            elif ch == "$":
                m = re.match(r"\$[A-Za-z_0-9]*\$", sql[i:])
                if m:
                    dollar_tag = m.group(0)
                    buf.append(dollar_tag)
                    i += len(dollar_tag)
                    continue
            elif ch == ";":
                out.append("".join(buf))
                buf = []
                i += 1
                continue

        buf.append(ch)
        i += 1

    out.append("".join(buf))

    for raw in out:
        s = raw.strip()
        if not s:
            continue
        # Skip trailing comment-only fragments.
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
