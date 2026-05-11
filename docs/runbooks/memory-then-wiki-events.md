# Memory → Wiki event contract

The Beever Atlas pipeline runs as **Source → Memory → Wiki**. The
wiki is a *projection* of memory and only refreshes when memory has
stopped changing for a channel. This runbook covers the two events
that drive that contract and how operators debug them.

## The two events

| Event | When it fires | Subscribers act? | Payload |
|---|---|---|---|
| `memory_changed` | After every per-channel extraction batch in every worker tick | NO — accumulator only | `(channel_id, fact_ids)` |
| `memory_settled` | When a channel's queue transitions to empty (`pending+extracting=0`) | YES — terminal trigger | `(channel_id,)` |

Both events are emitted by `ExtractionWorker` (see
`src/beever_atlas/services/extraction_worker.py`). The settlement check
runs at the end of every `tick()` cycle for each channel touched in
that tick.

## Subscribers

- **`WikiMaintainer.on_memory_changed`** — routes `fact_ids` to affected
  pages via `plan_updates`, then writes each `(channel_id, page_id,
  fact_ids)` tuple into the `wiki_dirty_queue` MongoDB collection. No
  LLM call, no debounce.
- **`WikiMaintainer.on_memory_settled`** — schedules one debounced
  flush (60s window by default). Multiple `memory_settled` events for
  the same channel within the window collapse to a single flush.
- **`AutoOverviewSubscriber.on_extraction_done` (called via
  `memory_settled`)** — runs its 4-gate check (feature flag, in-flight,
  min facts, no existing overview) and fires the Builder if all pass.

## The `wiki_dirty_queue` collection

Durable replacement for the in-memory `_dirty` dict. Each row is
`{channel_id, page_id, fact_ids[], status, created_at, updated_at}`.
Status transitions: `pending → flushing → done`. The maintainer's
`recover_stale_flushing` flips `flushing` rows older than 10 min back
to `pending` so crashed flushes recover automatically.

### Operator queries

```javascript
// Pending pages for a channel
db.wiki_dirty_queue.find({channel_id: "C1", status: "pending"})

// What's currently flushing (should drain within a few seconds)
db.wiki_dirty_queue.find({status: "flushing"})

// Recently flushed (audit trail)
db.wiki_dirty_queue.find(
  {status: "done"},
  {channel_id: 1, page_id: 1, updated_at: 1}
).sort({updated_at: -1}).limit(20)
```

## The kick channel

`SyncRunner` calls `extraction_worker.kick()` after every successful
`channel_messages` upsert with new inserted rows. This wakes the
worker's run loop immediately instead of waiting up to 10s for the
next tick boundary. Kicks coalesce — multiple kicks within a tick
trigger one wakeup.

`metrics_snapshot()['kick_received_count']` exposes the total kicks
received since process start. A value that stays at 0 during active
sync indicates SyncRunner→Worker wiring is broken.

## Debugging "the wiki didn't update after my sync"

1. **Check `wiki_dirty_queue`** for pending rows on the channel. If
   present, the maintainer's flush hasn't fired yet (debounce window
   or `WIKI_MAINTENANCE_MODE=manual`).
2. **Check the maintainer's logs** for
   `wiki_maintainer.on_memory_settled ... (flush scheduled)`. Absence
   means `memory_settled` didn't fire — usually a worker tick issue.
3. **Check the worker's `metrics_snapshot`** — if `kick_received_count`
   stayed at 0, SyncRunner isn't kicking after upsert.
4. **Check for `flushing` rows older than 10 min** — those are
   crashed flushes. The next maintainer flush cycle recovers them
   automatically.

## Manual mode operator workflow

When `WIKI_MAINTENANCE_MODE=manual`:

- `on_memory_changed` still enqueues to `wiki_dirty_queue`.
- `on_memory_settled` is a no-op (operator-driven trigger).
- The operator clicks "Maintain Wiki" → `WikiMaintainer.maintain_now`
  reads the queue and flushes pending rows.

The dirty queue acts as the durable backlog the operator can inspect
before clicking the button.

## Backwards compatibility

The legacy `subscribe_extraction_done` API is retained as a
transitional alias during the deprecation window. It fires the
legacy callbacks AND the `memory_changed` subscribers on every batch,
so out-of-tree callers continue working. The alias is removed in a
follow-up cleanup commit once all known subscribers have migrated.

Subscribers SHOULD migrate by calling:

```python
worker.subscribe_memory_changed(my_accumulator_handler)
worker.subscribe_memory_settled(my_terminal_handler)
```

…and stop calling `worker.subscribe_extraction_done`.
