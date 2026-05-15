# Wiki Builder recompile-skip

The Builder caches per-page LLM output via `wiki_pages.kind_schema_hash`. When
the canonical structured input for a page hasn't changed since the last build,
the LLM call is skipped and the prior prose is reused. Each Builder run emits
a `wiki_build_cost_summary` event so operators can see the savings live in the
SyncMonitor's right pane.

## What gets hashed

`wiki_redesign_gap_fill / src/beever_atlas/wiki/hashing.py` canonicalises the
`kind_schema` payload before hashing:

* dict keys sorted recursively
* string values stripped of whitespace
* per-kind list fields normalised (e.g., `entity_tags` is order-irrelevant
  for a topic; `epochs` is order-significant for a timeline)
* derived fields excluded (`generated_at`, `updated_at`, `fact_count`,
  computed summaries)
* `prompt_version` mixed in so prompt edits invalidate the cache

The Maintainer persists the hash on every `apply_update` save.

## Cost summary event

Each Builder run emits one `wiki_build_cost_summary` event:

```json
{
  "event_type": "cost_summary",
  "stage": "wiki_build",
  "label": "Build complete: 8 pages (2 skipped, 12.3s)",
  "payload": {
    "calls_total": 8,
    "calls_skipped": 2,
    "duration_ms": 12345
  }
}
```

The SyncMonitor's Wiki Updates pane renders this as a footer row.

## Frozen-page skip

Pages with `curation_mode="frozen"` are filtered out of the Builder's compile
plan up front. Their existing prose is preserved byte-identical. Each frozen
skip emits a `wiki_update` event with `action="skipped_frozen"` so the right
pane shows the operator-visible cleanup.

## Force a recompile

If a legitimate change isn't picked up (canonicalisation bug, prompt edit not
reflected), the Regenerate endpoint accepts `force=true` to bypass the skip.
Operator UI exposes this via the Regenerate button's secondary "Force
recompile" action.

## Troubleshooting

* **All pages show as skipped** — check that the maintainer's apply_update is
  writing `kind_schema_hash`. If `kind_schema` is None on the page, the hash
  is empty and the Builder can't compare.
* **Prompt edit doesn't bust cache** — the `prompt_version` parameter to
  `compute_kind_schema_hash` is currently optional. Wire `compute_prompt_version(prompt_text)`
  through the Builder's per-kind dispatch to enforce this.
* **Skip too aggressive** — verify the per-kind list in
  `_UNORDERED_LIST_FIELDS` matches the page kind's prompt expectations.
