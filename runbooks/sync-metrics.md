# Sync Metrics Runbook

Four structured log lines are emitted at the end of every sync completion event.
Each line begins with the stable prefix `sync_summary:` and uses `key=value` pairs
so they are parseable by `grep -oP` one-liners without a log aggregator.

## Metrics

### 1. `relationships_dropped_total`

Count of graph relationships that were silently skipped because one or both endpoint
entities did not exist in Neo4j at write time. This happens under concurrent batch
execution when batch N+1 references an entity still being written by batch N.

**Threshold**: average ≥5 dropped/sync over ≥3 consecutive baseline syncs triggers
PR-2 (MATCH→MERGE stub fix). Average <5 means the data-gated fix is deferred.

**Follow-up tasks**: PR-2 (Tasks 1+2) — MERGE-stub endpoint creation.

### 2. `cluster_size_histogram`

Distribution of topic-cluster `member_count` values at the end of the sync, bucketed
into `[1, 2, 3, 5, 10, 11+]`. Each pair `[bucket_upper, count]` counts how many
clusters have `member_count` in that bucket.

**Threshold**: if the `11+` bucket grows consistently, `cluster_similarity_threshold`
(OS-1) may need lowering. If the `1` bucket dominates, clusters are too fragmented.

**Follow-up tasks**: OS-1 — cluster threshold tuning (gated on this metric).

### 3. `entity_truncation_recoveries`

How many batches triggered the JSON-recovery path for entity extraction output.
`lost_estimate_sum` is the cumulative byte count of unrecoverable content across
those batches.

**Threshold**: consistent `lost_estimate_sum > 0` suggests the entity extractor
output is regularly exceeding the token budget. Consider reducing `max_facts_per_message`
or increasing output token budget.

**Follow-up tasks**: Entity extractor prompt/budget tuning.

### 4. `cross_batch_validator_llm_fallback_total`

Total LLM fallback calls made by the deterministic cross-batch validator across all
batches in the sync. The validator's fast path is embedding-based; the LLM path is
only hit when embedding similarity is ambiguous.

**Threshold**: if average >1% of entities processed, the LLM fallback is overactive.
Consider tightening the embedding similarity threshold or dropping the LLM fallback
tier entirely (gated on rate <1%).

**Follow-up tasks**: Drop the LLM fallback in cross_batch_validator (gated on <1%).

---

## Extraction One-Liners

Replace the `journalctl` command with your log source if running outside systemd
(e.g. `docker logs beever-atlas`, `cat /var/log/beever-atlas.log`).

```bash
# (1) relationships_dropped_total — avg across window
journalctl -u beever-atlas --since "3 days ago" \
  | grep -oP 'sync_summary: metric=relationships_dropped_total value=\K\d+' \
  | awk '{s+=$1; n++} END { if (n) printf "avg=%.2f n=%d\n", s/n, n }'

# (2) cluster_size_histogram — last sync
journalctl -u beever-atlas --since "1 hour ago" \
  | grep -oP 'sync_summary: metric=cluster_size_histogram value=\K\[[^ ]+\]' \
  | tail -1

# (3) entity_truncation_recoveries — sum over window
journalctl -u beever-atlas --since "3 days ago" \
  | grep -oP 'sync_summary: metric=entity_truncation_recoveries value=\K\d+' \
  | awk '{s+=$1} END {print s}'

# (4) cross_batch_validator_llm_fallback_total — avg
journalctl -u beever-atlas --since "3 days ago" \
  | grep -oP 'sync_summary: metric=cross_batch_validator_llm_fallback_total value=\K\d+' \
  | awk '{s+=$1; n++} END { if (n) printf "avg=%.2f n=%d\n", s/n, n }'
```

---

## PR-2 Gate Check

Run the following after ≥3 consecutive baseline syncs to decide whether PR-2 should
merge:

```bash
journalctl -u beever-atlas --since "3 days ago" \
  | grep -oP 'sync_summary: metric=relationships_dropped_total value=\K\d+' \
  | awk '{s+=$1; n++} END { if (n>=3 && s/n>=5) print "GATE OPEN avg="s/n; else print "GATE CLOSED avg="s/n" n="n }'
```

PR-2 merges only when output is `GATE OPEN`.
