# Push-source ingest — vendor-neutral integration cookbook

This page describes the protocol used by `POST /api/sources/{source_id}/events`,
the Beever Atlas endpoint that lets external runtimes (OpenClaw, Hermes
Agent, custom bots) push messages into a channel without holding a user
auth token. Per-source HMAC signatures replace Bearer auth; idempotency
keys deduplicate retries; a 24h replay-cache + ±5 min skew window cover
the standard threat model.

For platform-specific examples, see:

- [docs/integrations/openclaw.md](./openclaw.md)
- [docs/integrations/hermes.md](./hermes.md)

## 1. Register a source via the admin UI

```bash
# Generate the secret server-side; the secret is returned ONCE.
curl -X POST https://atlas.example/api/admin/sources \
  -H "X-Admin-Token: $BEEVER_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source_id": "openclaw-prod", "allowed_channels_pattern": "thread-*"}'
```

The response contains `{source_id, secret, secret_fingerprint}`. **Copy
`secret` immediately — there is no GET that returns it again.** Subsequent
`GET /api/admin/sources` calls return only the fingerprint.

To rotate:

```bash
curl -X PATCH https://atlas.example/api/admin/sources/openclaw-prod/rotate \
  -H "X-Admin-Token: $BEEVER_ADMIN_TOKEN"
```

The PATCH returns a NEW plaintext secret and bumps `rotated_at`. Old
signatures stop verifying immediately on the next request.

## 2. Sign a request

The signature header is `X-Beever-Signature: t=<unix_ts>,v1=<hex>` where
`hex` is the lowercase HMAC-SHA256 over the byte string `"{ts}.{request_body}"`
using the per-source secret as the key. The body MUST be the same bytes
the server reads — sign the JSON-encoded body BEFORE any whitespace
normalization.

### Python

```python
import hashlib
import hmac
import json
import time
import httpx

SOURCE_ID = "openclaw-prod"
SECRET = "..."  # 32-byte URL-safe random from the registration response

def sign(secret: str, ts: int, body: bytes) -> str:
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"

body = json.dumps({
    "channel_id": "thread-42",
    "channel_name": "incident-2026-05-01",
    "events": [
        {
            "message_id": "evt-1",
            "timestamp": "2026-05-01T12:00:00Z",
            "author": "U1",
            "author_name": "Alice",
            "content": "Triage started",
        }
    ],
}).encode("utf-8")
ts = int(time.time())

resp = httpx.post(
    f"https://atlas.example/api/sources/{SOURCE_ID}/events",
    content=body,
    headers={
        "Content-Type": "application/json",
        "X-Beever-Signature": sign(SECRET, ts, body),
        # Optional but strongly encouraged for retries:
        "X-Beever-Idempotency-Key": "your-uuid-here",
    },
    timeout=30.0,
)
resp.raise_for_status()  # 202 on success
```

### Node

```ts
import crypto from "node:crypto";

const SOURCE_ID = "openclaw-prod";
const SECRET = process.env.BEEVER_PUSH_SECRET!;

function sign(secret: string, ts: number, body: Buffer): string {
  const sig = crypto
    .createHmac("sha256", secret)
    .update(`${ts}.`)
    .update(body)
    .digest("hex");
  return `t=${ts},v1=${sig}`;
}

const body = Buffer.from(JSON.stringify({
  channel_id: "thread-42",
  events: [{ message_id: "evt-1", timestamp: new Date().toISOString(),
             author: "U1", content: "hi" }],
}));
const ts = Math.floor(Date.now() / 1000);

await fetch(`https://atlas.example/api/sources/${SOURCE_ID}/events`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-Beever-Signature": sign(SECRET, ts, body),
    "X-Beever-Idempotency-Key": crypto.randomUUID(),
  },
  body,
});
```

### curl

```bash
TS=$(date +%s)
BODY='{"channel_id":"thread-42","events":[{"message_id":"evt-1","timestamp":"2026-05-01T12:00:00Z","author":"U1","content":"hi"}]}'
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$BEEVER_PUSH_SECRET" -hex | awk '{print $NF}')
curl -X POST "https://atlas.example/api/sources/openclaw-prod/events" \
  -H "Content-Type: application/json" \
  -H "X-Beever-Signature: t=$TS,v1=$SIG" \
  -H "X-Beever-Idempotency-Key: $(uuidgen)" \
  -d "$BODY"
