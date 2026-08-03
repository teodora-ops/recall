"""
create_reader.py — the read-only SQL user the public UI and MCP server use.

Two reasons this exists, and the second is the important one:

1. The deployed replay page is public. Running it as `recall_admin` would put
   full DDL and write access behind an unauthenticated URL.

2. Replay reads history with AS OF SYSTEM TIME. A reader that can also write
   could change the very past it is meant to be reporting on — the audit trail
   and the thing auditing it would share a credential.

Role DDL lives here rather than in schema.sql deliberately: the applier splits
schema.sql into statements, and a CREATE USER ... ; GRANT ... block is exactly
the shape that used to break it. Keeping roles out of that path removes the
risk entirely.

    python create_reader.py --password '<pw>'     create or update
    python create_reader.py --verify              prove it cannot write

The password is never printed and never written to a file. Put the resulting
connection string in Vercel yourself.
"""

import argparse
import secrets
import sys

import psycopg

import db

READER = "recall_reader"

# Exactly what replay.py and the UI read. Nothing else.
READABLE = ["cases", "customers", "orders", "decisions", "actions"]


def create(password: str) -> None:
    with db.connect() as conn:
        cur = conn.cursor()

        cur.execute(f"SELECT count(*) FROM [SHOW USERS] WHERE username = %s",
                    (READER,))
        exists = cur.fetchone()[0] > 0

        # CREATE USER cannot take a placeholder for the password on all
        # versions, so use the ALTER form which does.
        if exists:
            cur.execute(f"ALTER USER {READER} WITH PASSWORD %s", (password,))
            print(f"[ OK ]   password updated for {READER}")
        else:
            cur.execute(f"CREATE USER {READER} WITH PASSWORD %s", (password,))
            print(f"[ OK ]   created {READER}")

        cur.execute(f"GRANT CONNECT ON DATABASE defaultdb TO {READER}")
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {READER}")
        for t in READABLE:
            cur.execute(f"GRANT SELECT ON TABLE {t} TO {READER}")
        print(f"[ OK ]   SELECT granted on {', '.join(READABLE)}")

        # Belt and braces: revoke anything a default may have handed over.
        for t in READABLE:
            cur.execute(f"REVOKE INSERT, UPDATE, DELETE ON TABLE {t} "
                        f"FROM {READER}")
        print(f"[ OK ]   INSERT/UPDATE/DELETE revoked")

        # The CREATE privilege does NOT come from this user's own grants, so
        # revoking it from the user does nothing. It is held by the `public`
        # pseudo-role, which every account inherits:
        #
        #   SHOW GRANTS ON SCHEMA public
        #     ('defaultdb','public','public','CREATE',False)   <- here
        #
        # It has to be revoked from `public` itself. This is a database-wide
        # change and the standard hardening step; admin accounts keep CREATE
        # through their ALL grant, so recall_admin is unaffected.
        cur.execute("REVOKE CREATE ON SCHEMA public FROM public")
        cur.execute(f"REVOKE CREATE ON SCHEMA public FROM {READER}")
        print(f"[ OK ]   CREATE on schema public revoked (from the public role)")

        cur.execute("SELECT current_database()")
        print(f"\nConnection string for Vercel (fill in the password):")
        print(f"  postgresql://{READER}:<password>@<host>:26257/"
              f"{cur.fetchone()[0]}?sslmode=verify-full")


def verify(password: str) -> int:
    """Connect AS the reader and prove the boundary holds."""
    base = db.dsn()
    # Swap the credentials in the existing DSN rather than rebuilding it.
    at = base.index("@")
    scheme = base[:base.index("://") + 3]
    reader_dsn = f"{scheme}{READER}:{password}{base[at:]}"

    failures = 0
    with psycopg.connect(reader_dsn, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("SELECT current_user")
        print(f"connected as: {cur.fetchone()[0]}")
        print("-" * 62)

        # Reads that must work.
        for sql, label in [
            ("SELECT count(*) FROM cases", "SELECT cases"),
            ("SELECT count(*) FROM decisions", "SELECT decisions"),
            ("SELECT count(*) FROM orders", "SELECT orders"),
        ]:
            try:
                cur.execute(sql)
                print(f"[ OK ]   {label:<34} {cur.fetchone()[0]} rows")
            except Exception as e:
                failures += 1
                print(f"[FAIL]   {label} -> {str(e).splitlines()[0][:50]}")

        # Replay's own access pattern must work for the reader too.
        try:
            cur.execute("SELECT decision_hlc FROM decisions LIMIT 1")
            hlc = cur.fetchone()
            if hlc:
                import txn
                ts = txn.read_snapshot(hlc[0])
                cur.execute(f"SELECT count(*) FROM cases "
                            f"AS OF SYSTEM TIME {ts}")
                print(f"[ OK ]   {'AS OF SYSTEM TIME read':<34} "
                      f"{cur.fetchone()[0]} rows")
        except Exception as e:
            msg = str(e).splitlines()[0]
            if "concurrently dropped" in msg or "does not exist" in msg:
                # Not a permissions problem. CockroachDB resolves role identity
                # at the historical timestamp, so a role cannot read a snapshot
                # from before it was created — the role genuinely did not exist
                # in that past.
                failures += 1
                print(f"[FAIL]   {'AS OF SYSTEM TIME read':<34} role predates "
                      f"the snapshot")
                print("         The reader was created AFTER these decisions.")
                print("         Create the reader BEFORE recording anything")
                print("         you intend to replay, then re-run the demos.")
            else:
                failures += 1
                print(f"[FAIL]   AS OF SYSTEM TIME -> {msg[:50]}")

        print("-" * 62)
        # Writes that must NOT work. A pass here is a security failure.
        #
        # The CREATE probe uses a fresh table name every run. A fixed name
        # passes for the wrong reason once the table exists: the refusal is
        # then 42P07 (duplicate_table), not 42501 (insufficient_privilege),
        # and the check silently stops testing permissions at all.
        probe_table = f"_perm_probe_{secrets.token_hex(4)}"
        for sql, label, want in [
            ("UPDATE orders SET refunded_minor = 0", "UPDATE orders", "42501"),
            ("DELETE FROM decisions", "DELETE decisions", "42501"),
            ("INSERT INTO cases (channel, customer_ref, body) "
             "VALUES ('email','x','y')", "INSERT cases", "42501"),
            ("DROP TABLE cases", "DROP TABLE cases", "42501"),
            (f"CREATE TABLE {probe_table} (a INT)", "CREATE TABLE", "42501"),
        ]:
            try:
                cur.execute(sql)
                failures += 1
                print(f"[LEAK]   {label:<34} SUCCEEDED — it should not have")
            except Exception as e:
                code = getattr(e, "sqlstate", "") or "?"
                if code == want:
                    print(f"[ OK ]   {label:<34} refused ({code})")
                else:
                    # Refused, but not for the reason we are testing.
                    failures += 1
                    print(f"[FAIL]   {label:<34} refused with {code}, "
                          f"expected {want} (insufficient privilege)")

    print("-" * 62)
    if failures:
        print(f"{failures} PROBLEM(S). Do not put this user behind a public URL.")
        return 1
    print("Reader can read history and cannot change it.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--password", help="password to set for the reader")
    ap.add_argument("--verify", action="store_true",
                    help="connect as the reader and test the boundary")
    args = ap.parse_args()

    db.safe_console()
    if not args.password:
        sys.exit("--password is required (it is never printed or stored)")

    if args.verify:
        sys.exit(verify(args.password))
    create(args.password)
    print("\nNow re-run with --verify to prove the boundary holds.")


if __name__ == "__main__":
    main()
