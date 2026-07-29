"""
retention.py — hold every replay-path table at the required MVCC window.

AS OF SYSTEM TIME cannot read older than gc.ttlseconds, so the replay feature
is only as good as the retention on the tables it reads. A new table picks up
the cluster default (4500s / 75 minutes) unless someone remembers to configure
it, and that failure is invisible until the history is old enough to have been
collected — i.e. during judging, not during the build.

So this is a check, not just a setter. Run it in the pre-submission pass.

    python retention.py           report every table's window
    python retention.py --apply   raise anything below target up to target

Usage note: raising the window keeps more MVCC garbage per table. On a corpus
this size that is immaterial; on a hot, heavily-updated table it would not be.
"""

import re
import sys

import db

# 90 days. Sized off the judging window (19 Aug – 15 Sep 2026), not the build
# schedule: a judge on the final day must be able to replay a decision seeded
# in mid-August, which is 30+ days back.
TARGET = 7776000

# Tables the replay path reads. Every one of these must hold the full window:
# a decision is only reconstructable if the cases it recalled, the order it
# acted on and the customer it concerned are all still readable at that
# timestamp. One short table silently truncates the whole feature.
REPLAY_TABLES = ["cases", "customers", "orders", "decisions", "actions"]


def current_ttl(cur, table: str) -> int | None:
    cur.execute(f'SHOW ZONE CONFIGURATION FROM TABLE "{table}"')
    zone = cur.fetchall()[0][1]
    m = re.search(r"gc\.ttlseconds = (\d+)", zone)
    return int(m.group(1)) if m else None


def main():
    db.safe_console()
    apply = "--apply" in sys.argv

    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
        present = [r[0] for r in cur.fetchall()]

        missing = [t for t in REPLAY_TABLES if t not in present]
        unlisted = [t for t in present if t not in REPLAY_TABLES]

        short = []
        print(f"target: gc.ttlseconds = {TARGET} ({TARGET / 86400:.0f} days)")
        print("-" * 68)

        for table in present:
            ttl = current_ttl(cur, table)
            listed = table in REPLAY_TABLES
            tag = "replay" if listed else "      "
            days = f"{ttl / 86400:.1f}d" if ttl else "inherited"

            if listed and (ttl is None or ttl < TARGET):
                if apply:
                    cur.execute(
                        f'ALTER TABLE "{table}" CONFIGURE ZONE USING '
                        f"gc.ttlseconds = {TARGET}"
                    )
                    new = current_ttl(cur, table)
                    ok = new == TARGET
                    print(f"[{' OK ' if ok else 'FAIL'}]  {tag} {table:<22} "
                          f"{days} -> {new / 86400:.0f}d")
                    if not ok:
                        short.append(table)
                else:
                    print(f"[WARN]  {tag} {table:<22} {days}  (below target)")
                    short.append(table)
            else:
                print(f"[{' OK ' if listed else '    '}]  {tag} {table:<22} {days}")

        print("-" * 68)
        for t in unlisted:
            print(f"note: '{t}' exists but is not listed in REPLAY_TABLES — "
                  f"add it if replay reads it")

        # A missing replay table is a failure, not a note.
        #
        # This check exists to catch a table that quietly inherited the
        # 75-minute default. A run that passes while one of those tables does
        # not exist at all is worse than no check: it reports "all replay
        # tables hold the required window" about a schema that cannot support
        # replay. That is precisely the false green a pre-submission sweep
        # must never produce.
        for t in missing:
            print(f"[FAIL]  replay table '{t}' is listed but DOES NOT EXIST")

        if missing or short:
            if short:
                print(f"\n{len(short)} replay table(s) below target: "
                      f"{', '.join(short)}")
                if not apply:
                    print("re-run with --apply to raise them")
            if missing:
                print(f"{len(missing)} replay table(s) missing: "
                      f"{', '.join(missing)}")
                print("run apply_schema.py before trusting this check")
            sys.exit(1)
        print("\nAll replay tables exist and hold the required window.")


if __name__ == "__main__":
    main()
