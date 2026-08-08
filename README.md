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

> Status: **three of four capabilities working**, each with reproducible
> evidence below. Semantic recall, the transactional race and point-in-time
> replay all run against the live cluster, and the replay UI is deployed.
> Survivability is not demonstrated yet. The [Evidence](#evidence) section
> records only what has actually been run — nothing is described as working on
> the strength of the code existing.

---

## What is at the live URL

[**recall-memory.vercel.app**](https://recall-memory.vercel.app)

- **The hero screen** — the email agent's approval beside the WhatsApp agent's
  decline. Same order, minutes apart, opposite decisions, both correct at their
  own timestamp. Under each, the cases it recalled *as they stood* — including
  the ones still open when it read them.
- **The counterfactual** — the same intent judged against today's memory, which
  flips to a decline.
- **A decision timeline** — click any row to reconstruct it.
- **"Run a new case"** — mints a throwaway order, runs an agent turn in a real
  serializable transaction, and the resulting decision is replayable seconds
  later. Not a recording.

Two properties worth stating, because they are structural rather than claimed:

**The page contains no diff logic.** It renders `replay.diff()` unchanged — the
same function `python replay.py diff` prints — so the terminal evidence below
and the deployed demo are provably the same code path.

**Replay makes no model call.** The query vector is stored on each decision, so
replay re-runs the exact historical query rather than re-embedding it. Verified
by running it with deliberately invalid AWS credentials: it completes normally,
while `recall.py search` fails with `UnrecognizedClientException` on the same
credentials. So the deployed page needs no AWS keys, costs nothing per view, and
is unaffected by a later model swap.

The live "run a new case" path is the same: it supplies a stored vector and a
fixed proposal, so the button makes no Bedrock call either. What it does *not*
fake is the part that matters — the transaction, the policy gate, the decision
row and the HLC anchor are all real, which is why the result genuinely replays.
Only the rationale text is pre-written, and the page says so.

---

## The four capabilities

| # | Capability | What it demonstrates | Status |
|---|---|---|---|
| 1 | **Semantic recall** | Past cases embedded into a `VECTOR(1024)` column with a distributed C-SPANN index. A new case retrieves the closest historical resolutions as context — across channels, so an email case can surface a WhatsApp resolution. | **Working** — 300-case corpus, index use verified at scale |
| 2 | **Transactional decisions** | The agent's decision and the action it authorises commit in a single serializable transaction. Two agents race the same refund; the second aborts, retries, sees the refund already happened, and does not double-pay. | **Working** — real `40001`, with a READ COMMITTED control showing the double-pay |
| 3 | **Point-in-time replay** | `AS OF SYSTEM TIME` reconstructs exactly what the agent knew at the moment of any past decision, plus a diff of what changed since. *"Why did the bot offer that discount?"* — **the headline feature.** | **Working** — CLI and live UI, with an exact counterfactual |
| 4 | **Survivability** | Kill a node mid-conversation; the agent keeps its memory and keeps going. | Not demonstrated yet — see [Evidence 5](#5-node-kill--survivability) |

Capabilities 2 and 3 are the ones that cannot be swapped onto another
database. They get protected ahead of everything else.

---

## Architecture

```
   email ─┐
whatsapp ─┼─→  agent  ───────────────→  Bedrock Nova Pro     (reasoning)
 webchat ─┘    (AWS Lambda)             Bedrock Titan v2     (embeddings, 1024d)
                     │                  S3                   (case artifacts)
                     ↓
              CockroachDB Cloud  ── cases + C-SPANN vector index
              (eu-west-2, v26.2.1)   customers, orders,
                                     decisions + actions
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
| Agent execution | **AWS Lambda** (Function URL) — and the same code from the CLI |
| Deployed demo | Vercel — static frontend and the read-only replay API |
| Analyst access | CockroachDB Cloud **managed MCP server**, read-only |

**All of it runs on AWS, in one region.** The CockroachDB Cloud cluster is
hosted on AWS in `eu-west-2`, across three availability zones — `SHOW REGIONS`
reports `aws-eu-west-2` with zones `aws-eu-west-2a/b/c`. Lambda, Bedrock and S3
are in the same region. Only the static page and the read-only replay API sit on
Vercel, and that is the one piece holding no credentials that can change
anything.

Co-locating them is not incidental: a full agent turn, including a serializable
transaction, returns in about a second because the function and the cluster are
in the same region.

**Where each part runs, and why.** The agent turn runs on **AWS Lambda** behind
a Function URL — that is the agentic work, and it is deployed on AWS. Vercel
serves the static page and the read-only replay API, because replay is pure SQL
against CockroachDB and needs no AWS credentials at all (see below), so putting
it on Lambda would buy nothing.

There is no Lambda-specific fork of the decision logic. `aws/handler.py` imports
`sandbox.py`, which imports `agent.py` — the same module `race.py` and the CLI
drive. What runs in production is what the evidence below was produced with.

The Converse API is used specifically so the reasoning model is swappable by
config rather than by rewrite. No model ID is hardcoded anywhere; both are read
from `.env`.

### Which CockroachDB and AWS tools are used, and how

**CockroachDB**

| Tool | How it is used |
|---|---|
| **Distributed vector indexing** | The corpus is a `VECTOR(1024)` column with a C-SPANN index. `explain_check.py` proves the planner *uses* it at real row counts — and that filtered variants fall off it, which is handled in Python rather than with a prefix column ([Evidence 1](#1-the-distributed-vector-index-accepts-vector1024--and-is-actually-used)) |
| **Cloud managed MCP server** | `mcp.json` connects any MCP client — Claude Code, Cursor, VS Code — to the cluster at `https://cockroachlabs.cloud/mcp`. An analyst asks questions of the corpus, the decisions and the actions in natural language, including historical ones, because `AS OF SYSTEM TIME` works through it. Read-only by default, audit-logged, authenticated by a service account whose only role is exactly that ([Evidence 4d](#4d-analyst-access-over-mcp-read-only)) |

Beyond the tool list, the entry leans on three CockroachDB capabilities that have
no equivalent in the Postgres + pgvector stack it would otherwise be:
`SERIALIZABLE` isolation, `AS OF SYSTEM TIME`, and `cluster_logical_timestamp()`
— which, as [Evidence 4b](#4b-the-control--the-same-code-one-isolation-level-down)
shows, is not even *supported* at a weaker isolation level.

**AWS**

| Service | How it is used |
|---|---|
| **Lambda** | Runs the agent turn behind a Function URL — recall, the policy gate, and the decision plus its action committed in one serializable transaction |
| **Bedrock — Titan Text Embeddings v2** | Embeds every case into the 1024-dim vector column, with a mandatory on-disk cache |
| **Bedrock — Nova Pro** | Proposes a resolution from the recalled cases, via the Converse API. It proposes and writes the rationale; it does not authorise — a pure policy gate does |
| **S3** | Stores case bodies. Vectors and pointers stay in CockroachDB; the artifacts do not |

### Repository layout

| File | |
|---|---|
| `schema.sql` | The `cases` corpus, the vector index, and MVCC retention |
| `db.py` | Connection helper — joins the cluster URL to the local CA cert |
| `embeddings.py` | Titan v2 embeddings with a mandatory on-disk cache |
| `recall.py` | The pipeline: `ingest_case` / `backfill` / `search` |
| `txn.py` | `run_serializable()`, the 40001 retry, and the `AS OF SYSTEM TIME` timestamp gate |
| `agent.py` | One agent turn: recall and propose outside the transaction, decide and write inside it |
| `race.py` | Two agents, one refund — and the isolation control |
| `replay.py` | Reconstruct what an agent knew, and diff it against now |
| `sandbox.py` | The live "run a new case" path, with no model call |
| `create_reader.py` | The read-only SQL user, and proof it cannot write |
| `aws/handler.py` | The agent turn as it runs on Lambda |
| `aws/deploy_lambda.py` | Builds a Linux-wheel deployment zip and ships it |
| `.mcp.json` | CockroachDB Cloud managed MCP server — analyst access, read-only |
| `apply_schema.py` | Idempotent schema applier; prints what landed |
| `retention.py` | Holds every replay-path table at the required MVCC window |
| `persona.py` | The fictional business the corpus describes — the human-editable part |
| `seed.py` | Builds the corpus: hand-written hero cases + Nova Pro batches, cached |
| `explain_check.py` | Whether the planner uses the vector index, filtered and unfiltered |
| `spike_replay.py` | The four schema-gating questions, answered against the cluster |
| `vector_index_check.py` | Probes whether the cluster indexes `VECTOR(1024)` |
| `verify_pipeline.py` | Self-cleaning end-to-end check of the whole path |
| `api/index.py` | The deployed API — one entrypoint, routes `/api/health`, `/api/replay`, `/api/run` |
| `public/index.html` | The replay UI |
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

Then the demos, in this order — it matters:

```bash
python create_reader.py --password '<pw>'   # BEFORE recording anything to replay
python create_reader.py --password '<pw>' --verify

python seed.py --drift --reset   # undo any previous drift
python race.py                   # two agents, one refund  -> Evidence 4
python race.py --control         # the same code at READ COMMITTED -> Evidence 4b
python seed.py --drift           # move the world on, AFTER the decisions
python replay.py list
python replay.py diff <id>       # -> Evidence 6

python sandbox.py                # one live agent turn, no model call
```

Two ordering constraints, both learned the hard way. The reader must exist
**before** the decisions you intend to replay, because CockroachDB resolves role
identity at the historical timestamp and a role cannot read a snapshot from
before it existed. And drift must run **after** the race, or there is nothing
changed to diff and the headline feature renders an empty panel.

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

**The commit timestamp is inclusive, which the replay code has to account
for.** `cluster_logical_timestamp()` returns the transaction's *commit*
timestamp, and `AS OF SYSTEM TIME` at that exact value already contains the
transaction's own writes. Measured on the live cluster against a real agent
decision:

```
AS OF decision_hlc      -> refunded_minor = 3498, decision row visible
AS OF decision_hlc - 1  -> refunded_minor = 0,    decision row absent
```

Reading at `decision_hlc` therefore reconstructs the world *after* the
decision, not the world it was made in — the opposite of what replay needs.
One logical tick earlier is the last instant before the transaction's effects
landed, which is exactly what the agent observed. `txn.read_snapshot()` does
that subtraction and every replay query goes through it.

Related: `AS OF SYSTEM TIME` rejects placeholders — *"only constant
expressions, with_min_timestamp, with_max_staleness, or
follower_read_timestamp are allowed"*. The timestamp must be interpolated as
text, so `txn.validate_hlc()` gates it with a strict pattern plus a `Decimal`
round-trip the value must survive unchanged. `now()`, `1e9`, `-1` and
`'...' OR '1'='1` are all rejected before any string reaches SQL.

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

Jess Ellis is charged £34.98 twice for `ORD-4502` and reports it on **email**
and on **WhatsApp**, minutes apart. Two agents pick it up. Both read the order,
both see no refund yet, both conclude "refund £34.98".

`python race.py`:

```
order      : ORD-4502 — Set of four stoneware mugs
amount     : £34.98   already refunded: £0.00
isolation  : serializable
----------------------------------------------------------------------
A  agent-email      attempts=1 committed=True isolation=serializable
   decision : refund_full  £34.98
B  agent-whatsapp   attempts=2 committed=True isolation=serializable sqlstates=['40001']
   decision : decline_already_refunded  £0.00
   aborted  : SQLSTATE 40001 on attempt 1, re-decided on attempt 2
   conflicts: 2652b314
   rationale: Order ORD-4502 is already fully refunded (£34.98). No further refund due.
----------------------------------------------------------------------
decisions written : 2
   agent-email      refund_full                £ 34.98  attempt=1
   agent-whatsapp   decline_already_refunded   £  0.00  attempt=2  aborted=40001
actions written   : 1
money actually out: £34.98
order says refunded: £34.98
----------------------------------------------------------------------
[ OK ]  exactly one refund action
[ OK ]  money out equals order value
[ OK ]  ledger agrees with money out
[ OK ]  both agents recorded a decision
[ OK ]  a serialization failure was observed
[ OK ]  the loser declined rather than refunded
```

**The retry produces a different decision, and that is the whole point.**
"Retried and succeeded" is a database feature. "Retried, re-read the world, and
correctly declined" is an agent one. The declined decision is still written, so
the audit trail records that a second agent considered this refund and refused
it — `attempt=2`, `abort_sqlstate=40001`, `conflicts_with` naming the winner.

**Why the assertions are not about the final amount.** The order update is an
absolute assignment computed in Python, so a lost update and a correct abort
leave the *same* `refunded_minor`. The number cannot distinguish them. Only the
observed `40001`, the row count in `actions`, and whether the loser declined
can — so those are what the harness asserts, and the amount is merely reported.

### 4b. The control — the same code, one isolation level down

`python race.py --control` runs the bare read-modify-write on the order,
stripped of everything else, at each isolation level:

```
isolation          : read committed
refunds authorised : 2  (committed: ['B', 'A'])
aborted            : 0  []
order says refunded: £34.98

LOST UPDATE. Two agents each authorised a £34.98 refund and both committed.
The customer is owed £34.98 and has been refunded £69.96, while the order row
still claims £34.98. The money and the ledger disagree, and nothing errored.
```

```
isolation          : serializable
refunds authorised : 1  (committed: ['A'])
aborted            : 1  ['B']

One refund authorised, one transaction aborted. The database refused the
second write.
```

That is the cost of the weaker isolation level, measured rather than asserted:
**£69.96 out against a £34.98 order, with a ledger that reads £34.98 and not a
single error raised.**

**And a stronger finding underneath it.** The control cannot write decision
rows at all, because `cluster_logical_timestamp()` is *unsupported* under READ
COMMITTED:

```
[ OK ]   cluster_logical_timestamp() under serializable: 1785759733163042399
[FAIL]   cluster_logical_timestamp() under read committed
         -> unsupported in READ COMMITTED isolation
```

Under READ COMMITTED every statement gets its own snapshot, so there is no
single transaction timestamp to record — and therefore no replay anchor. The
headline feature does not merely work *better* at SERIALIZABLE; it cannot be
recorded without it. Capabilities 2 and 3 turn out to be the same mechanism
viewed from two angles.

### 4c. The public page cannot write

The deployed UI reads through `recall_reader`, not the admin account. That is
checkable from outside — [`/api/health`](https://recall-memory.vercel.app/api/health)
reports which credential is live:

```json
{ "sql_user": "recall_reader", "read_only": true }
```

`python create_reader.py --password '<pw>' --verify` connects *as* that user and
proves the boundary rather than asserting it:

```
connected as: recall_reader
--------------------------------------------------------------
[ OK ]   SELECT cases                       303 rows
[ OK ]   AS OF SYSTEM TIME read             303 rows
--------------------------------------------------------------
[ OK ]   UPDATE orders                      refused (42501)
[ OK ]   DELETE decisions                   refused (42501)
[ OK ]   INSERT cases                       refused (42501)
[ OK ]   DROP TABLE cases                   refused (42501)
[ OK ]   CREATE TABLE                       refused (42501)
--------------------------------------------------------------
Reader can read history and cannot change it.
```

It reads history, including `AS OF SYSTEM TIME`, and every write is refused with
**42501 — insufficient privilege**. That distinction matters more than it looks:
a reader that could write could alter the very history it exists to report on,
so the audit trail and the thing auditing it would share a credential.

Three things this check caught that `GRANT` alone did not fix:

- **`CREATE TABLE` succeeded.** The privilege is not held by the user — the
  `public` pseudo-role holds `CREATE` on schema `public` and every account
  inherits it. Revoking from `recall_reader` was a no-op; it had to be revoked
  from `public` itself.
- **The `CREATE` probe then passed for the wrong reason.** It used a fixed table
  name, so once the leak had actually created it the refusal became `42P07`
  (*duplicate table*) rather than `42501` — the check silently stopped testing
  permissions. It now uses a random name per run and asserts the SQLSTATE, not
  merely that something threw.
- **`AS OF SYSTEM TIME` failed** for the reader with *"role was concurrently
  dropped"*. Not permissions: CockroachDB resolves role identity at the
  historical timestamp, and the role did not exist in that past. **A read-only
  user cannot replay history from before it was created** — so create the reader
  before recording anything you intend to replay.

### 4d. Analyst access over the Cloud managed MCP server

`.mcp.json` is the configuration the CockroachDB Cloud Console generates. Clone
the repo, authenticate once, and any MCP client — Claude Code, Cursor, VS Code —
can interrogate the memory layer in natural language.

Asked *"show me every decision on ORD-4502 and what each concluded"*, the server
returned:

```json
[
  {"agent_id": "agent-email",    "decision_kind": "refund_full",
   "amount_minor": 3498, "attempt": 1, "abort_sqlstate": null,
   "display_name": "Jess Ellis", "item": "Set of four stoneware mugs"},
  {"agent_id": "agent-whatsapp", "decision_kind": "decline_already_refunded",
   "amount_minor": 0,    "attempt": 2, "abort_sqlstate": "40001",
   "display_name": "Jess Ellis", "item": "Set of four stoneware mugs"}
]
```

and *"how many decisions were forced to retry by a serialization conflict?"*:

```json
[{"forced_to_retry": 1}]
```

That is the race, read back through CockroachDB's own tooling by someone who
wrote no SQL — the retry, the SQLSTATE, and the fact that the second agent
declined rather than paid again.

**No secret in the config.** `mcp-cluster-id` is the cluster's UUID, not a
credential; authentication is separate — OAuth interactively, or the
`recall-mcp-reader` service account for unattended use, whose Console
description is *"Read-only MCP analyst access to Recall agent memory. No write
privileges."*

**Two independent layers, neither relying on the other.** The service account
bounds what the MCP server can reach at the CockroachDB Cloud level; the
`recall_reader` SQL user bounds what any connection can do inside the database,
where every write is refused with `42501`. An analyst tool that could write
might alter the history it exists to report on — and pointing an LLM at that
tool sharpens the problem rather than softening it.

#### Feedback on the tool: MCP cannot do point-in-time reads

Worth reporting, since the managed server is one of the tools this entry is
built on. It pins its own read timestamp, so a user-supplied `AS OF SYSTEM TIME`
is rejected:

```
inconsistent AS OF SYSTEM TIME timestamp;
expected: 1786186664.072974567,0, got: 1786094389.108813397,0
```

Sensible as a default — it keeps analyst queries consistent and cheap. But it
means the headline capability of this project, reconstructing what an agent knew
at a past instant, is **not** reachable through MCP; `replay.py` uses a direct
SQL connection for that. An opt-in that let a caller supply its own timestamp
would make the managed server a complete analyst surface for exactly the
auditing use case CockroachDB's time-travel reads are best at.

### 5. Node kill — survivability

*Not yet run. Output goes here when it is.*

### 6. Replay diff — what the agent knew, and what changed since

The headline. `python replay.py diff fc4fbba6` against the winning decision
from the race above:

```
decision   : fc4fbba6-609c-4519-b1bb-5bfbc063bfb4
agent      : agent-email   2026-08-03 12:47:01+00:00
decided    : refund_full  £34.98
snapshot   : 1785761221071400222.000000000
==========================================================================
WHAT IT SAW
  order ORD-4502: Set of four stoneware mugs, £34.98, already refunded £0.00
  #1 [email   ] Charged twice for order 4502
      -> (still open)
  #2 [whatsapp] double charge
      -> the duplicate charge was refunded.
  #3 [webchat ] Duplicate Charge
      -> Duplicate charge was refunded.
  #4 [whatsapp] double charged
      -> (still open)
  #5 [email   ] Double Charge on Order ORD-1067
      -> The duplicate charge was refunded to the customer's account.
--------------------------------------------------------------------------
WHAT CHANGED SINCE
  order.refunded_minor: £0.00 -> £34.98
  case "Charged twice for order 4502" resolution:
      then: None
      now : Duplicate charge confirmed and refunded in full.
  case "double charged" resolution:
      then: None
      now : Duplicate charge confirmed and refunded in full.
  entered top-5 at #2: paid twice by mistake
  left top-5 (was #5): Double Charge on Order ORD-1067
  moved #3 -> #4: Duplicate Charge
  later: agent-whatsapp decline_already_refunded £0.00 (after 40001)
--------------------------------------------------------------------------
COUNTERFACTUAL — same intent (refund_full), judged again
  against the world it saw : refund_full
  against the world today  : decline_already_refunded
  THE DECISION WOULD FLIP. Same agent, same intent, different memory.
```

**Why this is not a text column.** Every database can store a rationale
string. What is reconstructed above is the *evidence*: the order as it stood,
the five cases the agent recalled with their resolutions at that instant, and
the same query vector re-run against the corpus as it was. Two of those cases
were **still open** when the agent read them and have since been resolved — so
the record shows the agent acting on genuinely incomplete information, which no
amount of after-the-fact logging would reveal.

**The counterfactual is exact, not estimated.** Authorisation lives in a pure
function, so replay re-runs the original *intent* against both worlds for the
cost of a function call. The same agent, given the same instruction and today's
memory, would decline.

Three properties make this hold up:

- **One snapshot for the whole diff.** The reconstruction touches four tables,
  so it runs inside `BEGIN AS OF SYSTEM TIME` rather than four independently
  timestamped statements that could disagree.
- **No model call.** The query vector is stored on the decision, so replay
  re-runs the exact historical query. Deterministic, free, unaffected by a
  later `BEDROCK_EMBED_MODEL` swap — and runnable from a deployed page with no
  AWS credentials at all.
- **No diff logic in the UI.** The web page renders exactly what `diff()`
  returns, so the terminal output above and the deployed demo are provably the
  same code path.

**A bug worth recording, because it failed convincingly.** The first working
version read the query vector inside the historical snapshot — but at that
timestamp the decision row does not exist yet, it committed one tick later. The
subquery returned NULL, every distance became NULL, and the "nearest cases as
they were" came back in arbitrary order. The output looked entirely plausible:
five real cases, five real subjects, nothing obviously broken. It was only
visible because the diff claimed all five results had *entered* the top five
and five unrelated cases had *left* it. The vector is now read at present time
and carried into the past.

---

## Licence

MIT — see [LICENSE](LICENSE).

Built by Flux AI Consulting Ltd for the CockroachDB × AWS "Build with Agentic
Memory" hackathon, 2026.
