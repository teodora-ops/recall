# Recall

**A shared agentic memory layer on CockroachDB.**

A small business runs customer support across three channels — email, WhatsApp
and web chat — with a different AI agent on each. Normally that means three
siloed memories: the WhatsApp agent has no idea the customer already emailed
about the same broken order, and cheerfully offers a second refund.

Recall gives that fleet **one** memory store instead of three.

The thesis is not "CockroachDB is a place to put embeddings." Plenty of
databases will hold a vector. The claim is that CockroachDB is what makes agent
memory *correct*: transactional, survivable, and reconstructable after the
fact. Two of the four capabilities below do not survive being ported to
Postgres + pgvector, and those are the two the project is built around.

> Status: **week 1, in progress.** The corpus schema, the distributed vector
> index and the embedding pipeline are live and verified against the cluster.
> The agent, the three demos and the replay UI are not built yet. The
> [Evidence](#evidence) section records only what has actually been run.

---

## The four capabilities

| # | Capability | What it demonstrates | Status |
|---|---|---|---|
| 1 | **Semantic recall** | Past cases embedded into a `VECTOR(1024)` column with a distributed C-SPANN index. A new case retrieves the closest historical resolutions as context — across channels, so an email case can surface a WhatsApp resolution. | Schema + pipeline verified |
| 2 | **Transactional decisions** | The agent's decision and the action it authorises commit in a single serializable transaction. Two agents race the same refund; the second aborts, retries, sees the refund already happened, and does not double-pay. | Not started |
| 3 | **Point-in-time replay** | `AS OF SYSTEM TIME` reconstructs exactly what the agent knew at the moment of any past decision, plus a diff of what changed since. *"Why did the bot offer that discount?"* — **the headline feature.** | Mechanism verified, UI not built |
| 4 | **Survivability** | Kill a node mid-conversation; the agent keeps its memory and keeps going. | Not started |

Capabilities 2 and 3 are the ones that cannot be swapped onto another
database. They get protected ahead of everything else.

---

## Architecture

```
   email ─┐
whatsapp ─┼─→  agent (AWS Lambda)  ─→  Bedrock Nova Pro      (reasoning)
 webchat ─┘          │                 Bedrock Titan v2      (embeddings, 1024d)
                     │
                     ↓
              CockroachDB Cloud  ── cases + C-SPANN vector index
              (eu-west-2, v26.2.1)   decisions + actions  [to come]
                     │
                     ├─→  AS OF SYSTEM TIME  ──→  replay UI
                     └─→  managed MCP server (read-only)  ──→  analyst access
```

| Layer | Choice |
|---|---|
| Database | CockroachDB Cloud (Basic), cluster `recall`, v26.2.1 |
| Region | eu-west-2 (London), everything |
| Reasoning | Amazon Bedrock, Nova Pro, via the Converse API |
| Embeddings | Amazon Bedrock, Titan Text Embeddings v2, 1024 dims |
| Artifacts | S3 |
| Execution | AWS Lambda |
| Analyst access | CockroachDB managed MCP server, read-only |

The Converse API is used specifically so the reasoning model is swappable by
config rather than by rewrite. No model ID is hardcoded anywhere; both are read
from `.env`.

### Repository layout

| File | |
|---|---|
| `schema.sql` | The `cases` corpus, the vector index, and MVCC retention |
| `db.py` | Connection helper — joins the cluster URL to the local CA cert |
| `embeddings.py` | Titan v2 embeddings with a mandatory on-disk cache |
| `recall.py` | The pipeline: `ingest_case` / `backfill` / `search` |
| `apply_schema.py` | Idempotent schema applier; prints what landed |
| `retention.py` | Holds every replay-path table at the required MVCC window |
| `vector_index_check.py` | Probes whether the cluster indexes `VECTOR(1024)` |
| `verify_pipeline.py` | Self-cleaning end-to-end check of the whole path |

---

## Setup

**Requirements:** Python 3.11+, a CockroachDB Cloud cluster, an AWS account
with Bedrock access in `eu-west-2`.

```bash
git clone https://github.com/teodora-ops/recall.git
cd recall
pip install "psycopg[binary]" boto3 python-dotenv
```

Download your cluster's CA certificate from the CockroachDB Cloud console. On
Windows it belongs at `%APPDATA%\postgresql\root.crt`; on macOS and Linux at
`~/.postgresql/root.crt`. `db.py` locates it automatically, which is why the
connection string does not carry an `sslrootcert` parameter.

Create a `.env` in the repository root:

```ini
AWS_REGION=eu-west-2
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
S3_BUCKET=...

# Bare model IDs. NOT the eu.* cross-region inference profile — that returns
# ValidationException: The provided model identifier is invalid on this
# account, and most Bedrock documentation assumes the profile form.
BEDROCK_CHAT_MODEL=amazon.nova-pro-v1:0
BEDROCK_EMBED_MODEL=amazon.titan-embed-text-v2:0

COCKROACH_URL=postgresql://<user>:<password>@<host>:26257/defaultdb?sslmode=verify-full
```

`.env` is gitignored and must stay that way.

Then verify the account and the cluster before creating anything:

```bash
python bedrock_check.py         # Bedrock reachable? which model IDs work?
python vector_index_check.py    # does this cluster index VECTOR(1024)?
python apply_schema.py          # create cases + index + retention
python retention.py             # confirm the MVCC window on replay tables
python verify_pipeline.py       # end-to-end, self-cleaning
```

Then use it:

```bash
python recall.py backfill
python recall.py search "you took the money off my card two times"
```

### A note on embedding cost

Every vector Bedrock returns is cached to `.cache/embeddings/`, keyed by
`sha256(model_id | text)`. Re-embedding the corpus on each dev restart is the
one realistic way to overspend on a project this size, so nothing is ever paid
for twice. Keying on the model as well as the text means swapping
`BEDROCK_EMBED_MODEL` invalidates the cache automatically rather than silently
serving vectors from the old model.

---

## Evidence

> Output pasted here is real output from a run that actually happened, added at
> the time the demo ran rather than assembled before submission. Each entry
> says why the output proves the claim, because raw output nobody reads is
> weaker than no entry.

### 1. The distributed vector index accepts `VECTOR(1024)` — and is actually used

The open question before any seeding was not whether CockroachDB would *store*
a 1024-dimension vector, but whether the distributed index would accept that
width and whether the planner would then choose it. If the index had rejected
1024, the fallback was to drop Titan to 512 dims — a decision that had to be
made while the table was empty, because changing it afterwards means
re-embedding the entire corpus.

Run against cluster `recall`, CockroachDB CCL **v26.2.1**, eu-west-2, 28 Jul 2026.

The index form this cluster accepts:

```sql
CREATE VECTOR INDEX cases_embedding_idx ON cases (embedding);
```

`EXPLAIN` for a nearest-neighbour query on the live `cases` table:

```
EXPLAIN SELECT case_id FROM cases
ORDER BY embedding <-> $1::VECTOR(1024) LIMIT 3;

distribution: local

• top-k
│ estimated row count: 1
│ order: +column18
│ k: 3
│
└── • render
    │
    └── • lookup join
        │ table: cases@cases_pkey
        │ equality: (case_id) = (case_id)
        │ equality cols are key
        │
        └── • vector search
              table: cases@cases_embedding_idx
              target count: 3
```

**Reading it bottom-up — why this is index access and not a scan:**

1. **`• vector search → table: cases@cases_embedding_idx`** is the leaf, and it
   is the whole proof. This node is C-SPANN index access: it descends the
   index and returns `target count: 3` approximate nearest candidates. A
   fallback plan would have no `vector search` node at all — it would show a
   table scan over `cases@cases_pkey` feeding a sort.
2. **`• lookup join` against `cases@cases_pkey`** exists *because* step 1 was an
   index read. The vector index stores the vector and the primary key, not the
   rest of the row, so the plan joins back to the PK to fetch the remaining
   columns. A full scan would already have every column and would need no such
   join — its presence is corroborating evidence of index access.
3. **`• top-k` at the apex** is a re-rank of the small candidate set from step 1
   by exact distance, not a sort of the table. Note `estimated row count: 1`:
   the planner expects a handful of rows arriving here, not the corpus. This
   node is what makes an approximate index result exact at the top of the list.

Two things worth stating because they affect how far this evidence goes:

- An earlier version of the checking script reported this as passing by
  matching the substring `"vector"` in the plan text — which also appears in
  the column type and the `ORDER BY` expression. That was a false positive on a
  plan nobody had read. The check now requires the *index name* to appear and
  `FULL SCAN` to be absent. The plan above satisfies the stricter check.
- The plan was captured with only 4 rows in `cases`. Small tables can
  legitimately plan as full scans, so this must be re-confirmed once real seed
  data lands; the row count that matters for the demo is not 4.

The width is separately confirmed to be enforced rather than decorative:

```
[ OK ]   column is declared VECTOR(1024)
         -> embedding VECTOR(1024) NULL,
[ OK ]   512-dim vector is rejected
         -> rejected: expected 1024 dimensions, not 512
[ OK ]   Bedrock returns 1024 dims
         -> 1024 dims from amazon.titan-embed-text-v2:0
```

**Verdict: `VECTOR(1024)` stands. No drop to 512.**

### 2. Cross-channel retrieval

The point of one shared memory is that the channel a question arrives on does
not determine which memories it can reach. Querying with a phrase written like
a chat message, against a corpus of four cases spanning all three channels:

```
query: "you took the money off my card two times"

[1.1099] webchat   Charged twice for the same order
[1.3523] whatsapp  still waiting on my parcel
[1.3664] email     Order #4471 arrived damaged
```

The nearest case is the duplicate-charge one, retrieved **from a different
channel than the query's phrasing implies**, and it wins by a clear margin
(1.11 vs 1.35) rather than by a hair. The vector index carries no `channel`
prefix column precisely so this works; prefixing it would re-create the silos
the project exists to remove.

### 3. Point-in-time replay reads deleted history

The retention setting is only meaningful if `AS OF SYSTEM TIME` genuinely
reconstructs the past on these tables. Inserting a case, capturing
`cluster_logical_timestamp()`, deleting it, then reading at that timestamp:

```
inserted, logical ts captured
rows now (present): 0
rows visible AS OF that ts: 1
   -> Refund promised on a call | Agent said 20 percent off next order.
final row count: 0
```

The row is gone at present time and fully reconstructed at the captured
timestamp. This is the mechanism the whole replay feature rests on, working on
the real table rather than in principle.

**The retention window is load-bearing and easy to get wrong.** This cluster's
default range is `gc.ttlseconds = 4500` — 75 minutes. `AS OF SYSTEM TIME`
cannot read older than that window, so a replay of yesterday's decision would
simply fail. `ALTER RANGE default` is refused to non-root users on Cloud Basic,
but per-table zone configs are permitted, which is sufficient:

```
target: gc.ttlseconds = 7776000 (90 days)
--------------------------------------------------------------------
[ OK ]  replay cases                  90.0d
--------------------------------------------------------------------
All replay tables hold the required window.
```

90 days is sized off the **judging window (19 Aug – 15 Sep 2026)**, not the
build schedule: a judge opening the replay UI on the final day must still
reconstruct a decision seeded in mid-August. Every table the replay path reads
needs its own config — new tables inherit the 75-minute default, and that
failure only surfaces once history is old enough to have been collected, which
is to say during judging rather than during the build. `retention.py` exists to
catch exactly that and exits non-zero if any replay table is below target.

### 4. Serializable race — two agents, one refund

*Not yet run. Output goes here when it is.*

### 5. Node kill — survivability

*Not yet run. Output goes here when it is.*

### 6. Replay diff — what the agent knew, and what changed since

*Not yet run. Output goes here when it is.*

---

## Licence

MIT — see [LICENSE](LICENSE).

Built by Flux AI Consulting Ltd for the CockroachDB × AWS "Build with Agentic
Memory" hackathon, 2026.
