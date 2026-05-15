# AI Setup — Endpoint + Assignment Configuration

This runbook covers the unified LLM configuration layer introduced by the
`agent-llm-provider-pluggable` change. It replaces the per-layer "Embedding
provider" and "Agent Models" surfaces with one model: **Endpoints** and
**Assignments**.

## Concepts

| Term | What it is |
|---|---|
| **Endpoint** | A single LLM endpoint Atlas can talk to — a preset (OpenAI, Anthropic, Google AI, Ollama, …) or a custom OpenAI-compatible URL. Carries: UUID, name, base URL, encrypted credential, RPM budget, curated model list, auth type. |
| **Assignment** | A per-consumer routing record: `{consumer, endpoint_id, model, temperature?, max_tokens?, response_format?, fallback_endpoint_id?}`. There are 17 consumers — the 16 ADK agents plus `embedding`. |
| **Preset** | Two kinds: *endpoint presets* (autofill the Add Endpoint form for a known provider) and *apply presets* (`gemini-balanced` / `openai-quality` / `claude-quality-gemini-fast` / `fully-local`) which seed all 17 Assignments at once. |
| **Consumer** | One of: `embedding`, `fact_extractor`, `entity_extractor`, `cross_batch_validator`, `coreference_resolver`, `contradiction_detector`, `image_describer`, `video_analyzer`, `audio_transcriber`, `summarizer`, `document_digester`, `echo`, `wiki_compiler`, `wiki_maintainer`, `qa_agent`, `qa_router`, `csv_mapper`. |

Add a key once → it's usable everywhere. A single Gemini key serves both
embedding (`gemini-embedding-001`) and the chat agents.

## Three ways to configure

### 1. Settings UI — `Settings → AI Setup`