```

## 3. Idempotency + replay handling

- `X-Beever-Idempotency-Key` is a client-generated UUID. The server
  caches the response for 24h via a Mongo TTL collection keyed by
  `(source_id, idempotency_key)`. A retry within 24h returns the
  CACHED 202 without re-upserting events.
- Same idempotency key reused across two different `source_id`s is
  treated as two independent requests (the cache key includes
  `source_id`).
- `t=<unix_ts>` MUST be within ±300 seconds of server time. Outside
  the window: 401 with `event=push_signature_rejected,reason=skew_exceeded`
  in the structured log.
- If you POST the same `message_id` twice with different idempotency
  keys, the second upsert is a no-op via the compound unique index on
  `(source_id, channel_id, message_id)` — but you'll have burned the
  rate limit budget on the duplicate, so prefer using the idempotency
  key for retries.

## 4. Rate limiting

`POST /api/sources/{source_id}/events` is rate-limited at **60 requests
per minute per source_id** (NOT per IP — clients may rotate IPs while
holding the same HMAC key, so the source is the natural unit of trust).
On breach the response is:

```json
HTTP/1.1 429 Too Many Requests
Retry-After: 60
Content-Type: application/json

{"error": "rate_limited", "source_id": "openclaw-prod", "retry_after_seconds": 60}
```

Well-behaved clients honour `Retry-After` and back off. The 429 short-
circuits before any state mutation: no events are written, no
idempotency-key reservation is recorded.

## 5. Response shape

```json
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "accepted": 50,
  "deduplicated": 0,
  "channel_id": "thread-42",
  "extraction": "queued"
}
```

`accepted` is the count of newly-inserted rows; `deduplicated` counts
rows the compound unique index treated as no-ops (same `(source_id,
channel_id, message_id)` already on file). `extraction: "queued"` means
the rows were marked `extraction_status="pending"` for the background
ExtractionWorker.

## 6. Troubleshooting HMAC failures

| Symptom | Likely cause | Fix |
|---|---|---|
| 401 with no log line | `X-Beever-Signature` header missing | Add the header on every request |
| 401, log: `reason=skew_exceeded` | Local clock drift > 5 min | NTP-sync the client; never use `time.time()` from inside a Docker container without `--privileged` |
| 401, log: `reason=signature_mismatch` | Body bytes signed differ from what the server reads | Sign the EXACT bytes you send. Don't re-encode JSON, don't pretty-print, don't add a trailing newline. |
| 401, log: `reason=secret_rotated` | Rotation happened during your in-flight request | Re-fetch the new secret and retry |
| 404 on the route | `source_id` was deleted | Re-register via the admin UI |
| 413 Payload too large | Body > 10 MB | Split the batch into smaller chunks |
| 429 with `Retry-After` | You exceeded 60 req/min for this source | Honour the header and back off |

## 7. Lifecycle hooks (push-when-ready pattern)

External runtimes typically push events at three lifecycle points:

1. **On message** — every new chat message immediately POSTed.
2. **On batch** — buffer N messages or M seconds and POST as a batch.
3. **On thread completion** — the entire conversation thread pushed at
   once when a triage / incident / decision flow concludes.

Recommended: combine #1 (real-time triage) with #2 (cost-optimised
backfill). Keep #3 for low-frequency / high-value sources where
context-completeness matters more than latency.

A skeletal lifecycle hook looks like:

```python
class PushClient:
    def __init__(self, source_id: str, secret: str, base_url: str):
        self.source_id, self.secret, self.base_url = source_id, secret, base_url
        self._buffer: list[dict] = []

    def append(self, event: dict) -> None:
        self._buffer.append(event)
        if len(self._buffer) >= 25:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        # ... sign + POST as in §2 ...
        self._buffer.clear()

    def close(self) -> None:
        self.flush()
```

Pair `flush()` with a 5-second timer for sources with bursty traffic so
no event waits more than 5 s for delivery even under low-volume periods.
