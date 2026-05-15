# LiteLLM Cutover — Operator Runbook

This runbook covers the **PR-A slice** of the `agent-llm-provider-pluggable` change: the cutover from ADK's native Google Gemini path to a single LiteLLM funnel for every agent completion, gated by the `LLM_USE_LITELLM_FOR_GEMINI` feature flag.

## What changed

Before PR-A:

```
LlmAgent(model="gemini-2.5-flash", ...)
   ↓
ADK native path → google.genai.Client.aio.models.generate_content(...)
   ↓
Gemini API  (BYPASSES dispatch_completion + LLMThrottle)
```

Eight other call sites (csv_mapper, decomposer, ask.py, wiki_maintainer, wiki/compiler, media_processor, media_extractors:DocumentExtractor + ImageExtractor) also used `google.genai.Client` directly.

After PR-A:

```
LlmAgent(model="gemini-2.5-flash", ...)
   ↓
resolve_model_object normalises to "gemini/gemini-2.5-flash" and wraps in LiteLlm(...)
   ↓
LiteLLM → dispatch_completion → litellm.acompletion → Gemini API
   ↓
THROUGH the throttle + 429 handling + Ollama-aware cache invalidation
```

Every direct `genai.Client()` call site (except the two `Files-API` extractors — Audio, Video, see below) was migrated to `dispatch_completion`.

## The feature flag

`LLM_USE_LITELLM_FOR_GEMINI` lives on `Settings.llm_use_litellm_for_gemini`.

| Value  | Behaviour                                                                                      |
|--------|------------------------------------------------------------------------------------------------|
| `true` (default) | Gemini bare strings (`gemini-2.5-flash`) normalise to `gemini/gemini-2.5-flash` and wrap in `LiteLlm(...)`. ADK calls flow through `litellm.acompletion`. Cutover is live. |
| `false`          | Gemini bare strings pass through to ADK's native Gemini path. Emergency rollback. |

Ollama paths and any non-Gemini prefixed string (`openai/...`, `anthropic/...`) ALWAYS wrap in `LiteLlm`, regardless of the flag value.

## Cutover plan

### Day 0 — Merge PR-A

1. Verify pytest green (`tests/llm/test_litellm_funnel.py` + `tests/llm/test_ollama_ttl.py` + the migrated-site coverage)
2. Merge to `feat/litellm-embedding-provider` (or whichever working branch)
3. **Default flag is ON in this PR.** Existing installs receive the cutover behaviour immediately on next restart — no operator action needed for the common case
4. Set `LLM_USE_LITELLM_FOR_GEMINI=false` in environments where you want a few days of stability with the legacy path before flipping

### Day 0–7 — Soak

1. Run normal ingestion + QA traffic with the flag on
2. Monitor:
   - **Tool-call success rate.** ADK's QA agent uses tools (search, retrieval). LiteLLM serializes tool_call deltas slightly differently than the native Gemini SDK — the orchestrator must still see the same shape. Watch for `qa_agent` regressions.
   - **Ingestion throughput.** Each `LlmAgent` call now adds one LiteLLM layer to the path; per-call latency may shift slightly.
   - **Token counts.** LiteLLM normalises usage reporting to OpenAI shape; per-call billable token totals should match within ±5% of pre-cutover.
   - **Error rate.** New error class to watch: `litellm.RateLimitError` (was: `google.api_core.exceptions.ResourceExhausted`). The throttle catches both.
3. If a regression surfaces, flip the flag to `false` via env var (no code change required), restart, and file a bug pointing at this runbook.

### Day 7 — Remove the flag

Once a 7-day green window is achieved on the dev cluster, ship a follow-up PR removing the flag entirely:

1. Delete `llm_use_litellm_for_gemini` from `infra/config.py`
2. Delete the flag-off branch in `llm/model_resolver.py:resolve_model_object` (the legacy native path)
3. Delete the env-flip from any deploy manifests
4. Update this runbook with "Flag removed in commit <sha>."

## How to verify the cutover is live

Run a one-shot probe:

