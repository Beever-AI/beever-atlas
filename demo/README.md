# Beever Atlas — Demo Workspace

This directory contains everything needed to run a zero-config local demo of Beever Atlas,
pre-seeded with a Wikipedia corpus about Ada Lovelace, Charles Babbage, and the history of
the Python programming language.

---

## Quick Start

```bash
# 1. Clone and configure
git clone https://github.com/beever-ai/beever-atlas.git
cd beever-atlas
cp .env.example .env

# 2. Edit .env — set at minimum:
#    BEEVER_API_KEYS=dev-key-change-me
#    NEO4J_PASSWORD=beever_atlas_dev
#    WEAVIATE_API_KEY=any-long-random-string
#    CREDENTIAL_MASTER_KEY=$(openssl rand -hex 32)
#    BRIDGE_API_KEY=$(openssl rand -hex 16)

# 3. Start the full stack + seed demo data (zero API keys required)
make demo
```

The seed loader starts automatically after all services are healthy, loads the pre-computed
fixtures, and exits. Seeding takes under 30 seconds.

---

## Requirements

| Requirement | Details |
|-------------|---------|
| **Disk space** | ~4 GB (Docker images + data store volumes) |
| **RAM** | ~4 GB |
| **First-run image pull** | ~3–5 minutes |
| **Docker + Docker Compose** | [Install Docker Desktop](https://docs.docker.com/get-docker/) |
| **`GOOGLE_API_KEY`** | Required for `/api/ask` (Q&A queries). **Not** needed for seeding. Get a free key at [aistudio.google.com](https://aistudio.google.com) — no credit card required. |

> **Seeding** (`make demo`) is zero-API-key and completes in <30 seconds.
> **Asking questions** via `/api/ask` requires `GOOGLE_API_KEY` in `.env` because the QA
> agent uses Google Gemini at query time.

---

## Asking Questions

Once the stack is running and seeded, use `GOOGLE_API_KEY` in `.env`, then:

**Who was Ada Lovelace?**

```bash
curl -N -X POST http://localhost:8000/api/channels/demo-wikipedia/ask \
  -H "Authorization: Bearer dev-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"question":"Who was Ada Lovelace?"}'
```

**What year was Python first released?**

```bash
curl -N -X POST http://localhost:8000/api/channels/demo-wikipedia/ask \
  -H "Authorization: Bearer dev-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"question":"What year was Python first released?"}'
```

The endpoint streams [Server-Sent Events (SSE)](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events):
`thinking` → `tool_call_start` → `tool_call_end` → `response_delta` (answer text) →
`citations` → `done`.

---

## Demo Corpus

The corpus is sourced from Wikipedia under the Creative Commons Attribution-ShareAlike 3.0
Unported licence (CC-BY-SA 3.0). See [Attribution](#attribution) below.

| File | Topic |
|------|-------|
| `corpus/ada-lovelace.md` | Ada Lovelace — biography and early life |
| `corpus/ada-lovelace-contributions.md` | Ada Lovelace — contributions to computing, first algorithm |
| `corpus/charles-babbage.md` | Charles Babbage — Difference Engine and Analytical Engine |
| `corpus/analytical-engine.md` | The Analytical Engine — design, programming, legacy |
| `corpus/python-history.md` | History of the Python programming language |
| `corpus/python-design-philosophy.md` | Python design philosophy and the PEP process |
| `corpus/guido-van-rossum.md` | Guido van Rossum — creator of Python |

The corpus is designed to produce a rich entity graph (Ada Lovelace → collaborated with →
Charles Babbage → built → Analytical Engine; Guido van Rossum → created → Python) and to
demonstrate grounded Q&A with citations.

---

## Fixture Regeneration

The `demo/fixtures/` files are pre-computed outputs of the full ADK ingestion pipeline run
against the corpus. They contain Weaviate embeddings, Neo4j graph statements, and MongoDB
documents — allowing `make demo` to seed everything without any API calls.

To regenerate fixtures after changing the corpus or models:

```bash
# Requires GOOGLE_API_KEY and JINA_API_KEY in .env
make demo-regenerate-fixtures
```

This runs `demo/seed.py --live --write-fixtures` and overwrites the files in `demo/fixtures/`.
Commit the resulting files to make changes available to other users.

See [`demo/fixtures/README.md`](fixtures/README.md) for the fixture file format specification.

---

## Architecture

```
make demo
  └── docker compose -f docker-compose.yml -f demo/docker-compose.demo.yml up --build
        ├── beever-atlas  (FastAPI :8000) — waits for Weaviate, Neo4j, MongoDB, Redis
        ├── web           (React :3000)
        ├── bot           (TypeScript :3001)
        └── seed-loader   (depends_on: beever-atlas healthy)
              └── python demo/seed.py  [--precomputed by default]
```

---

## Attribution

The demo corpus is adapted from Wikipedia articles, licensed under the
**Creative Commons Attribution-ShareAlike 3.0 Unported (CC-BY-SA 3.0)** licence.

- [Ada Lovelace](https://en.wikipedia.org/wiki/Ada_Lovelace) — Wikipedia contributors
- [Charles Babbage](https://en.wikipedia.org/wiki/Charles_Babbage) — Wikipedia contributors
- [Analytical Engine](https://en.wikipedia.org/wiki/Analytical_Engine) — Wikipedia contributors
- [History of Python](https://en.wikipedia.org/wiki/History_of_Python) — Wikipedia contributors
- [Python (programming language)](https://en.wikipedia.org/wiki/Python_(programming_language)) — Wikipedia contributors
- [Guido van Rossum](https://en.wikipedia.org/wiki/Guido_van_Rossum) — Wikipedia contributors

Licence: https://creativecommons.org/licenses/by-sa/3.0/

Each corpus file carries its own attribution header. Attribution is aggregated here per
CC-BY-SA 3.0 requirements. Beever Atlas source code is separately licensed under Apache-2.0
and is not affected by the CC-BY-SA corpus licence.
