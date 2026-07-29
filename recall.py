"""
recall.py — the embedding pipeline: text in, vector on a row, similar cases out.

Three entry points:

    ingest_case(...)   embed a case and write it in one statement
    backfill()         embed any row whose vector is missing or stale
    search(text, k)    nearest historical cases to a new message

The text that gets embedded is composed once, in embedding_text(), and its
fingerprint is stored on the row. Everything else keys off that: backfill
re-embeds exactly the rows whose fingerprint no longer matches, which is what
makes a model swap a one-command operation instead of a corpus rebuild.
"""

import sys
from typing import Iterable

import db
import embeddings

CHANNELS = ("email", "whatsapp", "webchat")


def embedding_text(subject: str | None, body: str) -> str:
    """What actually goes to Titan.

    *** FROZEN as of 29 Jul 2026, before the corpus was seeded. ***

    Do not change the composition. Every row stores an embed_fingerprint of
    sha256(model_id | this text); altering the format invalidates the
    fingerprint on every case at once, so backfill() would re-embed the entire
    corpus and every cached vector on disk would miss. That is recoverable but
    slow and pointless. If the composition genuinely must change, treat it as
    a corpus migration and say so out loud.

    Subject and body together — a subject line like "charged twice" carries
    most of the signal in short email cases, and dropping it measurably
    flattens retrieval. Resolution is deliberately excluded: at search time a
    new case has no resolution yet, so including it here would embed the
    corpus and the queries into different spaces.
    """
    subject = (subject or "").strip()
    body = (body or "").strip()
    return f"{subject}\n\n{body}".strip() if subject else body


def ingest_case(channel: str, customer_ref: str, body: str,
                subject: str | None = None, resolution: str | None = None,
                outcome: str | None = None, resolved: bool = False,
                conn=None) -> str:
    """Embed and insert one case. Returns the new case_id."""
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}, got {channel!r}")

    # resolved=True with no resolution text would set resolved_at while
    # leaving resolution NULL, violating resolved_cases_have_a_resolution.
    # Refuse it here rather than letting the seeder discover it as a constraint
    # error several hundred rows in.
    if resolved and resolution is None:
        raise ValueError(
            "a resolved case needs resolution text — "
            "pass resolution=..., or leave resolved=False for an open case"
        )

    text = embedding_text(subject, body)
    vec = embeddings.embed(text)
    model = embeddings.model_id()
    fp = embeddings.fingerprint(text, model)

    owns = conn is None
    conn = conn or db.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO cases (channel, customer_ref, subject, body,
                               resolution, outcome, resolved_at,
                               embedding, embed_model, embed_fingerprint,
                               embedded_at)
            VALUES (%s, %s, %s, %s, %s, %s,
                    CASE WHEN %s THEN now() ELSE NULL END,
                    %s::VECTOR(1024), %s, %s, now())
            RETURNING case_id
            """,
            (channel, customer_ref, subject, body, resolution, outcome,
             resolved or resolution is not None,
             embeddings.to_sql(vec), model, fp),
        )
        return str(cur.fetchone()[0])
    finally:
        if owns:
            conn.close()


def backfill(limit: int | None = None, progress: bool = True) -> int:
    """Embed every case whose vector is missing or was made by another model.

    Cheap to run repeatedly — the disk cache means a re-run after a crash
    costs nothing at Bedrock.
    """
    model = embeddings.model_id()
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT case_id, subject, body
            FROM cases
            WHERE embedding IS NULL
               OR embed_model IS DISTINCT FROM %s
               OR embed_fingerprint IS NULL
            ORDER BY opened_at
            {"LIMIT %s" if limit else ""}
            """,
            (model, limit) if limit else (model,),
        )
        todo = cur.fetchall()

        if not todo:
            if progress:
                print("nothing to embed — corpus is current")
            return 0

        if progress:
            print(f"embedding {len(todo)} case(s) with {model}")

        done = 0
        for case_id, subject, body in todo:
            text = embedding_text(subject, body)
            vec = embeddings.embed(text)
            cur.execute(
                """
                UPDATE cases
                   SET embedding = %s::VECTOR(1024),
                       embed_model = %s,
                       embed_fingerprint = %s,
                       embedded_at = now()
                 WHERE case_id = %s
                """,
                (embeddings.to_sql(vec), model,
                 embeddings.fingerprint(text, model), case_id),
            )
            done += 1
            if progress and (done % 25 == 0 or done == len(todo)):
                print(f"  {done}/{len(todo)}")
        return done


def search(text: str, k: int = 5, channel: str | None = None,
           resolved_only: bool = False, conn=None) -> list[dict]:
    """Nearest historical cases to a new message.

    channel is an optional post-filter, not the default: an email case
    retrieving a WhatsApp resolution is the behaviour Recall exists to show.

    Distance is <-> (L2). Vectors are normalised at write time, so this ranks
    identically to cosine while matching the index's default opclass — which
    is what keeps the query on the vector index instead of a full scan.
    """
    vec = embeddings.embed(text)

    where, params = [], [embeddings.to_sql(vec)]
    if channel:
        where.append("channel = %s")
        params.append(channel)
    if resolved_only:
        where.append("resolution IS NOT NULL")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(k)

    owns = conn is None
    conn = conn or db.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT case_id, channel, customer_ref, subject, body,
                   resolution, outcome, opened_at,
                   embedding <-> %s::VECTOR(1024) AS distance
            FROM cases
            {clause}
            ORDER BY distance
            LIMIT %s
            """,
            params,
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        if owns:
            conn.close()


def _cli(argv: Iterable[str]) -> None:
    db.safe_console()
    args = list(argv)
    if not args:
        print(__doc__.strip())
        print("\nusage:\n  python recall.py backfill\n"
              "  python recall.py search \"my parcel never arrived\"")
        return
    cmd = args[0]
    if cmd == "backfill":
        n = backfill()
        print(f"embedded {n} case(s)")
    elif cmd == "search":
        if len(args) < 2:
            sys.exit('search needs a query: python recall.py search "..."')
        for hit in search(args[1], k=5):
            print(f"[{hit['distance']:.4f}] {hit['channel']:<9} "
                  f"{hit['subject'] or '(no subject)'}")
            if hit["resolution"]:
                print(f"           -> {hit['resolution']}")
    else:
        sys.exit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    _cli(sys.argv[1:])