```bash
uv run python -c "
import asyncio
from beever_atlas.infra.config import get_settings
from beever_atlas.llm.model_resolver import resolve_model_object

settings = get_settings()
print('flag:', settings.llm_use_litellm_for_gemini)
obj = resolve_model_object('gemini-2.5-flash')
print('type:', type(obj).__name__)  # LiteLlm when flag on; 'str' when flag off
"
```

For a live completion check:

```bash
uv run python -c "
import asyncio
from beever_atlas.services.llm_dispatch import dispatch_completion

async def main():
    r = await dispatch_completion(
        provider='gemini',
        model='gemini/gemini-2.5-flash-lite',
        messages=[{'role': 'user', 'content': 'reply in exactly one word'}],
    )
    print(r.choices[0].message.content)

asyncio.run(main())
"
```

Expected: a one-word reply. If you see an `AuthenticationError`, your `GOOGLE_API_KEY` env isn't set.

## Smoke test against the full stack

When Docker is available:

```bash
make smoke-ingest
```

This runs a small ingest pass against a real Gemini key and checks:
- 16 agents complete without crashes
- Fact extraction produces at least one fact per message
- Tool-using `qa_agent` returns answers (verifies the LiteLLM tool-call path)

Compare timing + token counts against the pre-cutover baseline (captured below).

## Performance baseline

(Captured by the operator running the smoke test in dev cluster. Update on each cutover round.)

| Metric            | Pre-cutover (flag off) | Post-cutover (flag on) | Δ |
|-------------------|------------------------|------------------------|---|
| Smoke ingest p50  | _to fill_              | _to fill_              | _to fill_ |
| Smoke ingest p95  | _to fill_              | _to fill_              | _to fill_ |
| Input tokens     | _to fill_              | _to fill_              | _to fill_ |
| Output tokens    | _to fill_              | _to fill_              | _to fill_ |
| qa_agent tool-call success rate (10 trials) | _to fill_ | _to fill_ | _to fill_ |

Acceptance bar: Δ ≤ 10 % p95 latency, Δ ≤ 5 % token totals, qa_agent tool-call success rate within 1 trial of pre-cutover.

## Known surface area not covered by this cutover

These callers continue to use `google.genai` natively. Each is documented in code with a "DOCUMENTED EXCEPTION" notice referencing this design.

| File                             | Why                                                                                           |
|----------------------------------|-----------------------------------------------------------------------------------------------|
| `services/gemini_batch.py`       | Gemini batch API (50 % cost discount). LiteLLM has no batch primitive.                        |
| `services/media_extractors.py::VideoExtractor` | Files API upload for >20 MB videos. LiteLLM has no normalised file-upload abstraction. |
| `services/media_extractors.py::AudioExtractor` | Files API upload for audio. Same justification as Video.                                |

These three remain on native genai until LiteLLM exposes a unified primitive for each.

## ADK warning suppression

ADK emits this warning every time it constructs a `LiteLlm` wrapping a Gemini model:

```
UserWarning: [GEMINI_VIA_LITELLM] gemini/gemini-2.5-flash: You are using Gemini via LiteLLM ...
Set ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS=true to suppress this warning.
```

Set `ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS=true` in your deployment env to keep logs clean. The warning is informational — ADK's native path is *preferred* for performance, but we deliberately accept the trade-off to gain one funnel for the throttle + observability + future failover.

## Rollback

```bash
export LLM_USE_LITELLM_FOR_GEMINI=false
# restart the server
```

No data is lost. No agent state is touched. The eight migrated call sites continue to flow through `dispatch_completion` (LiteLLM is still used for everything), but ADK falls back to the native Gemini SDK for `LlmAgent` instantiation.

## Related changes

- The `LiteLlm` wrap of every provider is foundational for the broader `agent-llm-provider-pluggable` redesign. Subsequent PRs (PR-B, PR-C, etc.) introduce the `Endpoint`/`Assignment` data model on top of this funnel.
- Per-Endpoint throttle replaces the current per-provider bucket in PR-B. The current per-provider behaviour preserves correctness during the soak window.
