-- Recall — schema, week 1.
--
-- Scope: the cases corpus and its distributed vector index. The decision /
-- action tables that carry the serializable-race and replay demos land next;
-- this file is shaped so they attach to it without a rewrite.
--
-- Verified against CockroachDB CCL v26.2.1, cluster `recall`, eu-west-2,
-- 28 Jul 2026. VECTOR(1024) is indexable here — see vector_index_check.py.

CREATE TABLE IF NOT EXISTS cases (
    case_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Which front door the customer came through. The whole point of Recall
    -- is that these three share one memory, so channel is a plain attribute,
    -- deliberately NOT a partition key and NOT a vector index prefix.
    channel           STRING NOT NULL,
    customer_ref      STRING NOT NULL,

    subject           STRING,
    body              STRING NOT NULL,

    -- How it ended. NULL resolution = still open, and an open case is still
    -- worth retrieving as context, so this is nullable by design.
    resolution        STRING,
    outcome           STRING,

    opened_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at       TIMESTAMPTZ,

    -- Titan Text Embeddings v2, 1024 dims, L2-normalised at write time.
    embedding         VECTOR(1024),

    -- Which model produced the vector, and a fingerprint of the exact text
    -- fed to it. Together these make re-embedding idempotent: the pipeline
    -- can tell "already embedded" from "embedded by a model we've since
    -- swapped out" without re-reading the corpus into Bedrock.
    embed_model       STRING,
    embed_fingerprint STRING,
    embedded_at       TIMESTAMPTZ,

    CONSTRAINT channel_known
        CHECK (channel IN ('email', 'whatsapp', 'webchat')),
    CONSTRAINT resolved_cases_have_a_resolution
        CHECK ((resolved_at IS NULL) = (resolution IS NULL))
);

-- The distributed (C-SPANN) vector index.
--
-- No prefix column. A prefixed index (channel, embedding) would scope each
-- search to one channel, which is exactly the siloing Recall exists to
-- remove — an email case must be able to retrieve a WhatsApp resolution.
--
-- Default opclass is L2. Because the pipeline normalises every vector before
-- it is written, L2 ordering and cosine ordering are identical, so queries
-- use <-> and still rank by semantic similarity.
CREATE VECTOR INDEX IF NOT EXISTS cases_embedding_idx ON cases (embedding);

-- Ordinary lookups: a customer's own history, and the pipeline's "what still
-- needs embedding" sweep.
CREATE INDEX IF NOT EXISTS cases_customer_idx ON cases (customer_ref, opened_at DESC);
CREATE INDEX IF NOT EXISTS cases_unembedded_idx ON cases (embedded_at) WHERE embedding IS NULL;

-- MVCC retention.
--
-- This is load-bearing for the headline feature, not a tuning knob. AS OF
-- SYSTEM TIME cannot read older than the GC window, and this cluster's
-- default range is 4500s — 75 minutes. A replay demo of a decision made
-- yesterday would fail outright against that default.
--
-- The default range itself is not alterable by a non-root user on Cloud
-- Basic, but per-table zone configs are, which is all we need.
--
-- 90 days, sized off the judging window rather than the build: judging runs
-- 19 Aug – 15 Sep 2026, and a judge opening the replay UI on the last day
-- must still be able to reconstruct a decision seeded in mid-August. That is
-- 30+ days of history, so anything shorter breaks the headline feature
-- silently and late, in front of the people scoring it.
--
-- EVERY table the replay path reads needs this. Tables inherit the 75-minute
-- default otherwise, and the failure only shows up once the history is old
-- enough to have been collected — long after the code looks finished.
ALTER TABLE cases CONFIGURE ZONE USING gc.ttlseconds = 7776000;
