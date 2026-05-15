# Embedding-provider migration runbook

When you change `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, or `EMBEDDING_DIMENSIONS`
on a populated install (Weaviate has rows), the boot-time **dim guard** refuses
to start the backend. This runbook walks through the migration.

> **Why the guard exists.** Atlas's hybrid search compares query vectors
> against stored fact vectors via cosine similarity. Cosine similarity between
> vectors of *different dimensions* is undefined — Weaviate happily accepts
> a 3072d vector into a collection that previously held 2048d ones, and from
> that moment search returns garbage. There is no schema-level check; the
> dim guard is the only safety net.

## Operator playbook

### 1. Read the error

The container exits with a message like:

```
EmbeddingDimensionMismatch:
  Configured:  openai/text-embedding-3-large @ 3072
  Persisted:   jina_ai/jina-embeddings-v4 @ 2048
  Weaviate has 12,847 stored facts at the persisted dimension.

  Mixing dimensions will silently corrupt hybrid search.
  Either:
    1. Revert EMBEDDING_* to the persisted model, OR
    2. Run `make reembed-all` to rebuild the vector indexes.
```

### 2. Decide

You have three options:

#### A. Revert (no migration)

Set `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` back to
what `Persisted` shows. Restart. Done. No data is touched.

#### B. Re-embed all data (recommended)

```bash
# Confirm the cost preview first.
make reembed-dry-run
```

Output:

```
reembed: provider=openai model=text-embedding-3-large dim=3072
         facts=12,847 names=1,432 concurrency=4
reembed: --dry-run, exiting without changes
```

Then run for real:

```bash
make reembed-all
```

The script:

1. Walks every `AtomicFact` row in Weaviate, batches 100 at a time, computes
   embeddings via the new provider, replaces the vector in place (UUIDs
   unchanged so Neo4j's `EpisodicLink.weaviate_fact_id` foreign keys stay
   valid).
2. Walks every `Entity.name_vector` in Neo4j, same batching.
3. Atomically updates `embedding_meta` in MongoDB after both stores succeed.

Expected wall time at 100 facts/batch and `EMBEDDING_RPM=500`:

| Facts | Wall time (rough) |
|---:|---|
|   1,000 |   2 min |
|  10,000 |  20 min |
|  50,000 | 100 min |

Cost ballpark for the providers Atlas currently knows about (per 1M tokens):

| Provider/Model | Cost / 1M tokens | Free quota? |
|---|---:|---|
| `jina_ai/jina-embeddings-v4` | $0.18 | 1M free |
| `openai/text-embedding-3-large` | $0.13 | none |
| `openai/text-embedding-3-small` | $0.02 | none |
| `voyage/voyage-3-large` | $0.18 | 200M free |
| `cohere/embed-english-v3.0` | $0.10 | none |
| `gemini/gemini-embedding-001` | $0.025 | (rate-limit only) |
| `ollama/*` | $0.00 (local) | n/a |

#### C. Bypass the guard (NOT recommended)

```bash
EMBEDDING_DIM_GUARD=false ./atlas
```

The container starts, but **search returns garbage from the moment a
new-dim vector is written next to an old-dim one**. Only use this if you
have an external snapshot/restore plan.

### 3. Resume on crash

If `make reembed-all` is interrupted (Ctrl-C, OOM, network glitch), resume:

```bash
make reembed-resume
```

Checkpoints land in MongoDB collection `reembed_state` every 500 rows, so
the worst case is re-doing those 500. The script is idempotent: each
fact is updated in place by UUID, and Neo4j name vectors are upserted by
name.

### 4. Verify

```bash
docker compose up -d        # boot succeeds — dim guard now matches
curl http://localhost:8000/api/health   # ok
```

Then run a smoke search through the UI or:

```bash
curl -H "X-API-Key: $BEEVER_API_KEYS" \
  -d '{"query": "any test phrase you remember"}' \
  -H "Content-Type: application/json" \
  http://localhost:8000/api/search | jq .
```

Top results should look semantically reasonable. If they look random, the
re-embed didn't complete — check `reembed_state`:

```python
db.reembed_state.find_one()
# {stage: "weaviate_facts", processed: 6432, total: 12847}
```

## When the migration fails

* **Provider 401 / 403** mid-flight. Check that the new provider's API key
  is set (`EMBEDDING_API_KEY` env, the provider-default env var, or the
  encrypted DB-stored key from the Settings UI). Cancel with Ctrl-C, fix
  credentials, run `make reembed-resume`.
* **Provider rate-limited (429s).** Lower `EMBEDDING_RPM`. The shim
  retries 3× with backoff but a sustained 429 surface raises after
  budget exhaustion. The checkpoint will reflect what was completed.
* **Weaviate out of disk.** Free space, then resume.
* **MongoDB unavailable.** The shim writes vectors directly to Weaviate;
  the only loss is the checkpoint. Just rerun `make reembed-all` from
  scratch (it's idempotent).

## Rollback

If you decide post-migration that the new provider is wrong, the same
playbook applies in reverse: flip the env vars back, run
`make reembed-all` again.

The persisted vectors at any moment all share whatever model
`embedding_meta.dimensions` says — that invariant is preserved by the
atomic `embedding_meta` flip at the end of `reembed_facts.py`.
