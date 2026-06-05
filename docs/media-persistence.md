# Durable Channel-Media Persistence

Channel media (images, PDFs, video, audio shared in Slack / Discord / Microsoft
Teams / Mattermost) used to be **link-only**: ingestion downloaded the bytes to
run extraction, then kept only the platform CDN URL. Those URLs rot — Discord
signed URLs expire in ~24 h, and Slack / Teams / Mattermost URLs need a live bot
token forever — so media became unviewable over time.

This feature keeps a **durable copy of the bytes** and serves them back, while
keeping the platform URL as the identifier everywhere (facts, wiki, frontend,
and `channel_messages` are unchanged). A dead platform link still renders,
because your own server hands back the stored copy.

---

## How it works

The platform URL is used as a **lookup key**, not a fetch target. Bytes live in
a content-addressed blob store; a small `channel_media_refs` collection in Mongo
maps each URL's stable `host+path` to its blob.

```
INGEST  (best-effort — never blocks extraction)
  download bytes ──► Gemini extraction
                └──► save_blob:  sha256(bytes) ──► key "channels/{channel_id}/{sha256}"
                         ├─ BlobBackend.put(key, bytes)   →  GridFS  |  MinIO/S3
                         └─ channel_media_refs.upsert  { url_key = host+path , channel_id , sha256 , mime , size }
                              (query string dropped → survives re-signing;  Telegram URL → not indexed)

SERVE   browser renders  <img src="/api/files/proxy?url=<platform-url>&channel_id=...">
  proxy ─ normalize url → url_key ─ find ref ─ authorize channel ─ open blob
             HIT  → stream stored bytes        (header  X-Media-Source: store)
             MISS → fetch the platform origin   (header  X-Media-Source: origin)

PURGE   channel deletion → delete_prefix("channels/{channel_id}/") + drop its refs
BACKFILL  re-fetch + save_blob for media ingested before this shipped (resumable, idempotent)
```

**Why the platform URL stays:** rewriting every stored URL to an internal link
would force a migration of every fact / wiki page / message, a wiki recompile,
and would break the platform-URL-keyed dedup and frontend matching. Keeping the
URL as the key makes persistence purely **additive and reversible** (a flag),
with no data migration. The read-through proxy is the indirection that turns the
platform URL into local bytes.

**The metadata/byte split** is what makes the backend swappable:

| Layer | Lives in | Responsibility |
| --- | --- | --- |
| `channel_media_refs` + `url_key` + dedup + Telegram guard | **always MongoDB** | identity / lookup / per-channel scoping |
| Raw bytes (`BlobBackend`) | **GridFS** *or* **MinIO/S3** | storage only, keyed by `channels/{channel_id}/{sha256}` |

---

## Backends

| | **GridFS** (default) | **MinIO / S3** |
| --- | --- | --- |
| Stores bytes in | MongoDB (`channel_media` bucket) | Object storage |
| Extra infra | none | a MinIO container, or an S3 bucket |
| Best for | OSS / self-host / low volume | Enterprise / production at scale |
| Trade-off | bytes bloat the DB (backups, cache, replication) at volume | keeps the DB small; storage scales independently |

`channel_media_refs` always lives in Mongo regardless of backend — only the
bytes move.

> **Why GridFS by default?** Zero extra infrastructure for a self-hoster. At
> production volume, media in Mongo inflates backups and competes with the
> operational working set for cache — that is when MinIO/S3 earns its keep.

---

## Configuration

All settings have safe defaults; the feature is **on** out of the box on GridFS.

| Env var | Default | Purpose |
| --- | --- | --- |
| `CHANNEL_MEDIA_PERSIST` | `true` | Persist media bytes at ingestion (kill switch). |
| `CHANNEL_MEDIA_READ_THROUGH` | `true` | Serve stored bytes via the proxy (kill switch). |
| `CHANNEL_MEDIA_BACKEND` | `gridfs` | `gridfs` or `minio`. |
| `CHANNEL_MEDIA_MINIO_ENDPOINT` | `http://localhost:9000` | MinIO endpoint. **Leave empty for real AWS S3.** |
| `CHANNEL_MEDIA_MINIO_ACCESS_KEY` | — | Access key. |
| `CHANNEL_MEDIA_MINIO_SECRET_KEY` | — | Secret key. |
| `CHANNEL_MEDIA_MINIO_BUCKET` | `atlas-media` | Bucket name (must be **private**). |
| `CHANNEL_MEDIA_MINIO_REGION` | `us-east-1` | Region (used for S3). |
| `CHANNEL_MEDIA_MINIO_SECURE` | `false` | `true` for HTTPS — **set `true` whenever MinIO is not on localhost.** |
| `MEDIA_MAX_FILE_SIZE_MB` | `20` | Cap for non-video files; larger are skipped. |
| `MEDIA_VIDEO_MAX_SIZE_MB` | `100` | Cap for video. |

---

## Using MinIO / S3

