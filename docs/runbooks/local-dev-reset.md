# Local dev: reboot and wipe runbook

Recipes for stopping/starting the local dev stack and wiping data
between tests. Pick the scenario, copy the block.

The stack splits into three layers:

| Layer | Where it runs |
|---|---|
| Datastores (Mongo, Weaviate, Neo4j, Redis) | Docker |
| Backend (FastAPI / uvicorn) | Your shell, via `uv` |
| Frontend (Vite) | Your shell, via `npm` |

Backend reads `.env`. Backend connects to `localhost:27017 / 6380 /
8080 / 7687` for the four datastores. Frontend reads `VITE_API_URL`
(should be `http://localhost:8000`).

---

## TL;DR — Wipe everything and start fresh

Run these from the repo root.

```bash
# 1. Stop app processes
pkill -f "uvicorn beever_atlas"
pkill -f "vite"

# 2. Stop containers and wipe data volumes
docker compose down
docker volume rm beever-atlas_mongo_data beever-atlas_weaviate_data beever-atlas_neo4j_data

# 3. Bring infra back up (datastores only — backend + frontend run on host)
docker compose up -d mongodb weaviate neo4j redis

# 4. Wait until all four containers report healthy
until docker compose ps mongodb weaviate neo4j redis 2>&1 | grep -q "(healthy)"; do sleep 2; done

# 5. Start backend in a new terminal
uv run uvicorn beever_atlas.server.app:app --host 127.0.0.1 --port 8000

# 6. Start frontend in another terminal
cd web && npm run dev
```

Open `http://localhost:5173`. Channels list will be empty until you
sync.

---

## Scenario A — Restart app code only (keep data)

When you changed Python/TypeScript and want to pick up the change
without losing the wiki/memories you've already built.

```bash
# Backend: Ctrl-C in the uvicorn terminal, then re-run:
uv run uvicorn beever_atlas.server.app:app --host 127.0.0.1 --port 8000

# Frontend: Vite hot-reloads automatically. If it gets stuck:
#   Ctrl-C in the vite terminal, then re-run:
cd web && npm run dev
```

**Important**: Do NOT `Ctrl-C` uvicorn during an in-progress sync if
`DECOUPLE_EXTRACTION=true`. The background worker dies with uvicorn
and leaves rows stuck in `extraction_status="extracting"`. The
stale-sweep recovers them after 5 minutes, OR see Scenario D below to
unstick them manually.

Safe to restart any time when `DECOUPLE_EXTRACTION=false`.

---

## Scenario B — Stop everything (overnight, weekend)

```bash
pkill -f "uvicorn beever_atlas"
pkill -f "vite"
docker compose down
```

`docker compose down` (without `-v`) keeps data on the volumes. Next
`up` restores everything as you left it.

---

## Scenario C — Wipe data only, keep infra running

When you want fresh state but don't want to wait for containers to
restart.

```bash
# Stop only the backend so it isn't writing while we wipe
pkill -f "uvicorn beever_atlas"

# Wipe collections
docker exec beever-atlas-mongodb-1 mongosh beever_atlas --quiet --eval '
  db.dropDatabase();
'
docker exec beever-atlas-weaviate-1 sh -c 'curl -s -X DELETE -H "Authorization: Bearer $AUTHENTICATION_APIKEY_ALLOWED_KEYS" http://localhost:8080/v1/schema/AtomicFact > /dev/null'
docker exec beever-atlas-neo4j-1 cypher-shell -u neo4j -p beever_atlas_dev "MATCH (n) DETACH DELETE n"
docker exec beever-atlas-redis-1 redis-cli FLUSHDB

# Restart backend
uv run uvicorn beever_atlas.server.app:app --host 127.0.0.1 --port 8000
```

This skips the volume-recreation step (faster than the TL;DR) but
relies on the containers already running.

---

## Scenario D — Unstick orphaned extraction rows (no full wipe)

When the UI shows "X messages stuck" or batches never finish and
`channel_messages.extraction_status` has rows in `"extracting"` that
aren't progressing.

