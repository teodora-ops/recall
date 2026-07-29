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


def ca_path() -> str | None:
    """Locate the cluster CA, or None if we genuinely can't find one.

    Order matters. The repo-local copy comes first so a fresh clone works
    with no setup at all — including on Vercel's Linux builders, which have
    neither APPDATA nor a home-directory postgresql config.
    """
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", "root.crt"),
        os.getenv("COCKROACH_CA"),
        os.path.join(os.environ["APPDATA"], "postgresql", "root.crt")
        if os.environ.get("APPDATA") else None,
        os.path.join(os.path.expanduser("~"), ".postgresql", "root.crt"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def dsn() -> str:
    """Full connection string, CA cert attached only when TLS verification
    actually needs one."""
    url = os.getenv("COCKROACH_URL")
    if not url:
        sys.exit("COCKROACH_URL not set in .env or environment.")

    if "sslrootcert=" in url:
        return url

    # Only verify-full / verify-ca check the CA against a root. Attaching one
    # to sslmode=disable/require — as a local insecure cluster uses — makes
    # the connection fail on a file that was never relevant.
    if not any(m in url for m in ("sslmode=verify-full", "sslmode=verify-ca")):
        return url

    ca = ca_path()
    if not ca:
        sys.exit(
            "CA cert not found. Looked in:\n"
            "  ./certs/root.crt (shipped with the repo)\n"
            "  $COCKROACH_CA\n"
            "  %APPDATA%\\postgresql\\root.crt (Windows)\n"
            "  ~/.postgresql/root.crt (macOS/Linux)\n"
            "Download it from the CockroachDB Cloud console, or set "
            "COCKROACH_CA to its path."
        )
    return url + ("&" if "?" in url else "?") + "sslrootcert=" + ca.replace("\\", "/")


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
