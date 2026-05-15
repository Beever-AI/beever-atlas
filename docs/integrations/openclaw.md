# OpenClaw → Beever Atlas integration

OpenClaw is the external agent runtime that pushes incident-channel
events into Beever Atlas via the per-source HMAC ingest endpoint.
This guide is a thin wrapper over the vendor-neutral
[push-sources.md](./push-sources.md) cookbook with OpenClaw-specific
defaults documented inline. Read the vendor-neutral guide first; this
page assumes familiarity with the protocol.

## 1. Register the OpenClaw source

OpenClaw runs in a single environment per Atlas tenant. The convention
is one `source_id` per OpenClaw deployment:

| Env | source_id | allowed_channels_pattern |
|---|---|---|
| Staging | `openclaw-staging` | `thread-*` |
| Production | `openclaw-prod` | `thread-*` |

Register the source via the admin UI (`/admin/sources` route) or directly:

```bash
curl -X POST https://atlas.example/api/admin/sources \
  -H "X-Admin-Token: $BEEVER_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source_id": "openclaw-prod", "allowed_channels_pattern": "thread-*", "description": "OpenClaw agent runtime"}'
```

Copy the returned `secret` into OpenClaw's secret store
(`OPENCLAW_BEEVER_PUSH_SECRET`). The secret is shown ONCE and never
returned by `GET /api/admin/sources`. To rotate, use
`PATCH /api/admin/sources/openclaw-prod/rotate` and update the secret in
OpenClaw simultaneously — old signatures stop verifying immediately.

## 2. Lifecycle hook in OpenClaw

OpenClaw's `IncidentChannel.on_message` fires once per new chat message.
The recommended hook batches by 25 messages or 5 seconds, whichever
comes first, and uses a per-incident UUID as the idempotency key:

```python
from openclaw.atlas_push import PushClient

push = PushClient(
    source_id="openclaw-prod",
    secret=os.environ["OPENCLAW_BEEVER_PUSH_SECRET"],
    base_url="https://atlas.example",
)

@incident.on_message
def push_to_atlas(msg):
    push.append({
        "message_id": msg.id,
        "timestamp": msg.timestamp,
        "author": msg.author_id,
        "author_name": msg.author_name,
        "content": msg.text,
        "thread_id": msg.thread_id,
    }, channel_id=incident.thread_id, idempotency_key=incident.id)

@incident.on_close
def flush_remaining(_):
    push.flush()
```

The `PushClient.append` helper handles batching, signing, and the
`X-Beever-Idempotency-Key` header. See
[push-sources.md §7](./push-sources.md#7-lifecycle-hooks-push-when-ready-pattern)
for the implementation skeleton.

## 3. OpenClaw-specific defaults

- **Channel id** = OpenClaw's `thread_id` (the incident-channel id).
  This is the value Beever Atlas uses to scope the wiki page set, so
  one OpenClaw incident becomes one Atlas channel.
- **Author id** = OpenClaw's `participant_id`. Beever Atlas's entity
  registry resolves these against the Atlas user directory; if the
  author is an external escalation contact, set `is_bot=false` and
  use the human-readable name in `author_name`.
- **Bot messages** (`OpenClaw → /summary`, `OpenClaw → /alert`) MUST be
  flagged with `is_bot: true` so Atlas's coreference resolver doesn't
  treat them as utterances from a real participant.
- **Allowed-channels pattern** = `thread-*`. Atlas rejects events whose
  `channel_id` doesn't match the source's allowed pattern with HTTP 403,
  so OpenClaw's other channels (e.g. `metrics-*`) cannot accidentally
  spill into the incident wiki.

## 4. Verify end-to-end

After OpenClaw is wired:

1. Open a test incident in OpenClaw staging.
2. Post 5 messages from inside OpenClaw.
3. In Atlas: `GET /api/channels/<thread_id>/extraction-status`. Expect
   `counts.pending` to climb then drain to 0 within ~30 seconds (the
   ExtractionWorker tick interval).
4. Open the wiki for that channel in the Atlas dashboard. The Tensions
   row may be empty (no contradictions yet); `Maintain Wiki` should
   produce a non-stub overview page within seconds.

## 5. Troubleshooting

See [push-sources.md §6](./push-sources.md#6-troubleshooting-hmac-failures)
for the symptom → cause table. OpenClaw-specific gotchas:

- **OpenClaw runs the message hook in a forked subprocess.** If you
  see `reason=skew_exceeded`, NTP-sync the host (the subprocess
  inherits the parent's clock).
- **OpenClaw retries on 5xx but not on 429.** Honour `Retry-After`
  by capping the per-source flush rate at 50 req/min (under the
  60 req/min server limit, gives headroom for retries).
- **OpenClaw aggregates messages in `IncidentChannel.history`** — if
  the hook fires AFTER the in-process buffer was already POSTed, you
  may double-send the same `message_id`. The `(source_id, channel_id,
  message_id)` compound unique index makes this a no-op; the response
  reports them in `deduplicated`. Watch that counter — if it grows
  unboundedly, OpenClaw's idempotency-key generation is misconfigured.

## 7. Watch the wiki-drift dashboard during first soak

After OpenClaw starts pushing, open `/admin/wiki-drift` in the staging
dashboard. Once the integrated channel has accumulated ≥30 drift
reports (typically within the first day of active traffic with
`WIKI_DRIFT_AB=true`), confirm `pass_criterion_met=true`. A failing row
on a brand-new push integration usually points at one of:

- **Source-language mismatch** — OpenClaw is forwarding messages whose
  BCP-47 tag differs from the channel's `primary_language` setting.
- **Fact-shape skew** — OpenClaw's payload schema includes a non-
  standard field that's confusing the extractor's coreference pass.

See [`docs/runbooks/wiki-maintenance-soak.md`](../runbooks/wiki-maintenance-soak.md)
§22.4–§22.7 for the full drift-soak procedure.
