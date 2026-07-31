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

**Live:** [recall-memory.vercel.app](https://recall-memory.vercel.app) ·
health check at [`/api/health`](https://recall-memory.vercel.app/api/health)

> Status: **in progress.** Semantic recall works against a seeded 300-case
> corpus, and the deployment path is proven end to end. The agent, the three
> demos and the replay UI are not built yet — the deployed page is currently a
> health check, not the replay UI. The [Evidence](#evidence) section records
> only what has actually been run.

---

## The four capabilities

| # | Capability | What it demonstrates | Status |
|---|---|---|---|
| 1 | **Semantic recall** | Past cases embedded into a `VECTOR(1024)` column with a distributed C-SPANN index. A new case retrieves the closest historical resolutions as context — across channels, so an email case can surface a WhatsApp resolution. | **Working** — 300-case corpus, index use verified at scale |
| 2 | **Transactional decisions** | The agent's decision and the action it authorises commit in a single serializable transaction. Two agents race the same refund; the second aborts, retries, sees the refund already happened, and does not double-pay. | Not started |
| 3 | **Point-in-time replay** | `AS OF SYSTEM TIME` reconstructs exactly what the agent knew at the moment of any past decision, plus a diff of what changed since. *"Why did the bot offer that discount?"* — **the headline feature.** | Mechanism verified, UI not built |
| 4 | **Survivability** | Kill a node mid-conversation; the agent keeps its memory and keeps going. | Not started |

Capabilities 2 and 3 are the ones that cannot be swapped onto another
database. They get protected ahead of everything else.

---

## Architecture

```
   email ─┐
whatsapp ─┼─→  agent (Python)  ─────→  Bedrock Nova Pro      (reasoning)
 webchat ─┘          │                 Bedrock Titan v2      (embeddings, 1024d)
                     │                 S3                    (case artifacts)
                     ↓
              CockroachDB Cloud  ── cases + C-SPANN vector index
              (eu-west-2, v26.2.1)   decisions + actions  [to come]
                     │
                     ├─→  AS OF SYSTEM TIME  ──→  replay UI (Vercel)
                     └─→  managed MCP server (read-only)  ──→  analyst access
```

| Layer | Choice |
|---|---|
| Database | CockroachDB Cloud (Basic), cluster `recall`, v26.2.1 |
| Region | eu-west-2 (London), everything |
| Reasoning | Amazon Bedrock, Nova Pro, via the Converse API |
| Embeddings | Amazon Bedrock, Titan Text Embeddings v2, 1024 dims |
| Artifacts | S3 — case bodies; vectors and pointers stay in CockroachDB |
| Agent execution | Python, run from CLI and from the deployed app |
| Deployed demo | Vercel serverless functions + static frontend |
| Analyst access | CockroachDB managed MCP server, read-only |

Agent execution is plain Python rather than Lambda. The IAM machine user this
project runs under is scoped to Bedrock and S3 only, and on a two-week clock
the deployed replay UI is a hard contest requirement while Lambda is not — so
the deployment budget went to the thing that has to exist. Nothing in the
design depends on where the process runs.

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
| `persona.py` | The fictional business the corpus describes — the human-editable part |
| `seed.py` | Builds the corpus: hand-written hero cases + Nova Pro batches, cached |
| `explain_check.py` | Whether the planner uses the vector index, filtered and unfiltered |
| `spike_replay.py` | The four schema-gating questions, answered against the cluster |
| `vector_index_check.py` | Probes whether the cluster indexes `VECTOR(1024)` |
| `verify_pipeline.py` | Self-cleaning end-to-end check of the whole path |
| `api/`, `public/` | The deployed app |
| `certs/root.crt` | Cluster CA, shipped so a fresh clone needs no setup |

---

## Setup

**Requirements:** Python 3.10+, a CockroachDB Cloud cluster, an AWS account
with Bedrock access in `eu-west-2`.

```bash
git clone https://github.com/teodora-ops/recall.git
cd recall
pip install -r requirements.txt
```

The cluster CA ships in `certs/root.crt`, so there is nothing to download.
`db.py` looks there first, then `$COCKROACH_CA`, then the platform default
(`%APPDATA%\postgresql\root.crt` on Windows, `~/.postgresql/root.crt` on macOS
and Linux) — which is why the connection string carries no `sslrootcert`
parameter. It is attached only when `sslmode` actually verifies, so an insecure
local cluster still connects.

Copy `.env.example` to `.env` and fill it in:

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
python spike_replay.py          # the four questions the schema depends on
python vector_index_check.py    # does this cluster index VECTOR(1024)?
python apply_schema.py          # create every table, index and zone config
python retention.py             # confirm the MVCC window on replay tables
python verify_pipeline.py       # end-to-end, self-cleaning
```

Then build the corpus and use it:

```bash
python seed.py                        # generate only, writes nothing
python seed.py --apply --embed        # ~300 cases, to S3 and CockroachDB
python explain_check.py               # is the planner using the vector index?

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

### 0. The deployment reaches the cluster

Live response from [`/api/health`](https://recall-memory.vercel.app/api/health),
fetched 29 Jul 2026:

```json
{
  "ok": true,
  "ca_resolved": "/var/task/certs/root.crt",
  "cluster": "CockroachDB CCL v26.2.1",
  "cases": 0,
  "hlc": "1785338603511142481.0000000000",
  "vector_index": true,
  "retention_days": 90.0,
  "replay_ok": true,
  "ms": 1225
}
```

**Why each field is there rather than a bare "ok".** A health check that only
proves the process started is worth very little:

- **`ca_resolved`** is the CA certificate shipped in this repo, resolving inside
  the deployment's Linux filesystem. Earlier the same code built its CA path
  from `%APPDATA%`, so it worked on the author's Windows machine and exited with
  a misleading error everywhere else — including on any judge's Mac. This field
  is the regression test for that.
- **`hlc`** is `cluster_logical_timestamp()`. It is the exact value
  `AS OF SYSTEM TIME` consumes, so its presence proves the replay mechanism is
  reachable from the deployed environment, not merely from a laptop.
- **`vector_index`** asserts `cases_embedding_idx` still exists. An index that
  quietly stopped existing does not raise anything — every query keeps
  succeeding, just via a full scan.
- **`retention_days` / `replay_ok`** assert the MVCC window is still ≥ 90 days.
  A short `gc.ttlseconds` also fails silently, and only surfaces once history is
  old enough to have been garbage collected — which on this project's calendar
  means during judging rather than during the build.

`cases: 0` is correct: the corpus is empty until seeding.

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

`EXPLAIN` for a nearest-neighbour query on the live `cases` table, **at 300
seeded rows** (the earlier capture was at 4 rows, where a full scan would have
been a legitimate plan and the evidence proved nothing durable):

```
EXPLAIN SELECT case_id FROM cases
ORDER BY embedding <-> $1::VECTOR(1024) LIMIT 5;

distribution: local

• top-k
│ estimated row count: 5
│ order: +column19
│ k: 5
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
              target count: 5
```

**Reading it bottom-up — why this is index access and not a scan:**

1. **`• vector search → table: cases@cases_embedding_idx`** is the leaf, and it
   is the whole proof. This node is C-SPANN index access: it descends the
   index and returns `target count: 5` approximate nearest candidates. A
   fallback plan would have no `vector search` node at all — it would show a
   table scan over `cases@cases_pkey` feeding a sort.
2. **`• lookup join` against `cases@cases_pkey`** exists *because* step 1 was an
   index read. The vector index stores the vector and the primary key, not the
   rest of the row, so the plan joins back to the PK to fetch the remaining
   columns. A full scan would already have every column and would need no such
   join — its presence is corroborating evidence of index access.
3. **`• top-k` at the apex** is a re-rank of the small candidate set from step 1
   by exact distance, not a sort of the table. Note `estimated row count: 5`:
   the planner expects a handful of rows arriving here, not the corpus. This
   node is what makes an approximate index result exact at the top of the list.

One thing worth stating because it affects how far this evidence goes: an
earlier version of the checking script reported this as passing by matching the
substring `"vector"` in the plan text — which also appears in the column type
and the `ORDER BY` expression. That was a false positive on a plan nobody had
read. The check now requires the *index name* to appear and `FULL SCAN` to be
absent. The plan above satisfies the stricter check.

**Filtered searches fall off the index, and are handled in Python.**

Adding any `WHERE` clause alongside the vector `ORDER BY` drops the plan onto a
full scan. Run `python explain_check.py` to reproduce:

```
  index   unfiltered
  SCAN    channel = 'whatsapp'
  SCAN    resolved only
  SCAN    channel + resolved
```

```
└── • filter
    │ filter: (channel = 'email') AND (resolution IS NOT NULL)
    │
    └── • scan
          table: cases@cases_pkey
          spans: FULL SCAN
```

This is a planner limitation, not stale statistics — the same result holds
after `ANALYZE cases` with row counts correctly reporting 300. CockroachDB even
recommends `CREATE INDEX ON cases (channel, resolution) STORING (embedding)`.

**That recommendation is deliberately not taken.** A prefixed vector index
scopes every search to one channel, which re-creates exactly the silos this
project exists to remove. Instead `recall.search()` over-fetches through the
unfiltered index and filters the candidates in Python. The trade-off, stated
rather than hidden: over-fetching is approximate, so a highly selective filter
could push a true nearest neighbour outside the candidate window. The
`overfetch` parameter exists for when that matters more than latency.

This is the kind of finding a naive check misses entirely — the index exists,
the unfiltered query uses it, and every filtered query silently scans the table.

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
a chat message, against the seeded corpus of 300 cases:

```
query: "you took the money off my card two times"

[1.0201] email     Double charge on my credit card
[1.0291] whatsapp  double charged
[1.0364] email     Double charge on my credit card
[1.0496] webchat   Double charge on my card
[1.0564] whatsapp  charged twice
```

Every result is a duplicate-charge case, and **all three channels appear in the
top five**. A chat-phrased query reaches formal email cases and terse WhatsApp
messages alike, which is the behaviour the whole design is for. The vector
index carries no `channel` prefix column precisely so this works.

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