### Local MinIO (Docker)

1. In `.env`:
   ```bash
   CHANNEL_MEDIA_BACKEND=minio
   CHANNEL_MEDIA_MINIO_ENDPOINT=http://minio:9000   # service name inside compose
   CHANNEL_MEDIA_MINIO_ACCESS_KEY=<your-key>
   CHANNEL_MEDIA_MINIO_SECRET_KEY=<your-secret>
   CHANNEL_MEDIA_MINIO_BUCKET=atlas-media
   ```
2. Start the profile-gated MinIO (also creates the private bucket):
   ```bash
   docker compose --profile minio up -d
   ```
3. Restart the app so it picks up `CHANNEL_MEDIA_BACKEND=minio`.

The default `docker compose up` (no profile) does **not** start MinIO, so the
GridFS default stays zero-infra. MinIO console: `http://localhost:9001`.

### Production AWS S3 (EE)

Leave `CHANNEL_MEDIA_MINIO_ENDPOINT` **empty** so the client targets AWS S3, set
`CHANNEL_MEDIA_MINIO_REGION` + credentials (or an IAM role), keep the bucket
**private**, and set `CHANNEL_MEDIA_MINIO_SECURE=true`.

> **Security:** the bucket must stay private and be served only through the
> authenticated proxy. A public bucket would trade the DB-bloat problem for a
> data-exposure one.

---

## Operations

### Serving endpoints

| Endpoint | Notes |
| --- | --- |
| `GET /api/media/proxy?url=<platform-url>&channel_id=<id>` | Read-through for media. Pass `channel_id` for an exact, authorized lookup. |
| `GET /api/files/proxy?url=<platform-url>&connection_id=<id>&channel_id=<id>` | Read-through for file attachments (renders `<img>` etc.). |

Both authorize the caller against the channel that holds the media (so one
principal cannot read another channel's media), set `nosniff` + a CSP sandbox,
and report `X-Media-Source: store` (served from the blob store) or `origin`
(fetched live as a fallback).

### Backfilling already-ingested channels

Media ingested **before** this shipped has no stored copy. Backfill re-fetches
and stores it (anything still reachable; already-expired URLs cannot be
recovered).

```bash
# dry-run first — counts what WOULD be stored, writes nothing
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -X POST "$API/api/admin/channels/$CH/backfill-media" -d '{"dry_run": true}'

# real run
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -X POST "$API/api/admin/channels/$CH/backfill-media" -d '{"dry_run": false}'
```

Body: `dry_run` (bool) and `max_messages` (1–5000, default 500). The run is
**idempotent** (already-stored media is skipped) and **resumable** (it persists
a cursor in `media_backfill_state` and continues from it).

### Stats

```bash
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" "$API/api/admin/media/stats"
# {"total_blobs": N, "total_bytes": N, "total_refs": N}
```

A production smoke test covering ingestion, read-through, link-rot, the channel
ACL, and backfill lives at
[`scripts/smoke_test_media_persistence.sh`](../scripts/smoke_test_media_persistence.sh).

---

## Behaviour & limits

- **Best-effort persistence.** A storage failure never fails ingestion — the
  bytes were already downloaded for extraction; persisting them is a side
  effect, deduped per `(sha256, channel_id)`.
- **Link-rot is defeated going forward, not retroactively.** Media ingested
  after this is durable; old media is rescued by backfill only if its URL is
  still reachable. **Discord** URLs expire ~24 h, so old Discord media is
  usually unrecoverable — backfill it with fresh posts, not old history.
- **Telegram** file URLs embed the bot token in the path, so they are never
  indexed (no ref, no read-through) — by design.
- **Size caps** (`MEDIA_MAX_FILE_SIZE_MB` / `MEDIA_VIDEO_MAX_SIZE_MB`) are
  enforced before buffering; oversized media is skipped.

---

## Troubleshooting

**Backfill reports `download_failed` for everything.** The bot can't fetch from
the platform — usually a missing/stale adapter registration (after a bot
restart the in-memory adapters are lost while the connection still shows
"connected"). Re-register from the stored (encrypted) credentials:
```bash
curl -s -X POST "$API/api/connections/<connection_id>/validate" \
  -H "Authorization: Bearer <api-key>"
```
The bot log should show `bot rebuilt with adapters: …<platform>:<conn>…`.

**Backfill scans 0 messages on a re-run.** A previous run advanced the resume
cursor. The admin endpoint has no `reset`, so clear it:
```bash
mongosh --eval 'db.media_backfill_state.deleteMany({_id:{$regex:"<channel_id>"}})'
```

**`X-Media-Source: origin` instead of `store`.** The media isn't stored yet
(persisted off, or never backfilled), so the proxy fell back to the live
platform. Backfill the channel, or confirm `CHANNEL_MEDIA_PERSIST=true`.

**Media `403`s through the proxy.** The caller lacks access to the channel that
holds that media — expected (the proxy enforces per-channel access on a hit).
