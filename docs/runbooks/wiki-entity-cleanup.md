# Wiki entity-page cleanup

Two-phase cleanup of legacy `wiki_pages` rows where `kind="entity"`. Post
`wiki-redesign-gap-fill` the maintainer no longer writes those rows — entity
intent is absorbed into the canonical `people` and `glossary` pages — but
existing rows linger from prior syncs.

## Phase 1 — Archive (reversible)

Flips `archived=true` on every legacy `kind="entity"` row. Idempotent + reversible.

```bash
# Inspect first.
python -m beever_atlas.scripts.archive_kind_entity_pages --dry-run

# Pilot one channel.
python -m beever_atlas.scripts.archive_kind_entity_pages --channel-id <id> --dry-run
python -m beever_atlas.scripts.archive_kind_entity_pages --channel-id <id>

# Whole instance.
python -m beever_atlas.scripts.archive_kind_entity_pages
```

After archiving, the existing `/admin/entity-pages/<channelId>` debug endpoint
hides archived rows. Add `?include_archived=true` to inspect them.

To reverse:

```bash
python -m beever_atlas.scripts.archive_kind_entity_pages --unarchive
```

## Phase 2 — Drop (irreversible)

Run **only after** the retention window has elapsed (default 30 days). The
script refuses to delete without `--confirm`.

```bash
python -m beever_atlas.scripts.drop_archived_kind_entity_pages --dry-run
python -m beever_atlas.scripts.drop_archived_kind_entity_pages --confirm
```

Optional flags: `--channel-id <id>`, `--min-archived-age-days <N>` (default 30),
`--batch-size <N>` (default 500).

## Resume after interrupt

The archive script writes a checkpoint to the `migration_state` collection
under key `archive_kind_entity_pages` after every batch. A re-run with the
same scope (channel-id + target-lang + unarchive flag) sees the prior
`completed=true` and skips. Pass `--no-resume` to force a re-scan.

## Verification

After Phase 1 the debug endpoint should show:

```json
{
  "channel_id": "<id>",
  "count": 0,
  "archived_count": <n>,
  "include_archived": false,
  "pages": []
}
```

`archived_count > 0` with `count == 0` confirms the archive ran. After Phase 2,
both numbers go to zero.