- **Quick start** — click a preset chip; it seeds every Assignment and shows
  a confirmation banner ("Applied X to N assignments; M kept their custom
  params").
- **Endpoints** — `+ Add endpoint` opens an inline form with preset chips
  (autofill base URL + default models), Name / Base URL / API key / Models
  fields, and a "Get an API key →" link. Each endpoint row has **Test**
  (1-token probe), **Discover** (fetches `/v1/models` and writes the list
  back), and **Delete** (blocked with 409 if any Assignment references it).
- **Assignments** — rows grouped by category. Each has an Endpoint dropdown
  + a Model dropdown (incompatible models disabled — e.g. `qa_agent` can't
  pick a non-tool model), capability badges (🔨 tools / 👁 vision / 🎤 audio),
  and a cost-per-row hint.

### 2. `atlas` interactive wizard — Step 2

`./atlas` Step 2 is a provider picker (Google Gemini / OpenAI / Anthropic /
Mistral / DeepSeek / Groq / MiniMax / Ollama / Custom). It writes
`LLM_FAST_MODEL` / `LLM_QUALITY_MODEL` (LiteLLM-prefixed) + the provider's
key env var. Optional follow-up: "Configure a second provider for hybrid
setups?".

### 3. Declarative `atlas apply` — for CI / Docker / GitOps

**Env JSON envelope (Mode B):**
```bash
BEEVER_ENDPOINTS='[
  {"name":"google","preset":"google_ai","api_key":"$GOOGLE_API_KEY"},
  {"name":"anthropic","preset":"anthropic","api_key":"$ANTHROPIC_API_KEY"}
]' \
BEEVER_PRESET=claude-quality-gemini-fast \
python -m scripts.atlas_apply apply
```

**Single-provider shortcut:**
```bash
BEEVER_LLM_API_KEY=AIza...  ./atlas        # auto-detects Google AI, applies gemini-balanced
```

**Declarative YAML (Mode C):**
```yaml
# atlas.yaml
endpoints:
  - name: anthropic-prod
    preset: anthropic
    api_key: ${ANTHROPIC_API_KEY}
    rpm: 100
  - name: openai-prod
    preset: openai
    api_key: ${OPENAI_API_KEY}
  - name: ollama-local
    preset: ollama          # auth_type=none — no key

assignments:
  embedding:        { endpoint: openai-prod,    model: text-embedding-3-large }
  qa_agent:         { endpoint: anthropic-prod,  model: claude-sonnet-4-6, temperature: 0.2,
                      fallback_endpoint: openai-prod }
  image_describer:  { endpoint: ollama-local,    model: gemma3:e4b }
  # consumers not listed here get the preset's defaults

preset: claude-quality-gemini-fast
```
```bash
python -m scripts.atlas_apply plan    # show the diff
python -m scripts.atlas_apply apply   # write atomically — idempotent
```

`apply` is idempotent: re-running with no YAML changes produces zero writes.
It's additive — YAML adds/updates endpoints + assignments; it never deletes.

## REST API

| Route | Purpose |
|---|---|
| `GET /api/settings/endpoints` | list (credentials masked) |
| `POST /api/settings/endpoints` | create (encrypts the credential) |
| `GET /api/settings/endpoints/{id}` | fetch one |
| `PUT /api/settings/endpoints/{id}` | update (hot-reloads the runtime credential cache) |
| `DELETE /api/settings/endpoints/{id}` | delete — 409 if referenced by any Assignment |
| `POST /api/settings/endpoints/{id}/test` | 1-token probe; credential-marker-redacted errors |
| `POST /api/settings/endpoints/{id}/discover` | fetch `/v1/models` (or `/api/tags` for Ollama) |
| `GET /api/settings/assignments` | list + `default_consumers` + `capabilities` |
| `PUT /api/settings/assignments/{consumer}` | upsert — 422 on unknown consumer or incompatible model (with `suggested[]`) |
| `DELETE /api/settings/assignments/{consumer}` | clear |
| `POST /api/settings/assignments/preset` | preview (`confirm:false`) / apply (`confirm:true`) with diff; preserves custom-param Assignments unless `force_overwrite_custom` |

All routes require the standard Bearer auth. Credentials are AES-256-GCM
encrypted at rest with `CREDENTIAL_MASTER_KEY`, decrypted into a
process-local cache at boot, never returned in plaintext, never logged.

## Capability validation

Some consumers require model capabilities:

| Consumer | Required |
|---|---|
| `qa_agent`, `qa_router` | `tools` (function-calling) |
| `image_describer`, `video_analyzer`, `document_digester` | `vision` |
| `audio_transcriber` | `audio` |

Assigning an incompatible model (e.g. `qa_agent` → `deepseek/deepseek-reasoner`,
which lacks tools) returns 422 with `missing_capabilities` + `suggested[]`
(compatible alternatives from your existing endpoints, cheapest first; local
models preferred). The UI greys out incompatible models in the dropdown.
Unknown models fall back to a heuristic (`capability_infer.py` /
`knownModels.ts`) and the operator can override the flags on the endpoint.

## Failover

Set an Assignment's `fallback_endpoint_id` (UI drawer — coming; or via the
API / `atlas.yaml`'s `fallback_endpoint`). When the circuit breaker is open
and a fallback is configured, dispatch routes to the fallback endpoint
(preserving the Assignment's `model`). When the breaker is open and no
fallback is configured, the call fast-fails with
`CircuitBreakerOpenForBothPrimaryAndFallback` instead of a slow timeout.

> Note: today a single global circuit breaker drives failover. Per-Endpoint
> breaker state is a follow-up — until then, an outage on any provider can
> trigger cross-Endpoint failover.

## Migration from a legacy install

On first boot after upgrade, a hydration shim runs automatically (idempotent):
when the `endpoints` collection is empty AND legacy config exists (env keys,
`agent_model_config`, `embedding_settings`, `OLLAMA_ENABLED`), it synthesises
one Endpoint per credentialed provider + one Assignment per consumer. The
legacy collections are NOT deleted — they remain authoritative for the old
read paths until a future Phase-5 cleanup. Operators who never open AI Setup
keep working on the env + legacy-route path indefinitely.

## Cutover flag

`LLM_USE_LITELLM_FOR_GEMINI` (default `true`) controls whether Gemini chat
calls flow through LiteLLM (`dispatch_completion` → throttle) or ADK's native
`google.genai` path. Set it `false` for an emergency rollback to the
pre-cutover behaviour. See `docs/runbooks/litellm-cutover.md` for the soak
plan; the flag is removed after a 7-day green soak.

## The one operator caveat

Per-Assignment advanced params (temperature / max_tokens / response_format /
fallback) are supported by the data model and the API, but the UI currently
only edits endpoint + model per row — the advanced-params drawer is a
follow-up. To set them today, use `atlas apply` with an `atlas.yaml` or
`PUT /api/settings/assignments/{consumer}` directly. Bedrock / Vertex auth
(IAM / service-account JSON) is reserved in the data model but the Add
Endpoint UI form only surfaces `api_key` / `none` — use the API for those.
