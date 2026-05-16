# Channel Reset — drop and re-sync to refresh the graph

Use this when a channel's graph memory shipped under the pre-fix code path
(Topic-stub monoculture, missing types) and you want a clean baseline after
the Unresolved heal-path + 4 new entity types went in.

Existing entities/relationships in Neo4j are *not* auto-migrated. The
heal-path only kicks in for *new* writes — re-sync triggers fresh
extraction and the new pipeline produces a properly typed graph.

## Prerequisites

- API running locally on `http://localhost:8000`
- `BEEVER_ADMIN_TOKEN` or `BEEVER_API_KEYS` exported as `$T`
- `cypher-shell` available with credentials to your Neo4j instance
- Replace `$CH` and `$CH_NAME` below with the target channel id / display
  name.

## Steps

```bash
# 0. Identify the channel.
export T=0e59c822ee9756a4d7a9adeba51d7001d2447bb7151ffa1c
export CH=agypsmc1qfynzexdc4ec8wuske          # biz-consumer-council

# 1. Drop channel-scoped entities + relationships in Neo4j.
#    Global Persons / Technologies remain — they are workspace-wide and
#    will be re-bound to this channel on the next sync.
cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "
  MATCH (e:Entity)-[:MENTIONED_IN]->(:Event {channel_id:'$CH'})
    DETACH DELETE e;
  MATCH (ev:Event {channel_id:'$CH'}) DETACH DELETE ev;
"

# 2. Mark the channel's facts for re-embed (or wipe them via the admin
#    endpoint). The admin route resets stored facts so the next sync
#    re-extracts them from the source messages.
curl -X POST -H "Authorization: Bearer $T" \
  "http://localhost:8000/api/admin/sources/$CH/reset"

# 3. Force a fresh sync.
curl -X POST -H "Authorization: Bearer $T" \
  "http://localhost:8000/api/channels/$CH/sync?force=true"

# 4. Wait 5-8 min for a 417-message channel, then verify.
curl -s -H "Authorization: Bearer $T" \
  "http://localhost:8000/api/graph/entities?channel_id=$CH&limit=500" \
  | jq '[.[] | .type] | group_by(.) | map({t: .[0], n: length})'
```

## Acceptance targets (post-resync)

| Metric | Pre-fix | Post-resync target |
|---|---|---|
| Visible nodes in Memory Graph | ~40 (capped) | ≥ 100 |
| `Topic` entity share | ≈ 99% | ≤ 5% (only LLM-emitted Topic, no stubs) |
| `Unresolved` entities | 0 | ≤ 10 (transient — heal as more writes land) |
| Sum of typed entities (Person + Technology + Project + Team + Decision + Meeting + Artifact + Organization + Concept + Location + Event) | ~0 | ≥ 100 |
| `wiki/graph` `references_entity` edges | 0 | ≥ 50 |
| "Unconnected" pill in UI | 36 | ≤ 5 |

## Rollback

If something looks wrong, the heal path is fully forward-compatible —
old `Topic` stubs are not deleted by the new code, only avoided.
Reverting the application code restores the old behavior without any
schema-level rollback.

## Notes

- The 4 new entity types (`Organization`, `Concept`, `Location`, `Event`)
  are additive — existing `Person` / `Technology` / `Project` / `Team` /
  `Decision` / `Meeting` / `Artifact` remain unchanged.
- `Unresolved`-typed entities are *backend-only* — the LLM is never
  instructed to emit `Unresolved`; the persister/store uses it as a
  transitional state until a typed write heals it in place.
- Stub orphans (`Unresolved` with no edges, older than 24h) are pruned
  on the existing `orphan_reconciler` schedule.
