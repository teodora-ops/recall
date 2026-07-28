"""
db.py — one place that knows how to reach the cluster.

The COCKROACH_URL in .env carries sslmode=verify-full but not the CA path,
because the CA lives at a per-machine location. This module joins the two so
no other file has to think about it.
"""

import os
import sys

import psycopg

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def dsn() -> str:
    """Full connection string, CA cert included."""
    url = os.getenv("COCKROACH_URL")
    if not url:
        sys.exit("COCKROACH_URL not set in .env or environment.")
    if "sslrootcert=" not in url:
        ca = os.path.join(os.environ.get("APPDATA", ""), "postgresql", "root.crt")
        if not os.path.exists(ca):
            sys.exit(
                f"CA cert not found at {ca}\n"
                "Download it from the CockroachDB Cloud console."
            )
        url += ("&" if "?" in url else "?") + "sslrootcert=" + ca.replace("\\", "/")
    return url


def connect(autocommit: bool = True) -> psycopg.Connection:
    """Open a connection. autocommit=False when you want to drive the txn."""
    return psycopg.connect(dsn(), autocommit=autocommit)


def safe_console() -> None:
    """CockroachDB draws EXPLAIN output with box characters that the default
    Windows console codepage cannot encode."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
