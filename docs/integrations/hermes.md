# Hermes Agent → Beever Atlas integration

Hermes Agent is a long-running conversation runtime that holds
multi-week chat threads with a single user (think: project assistant,
lifecycle coach). Unlike OpenClaw (incident bursts), Hermes pushes
slow, sustained traffic. This guide is a thin wrapper over the vendor-
neutral [push-sources.md](./push-sources.md) cookbook with Hermes-
specific defaults inline.

## 1. Register the Hermes source

Hermes typically runs one source per workspace:

| Env | source_id | allowed_channels_pattern |
|---|---|---|
| Staging | `hermes-staging` | `hermes-*` |
| Production | `hermes-prod` | `hermes-*` |

```bash
curl -X POST https://atlas.example/api/admin/sources \
  -H "X-Admin-Token: $BEEVER_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source_id": "hermes-prod", "allowed_channels_pattern": "hermes-*", "description": "Hermes Agent runtime"}'
```

Copy the returned `secret` into Hermes' secret store
(`HERMES_BEEVER_PUSH_SECRET`). Rotate via `PATCH /rotate` per the
vendor-neutral guide.

## 2. Lifecycle hook in Hermes

Hermes' conversation engine emits `Conversation.on_turn` for each
user/agent exchange. Because the cadence is slow (days between turns),
do NOT batch — push immediately so the wiki reflects state in near-real-
time:

```python
from hermes.adapters.beever_atlas import PushClient

push = PushClient(
    source_id="hermes-prod",
    secret=os.environ["HERMES_BEEVER_PUSH_SECRET"],
    base_url="https://atlas.example",
)

@conversation.on_turn
async def push_turn_to_atlas(turn):
    await push.send_one(
        channel_id=f"hermes-{conversation.id}",
        event={
            "message_id": turn.id,
            "timestamp": turn.timestamp,
            "author": turn.user_id if turn.role == "user" else "hermes",
            "author_name": turn.user_name if turn.role == "user" else "Hermes",
            "content": turn.text,
            "is_bot": turn.role != "user",
        },
        idempotency_key=turn.id,  # turn id is already a UUID
    )
```

The `idempotency_key=turn.id` pattern is convenient — Hermes turn ids
are already UUIDs, so the cache lookup deduplicates retries naturally
without an extra UUID gen step.

## 3. Hermes-specific defaults

- **Channel id** = `hermes-<conversation_id>`. One Hermes conversation
  is one Atlas channel; this gives the user one wiki per long-running
  conversation thread.
- **Author id**: real users use their Atlas user id; the Hermes agent
  itself uses `"hermes"` as a stable bot id. Atlas's coreference
  resolver treats `"hermes"` as a single participant across all
  conversations.
- **`is_bot=true`** on every Hermes turn. The wiki maintainer's
  fact-routing skips bot messages from the "decisions" page (bots can
  surface decisions but rarely make them); set this flag correctly so
  the routing isn't polluted.
- **Allowed-channels pattern** = `hermes-*`. Other Hermes runtimes (e.g.
  Hermes Tools, Hermes Notifications) MUST register their own
  `source_id` with their own pattern.

## 4. Maintenance mode recommendation

Hermes conversations evolve incrementally — a daily turn that adds one
fact, one decision. The redesign's `WIKI_MAINTENANCE_MODE=auto` is
designed for exactly this cadence: every turn fires the maintainer,
which routes the new fact to ~1-3 affected pages and rewrites only
those sections.

Recommendation: set `wiki.maintenance_mode = "auto"` on the channel
policy for Hermes channels (via the `ChannelSettingsTab` UI or
`PATCH /api/channels/{id}/policy`). The global default may stay
`"manual"` for safety; the per-channel override flips Hermes to auto
without affecting other channels.

## 5. Verify end-to-end

1. Start a new Hermes conversation in staging.
2. Post 3 user/agent turns.
3. In Atlas: `GET /api/channels/hermes-<conv>/extraction-status` — expect
   `counts.pending` to climb then drain.
4. Open the wiki for that channel; with `wiki.maintenance_mode="auto"`,
   pages should refresh within seconds of each turn.
5. Run `lint_wiki(channel_id="hermes-<conv>")` from any MCP client and
   confirm zero findings on a healthy short conversation.

## 6. Troubleshooting

See [push-sources.md §6](./push-sources.md#6-troubleshooting-hmac-failures)
for the symptom → cause table. Hermes-specific gotchas:

- **Hermes' message hook is async.** If the HTTP POST fails, the hook
  MUST log + retry on the next turn (not crash the conversation). The
  `(source_id, channel_id, message_id)` compound unique index makes
  retried turns idempotent.
- **Long pauses between turns.** Atlas does not require a heartbeat;
  the channel can sit idle for weeks and the wiki stays stable. The
  ExtractionWorker only does work when there are pending rows.
- **Long idempotency-key TTL.** The 24h cache is plenty for a Hermes
  conversation that posts one turn per day; you don't need a longer
  TTL because the compound unique index handles duplicate `message_id`
  retries beyond the cache window.

## 6. Watch the wiki-drift dashboard during first soak

Hermes' slow, sustained traffic makes drift easy to read on the
`/admin/wiki-drift` admin dashboard. Because Hermes posts one turn at
a time, the maintainer's per-page comparator runs naturally inside the
60s rate-limit window — every turn that touches a page in `auto` mode
yields one drift report. Within ≈30 turns you'll have enough samples
to confirm `pass_criterion_met=true`.

If the row trends toward `pass_criterion_met=false`, suspect one of:

- **Voice drift** — Hermes' user-facing turns mix model + assistant
  voice; the maintainer's `_render_apply_update_prompt` may be picking
  up the assistant tone where it should be preserving the user voice.
- **Compounding-fact errors** — slow-traffic channels are where
  incremental updates compound the longest; a small per-turn drift
  will surface as elevated p95 medians over the 14-day window.

See [`docs/runbooks/wiki-maintenance-soak.md`](../runbooks/wiki-maintenance-soak.md)
§22.4–§22.7 for the full drift-soak procedure.
