# `atlas.yaml` — Declarative Endpoint + Assignment Config

`atlas apply` reads a declarative config file and reconciles the
`endpoints` + `llm_assignments` MongoDB collections to match it. This is the
GitOps path for the `agent-llm-provider-pluggable` change — commit the file,
run `atlas apply`, get a deterministic config.

See also `docs/runbooks/ai-setup.md` for the broader concepts.

## Quick start

```yaml
# atlas.yaml — place in the repo root (or pass --config <path>)
endpoints:
  - name: anthropic-prod
    preset: anthropic
    api_key: ${ANTHROPIC_API_KEY}
    rpm: 100
  - name: google-prod
    preset: google_ai
    api_key: ${GOOGLE_API_KEY}
  - name: ollama-local
    preset: ollama          # auth_type=none — no key field

assignments:
  embedding:
    endpoint: google-prod
    model: gemini-embedding-001
    dimensions: 3072
  qa_agent:
    endpoint: anthropic-prod
    model: claude-sonnet-4-6
    temperature: 0.2
    fallback_endpoint: google-prod
  image_describer:
    endpoint: ollama-local
    model: gemma3:e4b
  # Consumers not listed here inherit the preset's defaults.

preset: claude-quality-gemini-fast
```

```bash
python -m scripts.atlas_apply plan     # print the diff, write nothing
python -m scripts.atlas_apply apply    # apply atomically (idempotent)
python -m scripts.atlas_apply apply --config infra/atlas.yaml
python -m scripts.atlas_apply apply --quiet   # machine-readable; suppresses the human hint
```

## Schema

### `endpoints[]` — each entry

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Operator-facing label; the natural key used to reference the endpoint from `assignments`. |
| `preset` | yes | `google_ai` / `openai` / `anthropic` / `mistral` / `deepseek` / `groq` / `xai` / `minimax` / `cohere` / `voyage` / `jina_ai` / `ollama` / `vllm` / `lmstudio` / `openrouter` / `litellm_proxy` / `bedrock` / `vertex_ai` / `custom`. Fills in the default base URL when `base_url` is omitted. |
| `base_url` | no | Overrides the preset's default URL. Required for `custom` / `litellm_proxy` / `vllm` / `lmstudio`. |
| `auth_type` | no | `api_key` (default) / `aws_iam` / `google_sa` / `none`. The Ollama preset implies `none`; Bedrock implies `aws_iam`; Vertex implies `google_sa`. |
| `api_key` | when `auth_type=api_key` | Encrypted at rest. `${VAR}` interpolation supported. |
| `aws_access_key_id`, `aws_secret_access_key`, `aws_region` | when `auth_type=aws_iam` | Encrypted together as a JSON blob. |
| `google_sa_json` | when `auth_type=google_sa` | Service-account JSON, encrypted. |
| `models` | no | Curated model list surfaced in agent dropdowns. If omitted, the preset's defaults are used; refresh later via the Discover button in the UI. |
| `rpm` | no | Per-Endpoint throttle bucket size. Defaults per provider (Gemini 1000, OpenAI 500, Anthropic 100, Groq 30, Ollama 1000, …). |
| `headers` | no | Extra request headers (map). Merged with per-Assignment `extra_headers` at dispatch time. |
| `tags` | no | Free-form labels. `atlas apply` tags YAML-created endpoints with `atlas-yaml`. |

### `assignments{}` — keyed by consumer name

The 17 consumers: `embedding`, `fact_extractor`, `entity_extractor`,
`cross_batch_validator`, `coreference_resolver`, `contradiction_detector`,
`image_describer`, `video_analyzer`, `audio_transcriber`, `summarizer`,
`document_digester`, `echo`, `wiki_compiler`, `wiki_maintainer`, `qa_agent`,
`qa_router`, `csv_mapper`.

| Field | Required | Notes |
|---|---|---|
| `endpoint` | yes | The `name` of an endpoint defined above. |
| `model` | yes | A model name; combined with the endpoint's preset → LiteLLM model id. |
| `temperature` | no | Per-call override. |
| `max_tokens` | no | Per-call cap. |
| `response_format` | no | `text` or `json`. `json` translates to OpenAI's `{"type": "json_object"}`. |
| `extra_headers` | no | Per-Assignment headers; merged over the endpoint's `headers`. |
| `fallback_endpoint` | no | The `name` of another endpoint; resolved to its UUID at apply time. When the circuit breaker is open and this is set, dispatch routes here. |
| `dimensions` | no | Embedding consumer only — the vector dimension. |
| `task` | no | Embedding consumer only — the provider task hint (e.g. `text-matching` for Jina). |

### `preset` (optional, top-level)

One of `gemini-balanced`, `openai-quality`, `claude-quality-gemini-fast`,
`fully-local`, `custom`. Applied **after** the endpoints are created and
**before** the explicit `assignments` — i.e. the preset seeds defaults for
every consumer, then your explicit `assignments{}` entries override
specific ones. `custom` seeds nothing.

If the preset's required endpoints aren't present (e.g.
`claude-quality-gemini-fast` needs both an Anthropic and a Google AI
endpoint), `atlas apply` fails with `preset requirements not met`.

## `${VAR}` interpolation

Any string field can reference an environment variable: `${ANTHROPIC_API_KEY}`,
`${OLLAMA_HOST}`, etc. Unknown variables expand to empty string. Interpolation
happens at apply time, before parsing — the persisted (encrypted) value is the
resolved plaintext, not the literal `${...}` text.

## Idempotency

`atlas apply` is idempotent: a second run with no YAML changes produces zero
writes (the diff shows `unchanged`). It's **additive** — it creates or updates
endpoints and assignments named in the file; it never deletes endpoints or
assignments that aren't mentioned. To remove an endpoint, delete it via the UI
or `DELETE /api/settings/endpoints/{id}` (which 409s if any assignment still
references it).

## Plan vs apply

- `atlas apply plan` (or `python -m scripts.atlas_apply plan`) — prints the
  diff (`+ create`, `~ update`, ` unchanged`) and a summary line; writes
  nothing. Use it in CI to gate a PR.
- `atlas apply apply` — writes atomically (one upsert per resource).

## Relationship to the other install modes

| Mode | When | How |
|---|---|---|
| **A** — interactive wizard | First local dev install | `./atlas` Step 2 picker |
| **B** — env JSON | Docker / Helm / one-liner | `BEEVER_ENDPOINTS='[...]' BEEVER_PRESET=... ./atlas`, or `BEEVER_LLM_API_KEY=...` shortcut |
| **C** — declarative YAML | Team GitOps | `atlas.yaml` + `atlas apply` (this doc) |

All three write the same collections. The boot-time hydration shim
(`scripts/migrate_to_endpoint_catalog.py`) is a fourth path that runs
automatically on first boot of a *legacy* install (env keys +
`agent_model_config` + `embedding_settings` → new collections), idempotent and
non-destructive.
