"""
verify_pipeline.py — prove the whole path works before anyone seeds it.

Checks, in order:
  1. the column really is VECTOR(1024), not an unbounded VECTOR
  2. a wrong-width vector is rejected rather than silently stored
  3. Bedrock returns 1024 dims and the disk cache actually absorbs a re-embed
  4. ingest -> search returns semantically sensible neighbours
  5. the query plan uses the vector index rather than a full scan

Self-cleaning: every row it writes is removed again, so the corpus is left
empty and ready to seed.

Usage:  python verify_pipeline.py
"""

import sys

import db
import embeddings
import recall

MARKER = "verify-pipeline-throwaway"

# Realistic text, because this doubles as a retrieval sanity check and
# nonsense strings would not exercise the embedding meaningfully.
FIXTURES = [
    ("email", "Order #4471 arrived damaged",
     "The mug I ordered turned up cracked down one side. Box was fine, so I "
     "think it went out that way. Can I get a replacement?",
     "Sent a replacement mug free of charge, no return required.", "replacement"),
    ("whatsapp", "still waiting on my parcel",
     "hi, ordered on the 4th and tracking hasn't moved in a week. is it lost?",
     "Opened a carrier trace, refunded postage as a goodwill gesture.", "goodwill_refund"),
    ("webchat", "Charged twice for the same order",
     "I've been billed 34.98 twice for order 4502. Only ordered once.",
     "Confirmed duplicate authorisation, refunded the second charge in full.", "refund"),
    ("email", "Wrong size delivered",
     "Ordered a large apron, a small one arrived. Happy to swap.",
     "Sent correct size with a prepaid return label.", "exchange"),
]


def check(label, fn):
    try:
        ok, detail = fn()
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL]   {label}")
        print(f"         -> {type(e).__name__}: {str(e).strip().splitlines()[0]}")
        return False
    print(f"[{' OK ' if ok else 'FAIL'}]   {label}")
    if detail:
        for line in str(detail).splitlines():
            print(f"         -> {line}")
    return ok


def main():
    db.safe_console()
    failures = 0

    with db.connect() as conn:
        cur = conn.cursor()

        # 1. Declared width.
        def declared_width():
            cur.execute("SHOW CREATE TABLE cases")
            ddl = cur.fetchone()[1]
            line = next(l.strip() for l in ddl.splitlines() if "embedding" in l
                        and "VECTOR" in l.upper())
            return "VECTOR(1024)" in line.upper().replace(" ", ""), line

        failures += not check("column is declared VECTOR(1024)", declared_width)

        # 2. Wrong width must be rejected. If this passes silently, the
        #    dimension is decorative and the whole verification is worthless.
        def rejects_wrong_width():
            bad = "[" + ",".join(["0.1"] * 512) + "]"
            try:
                cur.execute(
                    "INSERT INTO cases (channel, customer_ref, body, embedding) "
                    "VALUES ('email', %s, 'width probe', %s::VECTOR(512))",
                    (MARKER, bad),
                )
            except Exception as e:  # noqa: BLE001
                return True, f"rejected: {str(e).strip().splitlines()[0]}"
            return False, "ACCEPTED a 512-dim vector into a VECTOR(1024) column"

        failures += not check("512-dim vector is rejected", rejects_wrong_width)

    # 3. Bedrock width + cache behaviour.
    def bedrock_width():
        v = embeddings.embed("a customer says their order never arrived")
        return len(v) == embeddings.DIMS, f"{len(v)} dims from {embeddings.model_id()}"

    failures += not check("Bedrock returns 1024 dims", bedrock_width)

    def cache_works():
        before = embeddings.cache_stats()[0]
        embeddings.embed("a customer says their order never arrived")
        after = embeddings.cache_stats()[0]
        return after == before, f"cached files {before} -> {after} (no new call)"

    failures += not check("re-embed is served from disk cache", cache_works)

    # 4 & 5. Ingest, search, and inspect the plan.
    ids = []
    try:
        for channel, subject, body, resolution, outcome in FIXTURES:
            ids.append(recall.ingest_case(
                channel=channel, customer_ref=MARKER, subject=subject,
                body=body, resolution=resolution, outcome=outcome,
            ))
        print(f"[ OK ]   ingested {len(ids)} case(s) through the pipeline")

        def retrieval_is_sane():
            # A billing complaint should pull back the duplicate-charge case
            # first — and it should do so from a different channel than the
            # query implies, which is the cross-channel behaviour Recall is for.
            hits = recall.search("you took the money off my card two times", k=3)
            lines = [f"[{h['distance']:.4f}] {h['channel']:<9} {h['subject']}"
                     for h in hits]
            top = hits[0]["subject"] if hits else ""
            return "twice" in top.lower(), "\n".join(lines)

        failures += not check("nearest neighbour is the duplicate-charge case",
                              retrieval_is_sane)

        def plan_uses_index():
            vec = embeddings.embed("you took the money off my card two times")
            with db.connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "EXPLAIN SELECT case_id FROM cases "
                    "ORDER BY embedding <-> %s::VECTOR(1024) LIMIT 3",
                    (embeddings.to_sql(vec),),
                )
                plan = "\n".join(r[0] for r in cur.fetchall())
            low = plan.lower()
            ok = "cases_embedding_idx" in low and "full scan" not in low
            return ok, plan

        failures += not check("query plan uses cases_embedding_idx", plan_uses_index)

    finally:
        with db.connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM cases WHERE customer_ref = %s", (MARKER,))
            removed = cur.rowcount
            cur.execute("SELECT count(*) FROM cases")
            remaining = cur.fetchone()[0]
        print("-" * 68)
        print(f"cleanup: removed {removed} throwaway row(s); "
              f"cases now holds {remaining} row(s)")

    files, size = embeddings.cache_stats()
    print(f"embedding cache: {files} vector(s), {size / 1024:.0f} KB")

    print("-" * 68)
    if failures:
        print(f"{failures} CHECK(S) FAILED — do not seed yet.")
        sys.exit(1)
    print("All checks passed. VECTOR(1024) pipeline is live and the corpus")
    print("is empty and ready to seed.")


if __name__ == "__main__":
    main()