```bash
# Replace <CHANNEL_ID> with the actual id (e.g. fhg8s6z6ip8oifqxmhuaqns78r)
docker exec beever-atlas-mongodb-1 mongosh beever_atlas --quiet --eval '
  db.channel_messages.updateMany(
    {channel_id: "<CHANNEL_ID>", extraction_status: "extracting"},
    {$set: {extraction_status: "pending", claimed_at: null}}
  );
'
```

Then re-trigger sync from the UI. The next worker tick (or inline
SyncRunner if `DECOUPLE_EXTRACTION=false`) will pick them up.

To find the channel id from a name:

```bash
curl -s -H "Authorization: Bearer $(grep '^BEEVER_API_KEYS=' .env | head -1 | cut -d= -f2 | cut -d, -f1)" \
  http://localhost:8000/api/channels \
  | python3 -c "import json,sys; [print(c['channel_id'], c['name']) for c in json.load(sys.stdin)]"
```

---

## Scenario E — Force wiki rebuild without a full resync

When extraction is done but the wiki didn't generate, OR the wiki is
stale and you want to regenerate from current memory.

```bash
# Replace <CHANNEL_ID>
curl -s -X POST \
  -H "Authorization: Bearer $(grep '^BEEVER_API_KEYS=' .env | head -1 | cut -d= -f2 | cut -d, -f1)" \
  "http://localhost:8000/api/channels/<CHANNEL_ID>/wiki/refresh?mode=rebuild&force=true"
```

`mode` options:
- `update` — refresh page contents, keep folder structure
- `reorganize` — also re-run the structure planner
- `rebuild` — snapshot current wiki to history, wipe cache, regenerate
  from scratch (most aggressive)

Add `force=true` to bypass the build-input hash skip (use when only
prompts changed but corpus is identical).

---

## Verify everything is healthy

```bash
# All four datastores up?
curl -s http://localhost:8000/api/health | python3 -m json.tool

# Frontend serving?
curl -sf http://localhost:5173 > /dev/null && echo "vite up" || echo "vite down"

# Which ports are listening?
lsof -nP -iTCP -sTCP:LISTEN | awk '$9 ~ /:(3000|5173|6380|7687|8000|8080|27017)$/'
```

Expected ports:
- `5173` — vite (frontend)
- `8000` — uvicorn (backend)
- `27017` `6380` `8080` `7687` `7474` — Docker (datastores)

---

## Common gotchas

**Redis on host port 6380 (not 6379)**. Docker maps Redis to 6380 to
avoid conflicting with any local Redis install. `.env` must say
`REDIS_URL=redis://localhost:6380`.

**Conda's `(base)` shell does not break anything** — `uv run` uses the
project's `.venv` regardless of conda. Optional: `conda deactivate`
for a cleaner prompt.

**`docker compose down -v` wipes volumes**. Plain `docker compose
down` is safe.

**Vite serves source from `web/src`** — no rebuild needed when you
edit `.ts`/`.tsx`. If the hot reload is wrong, hard-refresh in the
browser (Cmd-Shift-R on Mac).

**Backend changes require uvicorn restart** unless launched with
`--reload`. The `--reload` flag is safe in dev but unreliable when
sync is in flight; prefer manual restarts.

**`DECOUPLE_EXTRACTION=true` orphans rows on Ctrl-C** (see Scenario
D). For testing, set it to `false` in `.env` — slower per sync but
safe to interrupt.

---

## Quick env-var cheatsheet

The two flags that change pipeline behavior most:

| Var | Values | Effect |
|---|---|---|
| `DECOUPLE_EXTRACTION` | `true` / `false` | `true` = sync HTTP returns fast, background worker does LLM work. `false` = sync HTTP blocks until extraction is done. Default `true` for prod, set `false` for safer dev. |
| `SYNC_BATCH_SIZE` | int (default 50) | Messages per batch. Smaller → more granular progress, more LLM calls. |
| `INGEST_BATCH_CONCURRENCY` | int (default 4) | How many batches run in parallel against Gemini. |
| `WIKI_MAINTENANCE_MODE` | `auto` / `manual` | `auto` = wiki rebuilds on memory_settled. `manual` = wiki only rebuilds when you click Maintain. |

Any change to `.env` requires restarting uvicorn.
