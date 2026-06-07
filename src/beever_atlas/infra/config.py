"""Application configuration loaded from environment variables."""

import logging
from functools import lru_cache
from typing import ClassVar, Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def validate_keys_disjoint(
    api_keys: str,
    bridge_api_key: str,
    mcp_api_keys: str,
) -> None:
    """Assert that MCP keys are disjoint from user keys and the bridge key.

    Called at boot time (inside the Settings model validator). Raises
    ``ValueError`` if any key appears in more than one pool so a
    misconfiguration is caught before any request is served.
    """

    def _split(raw: str) -> set[str]:
        return {k.strip() for k in raw.split(",") if k.strip()}

    user_pool = _split(api_keys)
    bridge_pool = _split(bridge_api_key)
    mcp_pool = _split(mcp_api_keys)

    overlap_user_mcp = user_pool & mcp_pool
    if overlap_user_mcp:
        raise ValueError(
            "BEEVER_MCP_API_KEYS overlaps with BEEVER_API_KEYS. "
            "MCP keys must be distinct from user keys. "
            f"Offending key(s) detected (not shown to avoid leaking secrets). "
            f"Count: {len(overlap_user_mcp)}"
        )

    overlap_bridge_mcp = bridge_pool & mcp_pool
    if overlap_bridge_mcp:
        raise ValueError(
            "BEEVER_MCP_API_KEYS overlaps with BRIDGE_API_KEY. "
            "MCP keys must be distinct from the bridge key. "
            f"Count: {len(overlap_bridge_mcp)}"
        )

    # NOTE: user_pool ↔ bridge_pool overlap is intentionally NOT asserted here.
    # The legacy BEEVER_ALLOW_BRIDGE_AS_USER emergency override (handled by
    # require_user) is orthogonal; some dev/test fixtures reuse the same
    # token for both roles. The H4 hardening already rejects bridge keys on
    # user routes at the request boundary when the override is off.


class ConfigurationError(RuntimeError):
    """Raised when feature-flag coupling or settings are invalid."""


# Issue #41 — the well-known development placeholder shipped in `.env.example`
# (and seeded by the `atlas` installer when Python is missing without this fix).
# It IS valid 64-char hex so the bare hex_ok check would pass it, but using it
# as the AES-256-GCM master key makes "encrypted" credentials effectively
# plaintext to anyone who reads the public `.env.example`. The validator
# rejects this exact value: hard error in production, loud warning in dev.
# The `tests/infra/test_credential_master_key_validation.py` regression test
# asserts this constant matches the value in `.env.example` so the two
# sources cannot drift.
_INSECURE_PLACEHOLDER_KEY = "00000000000000000000000000000000000000000000000000000000deadbeef"


class Settings(BaseSettings):
    """Beever Atlas configuration — all values from env vars."""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "populate_by_name": True,
    }

    # Data stores
    weaviate_url: str = Field(default="http://localhost:8080")
    weaviate_api_key: str = Field(default="")
    neo4j_uri: str = Field(default="bolt://localhost:7687")
    neo4j_auth: str = Field(default="neo4j/beever_atlas_dev")
    # Name of the Neo4j database targeted by per-session operations. Used by
    # destructive ops (e.g. /api/dev/reset) to scope wipes away from the
    # default graph when a shared cluster hosts multiple tenants.
    neo4j_database: str = Field(default="neo4j", alias="NEO4J_DATABASE")
    neo4j_batch_name_vector: bool = Field(
        default=True,
        alias="NEO4J_BATCH_NAME_VECTOR",
        description="When true, persister batches name_vector writes via a single UNWIND Cypher call. Falls back to per-entity on failure.",
    )
    neo4j_relationship_stub_endpoints: bool = Field(
        default=True,
        alias="NEO4J_RELATIONSHIP_STUB_ENDPOINTS",
        description="When true, MATCH→MERGE in upsert_relationship + batch_create_episodic_links auto-creates stub Entity nodes for unknown endpoint names. Eliminates silent relationship loss from cross-batch races. Set false to revert to legacy MATCH-and-skip behaviour.",
    )
    mongodb_uri: str = Field(default="mongodb://localhost:27017/beever_atlas")
    redis_url: str = Field(default="redis://localhost:6379")

    # LLM providers
    google_api_key: str = Field(default="")

    # External services
    jina_api_key: str = Field(default="")
    tavily_api_key: str = Field(default="")

    # LLM model tiers (ADK pipeline)
    llm_fast_model: str = Field(default="gemini-2.5-flash")
    llm_quality_model: str = Field(default="gemini-2.5-flash")

    # Cutover flag for the agent-llm-provider-pluggable change. When True,
    # every provider — including Gemini — is wrapped in ``LiteLlm(...)`` inside
    # ``resolve_model_object``, so completions flow through ``litellm.acompletion``
    # (and therefore through ``dispatch_completion`` + ``LLMThrottle``) instead of
    # ADK's native ``google.genai`` client.
    #
    # DEFAULT IS FALSE (since F12). The post-cutover True default silently broke
    # the extraction pipeline: ADK's LiteLlm wrapper does NOT translate
    # ``GenerateContentConfig.response_mime_type="application/json"`` (set by
    # fact_extractor, entity_extractor, coreference_resolver, …) into LiteLLM's
    # ``response_format`` parameter. The model returned unstructured text, the
    # fact-extractor recovery parser found no fact array, and every batch
    # persisted ``facts=0 entities=0``. LLM calls all reported ``ok: true`` so
    # the regression was invisible to operators until they checked the wiki.
    #
    # With the default False, Gemini agent calls go through ADK's native
    # ``google.genai`` client which fully honors ``response_mime_type`` and
    # produces extractable JSON. Non-Gemini providers (OpenAI, Anthropic,
    # Mistral, DeepSeek, Groq, MiniMax, Ollama) still flow through LiteLlm
    # — they aren't affected by this flag. The unified ``dispatch_completion``
    # path (used by the QA pipeline and embedding shim) is also unaffected;
    # only the per-agent ADK ``LlmAgent`` path returns to native.
    #
    # Set to True via env if a future change makes the LiteLlm wrapper honor
    # response_mime_type / response_format symmetrically — then we can fully
    # close the cutover. Until that lands, False is the only safe default.
    llm_use_litellm_for_gemini: bool = Field(default=False)

    # SSRF guard for the operator-facing Endpoint Test/Discover routes. When
    # True, ``base_url`` is resolved + validated against ``infra.http_safe``
    # before any outbound probe — private/link-local/metadata IPs are refused.
    # Default False so the first-class "fully local (Ollama/vLLM/LM Studio)"
    # presets (which point at ``localhost``) keep working out of the box;
    # turn it on for hardened multi-operator deployments.
    llm_endpoint_ssrf_guard: bool = Field(default=False)

    # Pipeline config
    # memory-then-wiki-pipeline-realignment perf P0 — bumped from 10 to 25.
    # Combined with default ``ingest_batch_concurrency=4`` this gives the
    # ExtractionWorker a 100-message claim_size per tick, reducing the
    # tick count by 2.5× on bulk syncs.
    sync_batch_size: int = Field(default=25)
    sync_max_messages: int = Field(default=1000)
    quality_threshold: float = Field(default=0.5)
    entity_threshold: float = Field(default=0.6)
    max_facts_per_message: int = Field(default=2)
    sync_batch_timeout_seconds: int = Field(default=600)
    # When true, the PreprocessorAgent KEEPS messages flagged as bot-authored
    # (top-level ``is_bot`` or ``raw_metadata.is_bot``) instead of dropping them
    # as integration noise. Needed when "people" are posted via webhooks — e.g.
    # Discord webhook personas (each message carries a custom username + avatar
    # but Discord stamps ``author.bot=true``), or community/bridged webhooks.
    # Empty-text and system-subtype messages are still skipped. Default False
    # preserves CI/deploy-bot noise filtering for normal deployments.
    ingest_bot_messages: bool = Field(
        default=False,
        alias="INGEST_BOT_MESSAGES",
        description="Keep bot/webhook-authored messages during preprocessing (e.g. Discord webhook personas) instead of dropping them as noise.",
    )

    # ── Embedding (provider-pluggable via LiteLLM) ────────────────────────
    # Defaults preserve the legacy Jina-v4 @ 2048d behaviour bit-for-bit so
    # existing installations keep working without a .env edit. Set
    # EMBEDDING_PROVIDER + EMBEDDING_MODEL + EMBEDDING_DIMENSIONS to switch
    # providers (OpenAI / Cohere / Voyage / Gemini / Ollama / etc).
    # Switching on a populated install requires `make reembed-all` first
    # (the boot-time dim guard refuses to start otherwise).
    embedding_provider: str = Field(default="jina_ai")
    embedding_model: str = Field(default="jina-embeddings-v4")
    embedding_dimensions: int = Field(default=2048)
    embedding_rpm: int = Field(default=500)
    embedding_api_base: str = Field(default="")
    embedding_api_key: str = Field(default="")
    embedding_task: str = Field(default="text-matching")
    # Boot-time dimension guard — refuses to start when configured dim
    # disagrees with the dim already persisted to Weaviate. Set false to
    # bypass (loud WARN per boot, no abort).
    embedding_dim_guard: bool = Field(default=True)
    # In-flight LiteLLM batches during a re-embed migration job.
    embedding_reembed_concurrency: int = Field(default=4, ge=1, le=16)

    # ── Legacy Jina aliases (DEPRECATED — see Embedding block above) ──────
    # Loaded for one minor release; an init-time bridge copies these into the
    # generic embedding_* fields when the new ones are unset, with a
    # one-shot deprecation warning per field. Removed in v0.3.
    jina_api_url: str = Field(default="https://api.jina.ai/v1/embeddings")
    jina_model: str = Field(default="jina-embeddings-v4")
    jina_dimensions: int = Field(default=2048)

    # File import
    file_import_llm_mapping_enabled: bool = Field(default=True)
    file_import_staging_dir: str = Field(default=".omc/imports")
    file_import_staging_ttl_seconds: int = Field(default=3600)
    file_import_max_rows: int = Field(default=100000)

    # Coreference resolution
    coref_enabled: bool = Field(default=True)
    coref_history_limit: int = Field(default=20)
    coref_model: str = Field(default="gemini-2.5-flash")

    # Semantic entity deduplication
    entity_similarity_threshold: float = Field(default=0.85)
    merge_rejection_ttl_days: int = Field(default=30)

    # Cross-batch validator — P0-3 (plan ``pipeline-cost-latency-reduction-v2.md``).
    # When True, the cross_batch_validator stage runs a deterministic
    # ``BaseAgent`` (name normalization + embedding cosine similarity)
    # instead of the legacy LLM-based validator. Default True — the
    # deterministic path eliminates ~24 Gemini calls/sync and removes the
    # ``json_recovery: truncated validation result`` failure mode. The
    # legacy LlmAgent path was removed in this change; the flag is
    # retained for forward compatibility (a False value logs a one-shot
    # WARN and falls through to the deterministic agent). Roll back via
    # git revert if a regression is observed during the soak window.
    cross_batch_validator_deterministic: bool = Field(default=True)
    # When True (default), the deterministic validator falls back to a
    # bounded LLM call (max 5 pairs per batch) for ambiguous cosine band
    # 0.85–0.92. Architect demanded the safety net stay ON until 2 weeks
    # of soak data confirm the deterministic-only path is safe to default
    # OFF. The per-batch fallback counter is logged as
    # ``cross_batch_validator_llm_fallback_count`` for calibration.
    cross_batch_validator_llm_fallback: bool = Field(default=True)

    # Multimodal expansion
    media_video_max_duration_minutes: int = Field(default=10)
    media_video_max_size_mb: int = Field(default=100)
    media_audio_max_duration_minutes: int = Field(default=30)
    media_office_max_chars: int = Field(default=10000)
    whisper_api_url: str = Field(default="https://api.openai.com/v1/audio/transcriptions")
    openai_api_key: str = Field(default="")

    # Semantic search
    semantic_search_min_similarity: float = Field(default=0.7)

    # Hybrid search alpha controls the BM25 / vector blend.
    # 0.0 = pure BM25 (keyword only), 1.0 = pure vector (semantic only).
    # Default 0.6 gives a slight semantic bias while retaining keyword recall.
    weaviate_hybrid_alpha: float = Field(default=0.6, alias="WEAVIATE_HYBRID_ALPHA")

    # Temporal fact lifecycle
    contradiction_confidence_threshold: float = Field(default=0.8)
    contradiction_flag_threshold: float = Field(default=0.5)

    # P0-1 (pipeline-cost-latency-reduction-v2): when True, BatchProcessor
    # skips its per-batch detached contradiction check; a single bulk pass
    # fires post-sync on ``memory_settled`` via ``check_and_supersede_for_channel``.
    # When False, the legacy per-batch fire-and-forget behaviour is restored
    # (kill switch for emergency rollback without redeploy).
    defer_contradiction: bool = Field(default=True, alias="DEFER_CONTRADICTION")

    # Provider rate limits (requests per minute)
    gemini_rpm: int = Field(default=300)
    jina_rpm: int = Field(default=500)

    # Bounded inter-batch concurrency (1–8).
    # Default 4: live telemetry showed p95 semaphore_wait ~518s at concurrency=2
    # on 11-batch syncs — the semaphore was the dominant bottleneck, not Gemini quota.
    ingest_batch_concurrency: int = Field(default=4, ge=1, le=8)

    # Intra-batch contradiction detection concurrency (1–16)
    contradiction_concurrency: int = Field(default=4, ge=1, le=16)

    # Bounded concurrent in-flight Gemini image description calls (1–16)
    image_extractor_concurrency: int = Field(default=4, ge=1, le=16)

    # Cross-batch thread context
    cross_batch_thread_context_enabled: bool = Field(default=True)
    thread_context_max_length: int = Field(default=200)

    # Soft orphan handling
    orphan_grace_period_days: int = Field(default=7)

    # Reconciler
    reconciler_interval_minutes: int = Field(default=15)

    # Consolidation pipeline
    cluster_similarity_threshold: float = Field(default=0.6)
    cluster_merge_threshold: float = Field(default=0.85)
    cluster_max_size: int = Field(default=100)
    consolidation_max_concurrent_llm: int = Field(default=5)
    consolidation_enabled: bool = Field(default=True)
    # Defer cluster/channel summary LLM calls from per-batch consolidation to
    # the ``memory_settled`` event so summaries fire exactly once per channel
    # per sync (against the stable post-drain state) instead of once per
    # extraction batch. Saves ~40-80s and ~$0.05 per 25-batch sync. Set to
    # ``false`` to restore the legacy per-batch behaviour.
    consolidation_summarize_on_settle: bool = Field(
        default=True,
        alias="CONSOLIDATION_SUMMARIZE_ON_SETTLE",
    )

    # Citation registry — enterprise citation architecture.
    # Enabled by default: QA tool outputs flow through SourceRegistry and
    # the LLM emits [src:xxx] tags rewritten to [N] at stream time. Set
    # CITATION_REGISTRY_ENABLED=false to revert to the legacy regex path.
    citation_registry_enabled: bool = Field(default=True)

    # Application
    beever_api_url: str = Field(default="http://localhost:8000")
    cors_origins: str = Field(default="http://localhost:5173,http://localhost:3000")
    # Public base URL of the web app (used to turn internal citation routes such
    # as /channel/{id}/wiki/overview into ABSOLUTE http(s) links the chat
    # renderer will keep — its cleanUrl drops bare relative paths). Empty by
    # default: when unset the resolver returns None for internal-route kinds
    # rather than emitting a broken bare path. Trailing slash is stripped so
    # f"{base}{path}" never double-slashes.
    public_web_url: str = Field(default="", alias="PUBLIC_WEB_URL")

    @property
    def public_web_base(self) -> str:
        """Public web base URL with any trailing slash removed (empty if unset)."""
        return (self.public_web_url or "").rstrip("/")

    # Media processing
    media_max_file_size_mb: int = Field(default=20)
    media_vision_timeout_seconds: int = Field(default=180)
    media_vision_model: str = Field(default="gemini-2.5-flash")
    media_supported_image_types: str = Field(default="png,jpg,jpeg,gif,webp")
    media_supported_doc_types: str = Field(default="pdf")

    # PDF chunked extraction
    pdf_chunk_pages: int = Field(default=4)
    pdf_max_pages: int = Field(default=100)
    pdf_summarize_large_docs: bool = Field(default=False)
    pdf_large_doc_threshold: int = Field(default=50)

    # Document digest via LLM agent (disable to skip expensive LLM calls during media processing)
    media_digest_enabled: bool = Field(default=True)

    # Bridge (bot service)
    bridge_url: str = Field(default="http://localhost:3001")
    bridge_api_key: str = Field(default="")
    # When True, accept both constant-time and legacy `==` bridge auth paths
    # for one release cycle. Every legacy accept logs a warning.
    bridge_hmac_dual: bool = Field(default=False, alias="BEEVER_BRIDGE_HMAC_DUAL")
    # Allowlist of external hostnames reachable by outbound MCP tool calls.
    # Empty list disables allowlist enforcement (SSRF guard still rejects
    # private IPs).
    external_mcp_allowlist: list[str] = Field(default_factory=list)

    # Graph database backend
    graph_backend: str = Field(default="neo4j")  # "neo4j", "nebula", or "none"
    nebula_hosts: str = Field(default="127.0.0.1:9669")
    nebula_user: str = Field(default="root")
    nebula_password: str = Field(default="nebula")
    nebula_space: str = Field(default="beever_atlas")

    # Gemini Batch API
    use_batch_api: bool = Field(default=False)
    batch_poll_interval_seconds: int = Field(default=15)
    batch_max_wait_seconds: int = Field(default=3600)
    # memory-then-wiki-pipeline-realignment perf P0 — bumped from 6000
    # to 12000. Gemini 2.5 Flash comfortably handles 12-15k input tokens,
    # and the doubling cuts the number of sub-batches in half (fewer LLM
    # round trips → faster extraction wall time).
    batch_max_prompt_tokens: int = Field(default=12000)
    batch_time_window_seconds: int = Field(default=600)
    # Output-token ceiling for adaptive batching. Projects expected response
    # size per batch so we split BEFORE Gemini hits its max_output_tokens
    # ceiling (entity=65536, fact=131072). Default ~70% of entity ceiling
    # leaves headroom for schema overhead and estimator drift.
    # Set to 0 to disable output-aware batching (input-only, legacy behaviour).
    batch_max_output_tokens: int = Field(default=24000, alias="BATCH_MAX_OUTPUT_TOKENS")
    batch_max_messages: int = Field(
        default=30,
        ge=5,
        le=60,
        description="Hard cap on messages per batch. Derived from observed bench data — successful batches had ≤65 msgs; failure cluster started at 89. Prevents output truncation at the source.",
    )
    llm_outage_breaker_threshold: int = Field(
        default=3,
        ge=1,
        le=10,
        description="After this many consecutive cross-batch Gemini 5xx, fail fast instead of burning per-batch retry budget.",
    )
    fact_max_retries: int = Field(default=3)
    stale_job_threshold_hours: float = Field(default=1.0)

    # Ollama (local models)
    ollama_enabled: bool = Field(default=False)
    ollama_api_base: str = Field(default="http://localhost:11434")

    # LLM structured output (schema-constrained decoding for fact/entity extractors).
    # When True, Gemini receives a Pydantic response_schema and cannot emit malformed JSON.
    # Kill switch: flip to False if SDK/model regression is observed; json_recovery still handles fallback.
    use_llm_structured_output: bool = Field(default=True, alias="USE_LLM_STRUCTURED_OUTPUT")

    # QA agent configuration
    qa_confidence_threshold: float = Field(default=0.4, alias="QA_CONFIDENCE_THRESHOLD")
    external_mcp_servers: str = Field(default="", alias="EXTERNAL_MCP_SERVERS")
    # Strengthened OUTPUT_CONTRACT + registry-style citation tags.
    # Env var: QA_RICH_OUTPUT (preferred) or QA_NEW_PROMPT (legacy alias).
    qa_new_prompt: bool = Field(
        default=False,
        validation_alias=AliasChoices("QA_RICH_OUTPUT", "QA_NEW_PROMPT"),
    )

    # QA Agent Skills (progressive disclosure via ADK SkillToolset).
    # When ON, create_qa_agent() wires an 8-skill pack into the QA LlmAgent
    # so the model can load_skill / load_resource on demand for richer
    # formatted output (timelines, profile cards, comparison tables, etc.).
    # REQUIRES qa_new_prompt=True (QA_RICH_OUTPUT); agent-build raises
    # ConfigurationError if this flag is on while qa_new_prompt is off.
    # DEFAULT OFF.
    qa_skills_enabled: bool = Field(default=False, alias="QA_SKILLS_ENABLED")

    # Onboarding response length monitor.
    # When ON, a warning is logged if a non-deep response exceeds 1500 chars.
    # No truncation occurs — warn-only.
    qa_onboarding_length_monitor: bool = Field(default=True, alias="QA_ONBOARDING_LENGTH_MONITOR")

    # QA history negative-answer filter.
    # When ON, search_qa_history drops entries classified as "refused" so the
    # agent cannot recycle hollow non-answers as context. DEFAULT OFF until
    # false-positive rate is measured against Pass-1 fixtures.
    qa_history_negative_filter: bool = Field(default=False, alias="QA_HISTORY_NEGATIVE_FILTER")

    # QA ADK SSE streaming mode.
    # When ON, runner.run_async() receives RunConfig(streaming_mode=StreamingMode.SSE)
    # so ADK emits partial=True token deltas. response_delta/thinking events fire
    # only on partials; the final aggregate drives turn_complete bookkeeping only.
    # DEFAULT OFF. Flip QA_ADK_STREAMING_SSE=1 to enable.
    qa_adk_streaming_sse: bool = Field(default=False, alias="QA_ADK_STREAMING_SSE")

    # Ingestion/extraction ADK SSE streaming mode (Issue #223).
    # When ON, the BatchProcessor's runner.run_async() receives
    # RunConfig(streaming_mode=StreamingMode.SSE) so ADK dispatches the
    # native google-genai generate_content_stream for the extraction stages
    # (fact/entity/coref/contradiction/validator/summarizer). Streaming keeps
    # the socket warm with incremental chunks so a long (>120s) gemini-2.5-pro
    # call no longer idles past the ~127-131s edge-proxy disconnect threshold
    # that surfaced as aiohttp.ServerDisconnectedError → rows succeeded=0
    # total_facts=0. ADK aggregates the streamed JSON chunks into one assembled
    # LlmResponse BEFORE the after_agent_callback, so the existing partial-JSON
    # recovery + retry ladder runs unchanged on the same assembled text. Safe
    # here because these agents use only response_mime_type=application/json
    # (no output_schema, no tools) and therefore do NOT trigger ADK bug #3599.
    # DEFAULT ON — this is the actual Issue #223 fix; with it OFF the long
    # extraction call still idle-disconnects and yields 0 facts. Verified safe:
    # the same SSE path runs in QA prod, ADK aggregates the streamed JSON before
    # the recovery callback, and an end-to-end re-extraction confirmed facts>0.
    # Set INGEST_ADK_STREAMING_SSE=0 to revert. No-streaming fallback knob:
    # lower BATCH_MAX_MESSAGES (see batch_max_messages above) to ~12-16 and/or
    # cap BATCH_MAX_OUTPUT_TOKENS so each call finishes under the idle ceiling.
    ingest_adk_streaming_sse: bool = Field(default=True, alias="INGEST_ADK_STREAMING_SSE")

    # Multilingual memory & wiki/QA rendering (change: multilingual-native-memory).
    # When ON, ingestion detects BCP-47 source_lang per channel/message,
    # facts/entities are stored in source language, wiki/QA render in requested
    # target language. When OFF, everything hardcodes source_lang="en".
    # Default ON so multilang channels (zh-HK, ja, es, …) work without manual
    # env configuration. Set LANGUAGE_DETECTION_ENABLED=false to force English.
    language_detection_enabled: bool = Field(default=True, alias="LANGUAGE_DETECTION_ENABLED")
    default_target_language: str = Field(default="en", alias="DEFAULT_TARGET_LANGUAGE")
    supported_languages: str = Field(
        default=(
            # CJK + Japanese + Korean (script fast-path)
            "en,zh-HK,zh-TW,zh-CN,ja,ko,"
            # Common European languages (langdetect fallback)
            "es,fr,de,pt,it,nl,sv,da,no,fi,pl,cs,ru,uk,tr,"
            # Non-Latin common scripts
            "ar,he,hi,th,el,vi,id"
        ),
        alias="SUPPORTED_LANGUAGES",
    )
    language_detection_confidence_threshold: float = Field(
        default=0.6, alias="LANGUAGE_DETECTION_CONFIDENCE_THRESHOLD"
    )

    @property
    def supported_languages_list(self) -> list[str]:
        return [s.strip() for s in self.supported_languages.split(",") if s.strip()]

    # Wiki compiler feature flags
    # Phase 1: control-char sanitizer, degenerate-content guard, retry gating.
    # Pure defensive additions — default ON.
    wiki_parse_hardening: bool = Field(default=True, alias="BEEVER_WIKI_PARSE_HARDENING")
    # Phase 2: parallelize title translation with page dispatch. Default ON.
    wiki_parallel_dispatch: bool = Field(default=True, alias="BEEVER_WIKI_PARALLEL_DISPATCH")
    # Phase 3: per-page-kind token budgets. Default ON.
    wiki_token_budget_v2: bool = Field(default=True, alias="BEEVER_WIKI_TOKEN_BUDGET_V2")
    # Phase 4+5: deterministic Key Facts table + delimited response parser. Default OFF.
    wiki_compiler_v2: bool = Field(default=False, alias="BEEVER_WIKI_COMPILER_V2")
    # Issue #223 — stream the wiki page-compile LLM call (up to 32k output
    # tokens) so a long generation does not idle past the ~127-131s edge-proxy
    # disconnect (aiohttp.ServerDisconnectedError → degenerate/fallback wiki
    # content). The streamed chunks are reassembled into the same response shape,
    # so parsing is unchanged. Default ON (the fix). Set 0 to revert.
    wiki_llm_streaming: bool = Field(default=True, alias="WIKI_LLM_STREAMING")

    # Credential encryption
    credential_master_key: str = Field(default="")

    # Deployment environment — gates fail-fast production validation
    beever_env: Literal["development", "production", "test"] = Field(
        default="development", alias="BEEVER_ENV"
    )
    # API bearer tokens (comma-separated)
    api_keys: str = Field(default="", alias="BEEVER_API_KEYS")

    # MCP bearer tokens (comma-separated). MUST be disjoint from
    # BEEVER_API_KEYS and BRIDGE_API_KEY — a boot-time assertion enforces
    # this. Each key identifies an agent instance (not a human user); keys
    # are issued per external project consuming the Atlas MCP surface.
    beever_mcp_api_keys: str = Field(
        default="",
        alias="BEEVER_MCP_API_KEYS",
        description=(
            "Comma-separated bearer keys accepted on the /mcp mount. "
            "MUST be disjoint from BEEVER_API_KEYS and BRIDGE_API_KEY "
            "(boot-time assertion fails otherwise)."
        ),
    )

    # Mount the /mcp server (with auth middleware + ACL enforcement).
    # Default off; flip to true to expose the sole MCP surface.
    beever_mcp_enabled: bool = Field(
        default=False,
        alias="BEEVER_MCP_ENABLED",
        description=(
            "Mount the /mcp server (with auth middleware + ACL enforcement). "
            "This is the sole MCP surface; the legacy unauthenticated /mcp "
            "mount has been retired."
        ),
    )

    # MCP rate-limit backend. "memory" (default) is a per-process sliding
    # window — safe for single-worker deploys. "redis" uses the configured
    # redis_url so counters are shared across workers; required before
    # flipping BEEVER_MCP_ENABLED=true in multi-worker production.
    beever_mcp_rate_limit_backend: Literal["memory", "redis"] = Field(
        default="memory",
        alias="BEEVER_MCP_RATE_LIMIT_BACKEND",
        description=(
            "MCP rate-limiter backend. 'memory' is per-process (v1 default). "
            "'redis' uses REDIS_URL for distributed sliding-window counters."
        ),
    )
    # Admin token for /api/dev/* endpoints
    admin_token: str = Field(default="", alias="BEEVER_ADMIN_TOKEN")

    # Emergency override for security finding H4. When False (the v1.0
    # default), `require_user` rejects BRIDGE_API_KEY on user-facing
    # routes — a leaked bridge key can no longer act as a super-admin
    # on /api/memories, /api/channels/*/data, etc. Set to True only as a
    # temporary escape hatch if a downstream integration breaks; every
    # boot with True logs a loud warning so operators notice.
    allow_bridge_as_user: bool = Field(default=False, alias="BEEVER_ALLOW_BRIDGE_AS_USER")

    # Issue #89 — HMAC-signed scoped tokens for browser-loader URLs.
    # `LOADER_TOKEN_SECRET` is signed-token material distinct from the user
    # API keys and the bridge key, so a leak of one credential type does
    # not compromise the others. Empty in production WARNS (not fails)
    # because raw-key fallback is still active during the migration window.
    loader_token_secret: str = Field(default="", alias="LOADER_TOKEN_SECRET")
    # Token TTL in seconds. 5 minutes is a conservative default chosen to
    # keep the leak window short while still tolerating slow page loads.
    loader_token_ttl: int = Field(default=300, alias="LOADER_TOKEN_TTL")
    # During the migration window, `require_user_loader` falls back to
    # legacy raw `?access_token=` matching when (a) no `?loader_token=` is
    # present, or (b) the signed token verifies false. Flip to False in a
    # follow-up PR after monitoring confirms zero `auth.loader_fallback_raw_key`
    # log lines for the soak window.
    loader_raw_key_fallback: bool = Field(
        default=True,
        alias="BEEVER_LOADER_RAW_KEY_FALLBACK",
    )

    # Dual-read fallback for channel messages.
    # When True, ``GET /api/channels/{channel_id}/messages`` reads from the
    # durable ``channel_messages`` collection populated by the sync runner.
    # Falls back to ``adapter.fetch_history`` when the store is empty for that
    # channel OR a sync is currently running (so partial rows are not surfaced
    # mid-flight). Default OFF — staging soak before flipping in production.
    # See ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/message-store/``
    # → "Dual-read fallback during migration".
    read_from_message_store: bool = Field(default=False, alias="READ_FROM_MESSAGE_STORE")

    # Read-side flag for the file-imports branch in ``api/channels.py``.
    # When True AND ``channel_messages`` carries rows for the requested
    # file channel (``source_id="file"``), the messages tab serves data
    # from the durable Message Store. Otherwise falls back to the legacy
    # ``imported_messages`` collection. Default OFF — staging soak after
    # the migration script runs before flipping in production.
    read_file_imports_from_channel_messages: bool = Field(
        default=False, alias="READ_FILE_IMPORTS_FROM_CHANNEL_MESSAGES"
    )

    # Dual-write window for file imports.
    # When True, ``api/imports.commit_import`` writes new file-import rows
    # to BOTH ``channel_messages`` (the new home) and ``imported_messages``
    # (legacy, kept for instant rollback). Default ON for the soak window;
    # flip OFF after the read flag has been ON in production for one week
    # with zero ``channel_messages_fallback`` (reason="empty_store") log
    # lines. ``channel_messages`` writes are unconditional regardless of
    # this flag — only the legacy collection write is gated.
    write_dual_file_imports: bool = Field(default=True, alias="WRITE_DUAL_FILE_IMPORTS")

    # Background extraction worker flag.
    # When True, ``services/sync_runner.py`` skips the inline
    # ``BatchProcessor.process_messages()`` call after upserting messages
    # to ``channel_messages``. The background ExtractionWorker registered
    # by the scheduler claims the rows in the next tick (default 30 s) and
    # runs the 6-stage ADK pipeline asynchronously. This is the primary
    # lever that makes a Gemini 503 storm survivable — sync (fetch + persist)
    # finishes in seconds; extraction proceeds in the background and retries
    # with exponential backoff. Default ON — the durable channel_messages
    # queue makes uvicorn restarts, worker crashes, and Gemini outages
    # recoverable without re-fetching from the source. Flip OFF only for
    # CI tests that need single-process determinism, or local dev sessions
    # where you Ctrl-C uvicorn often and want to avoid orphan extracting
    # rows (the stale-sweep recovers them in 5 min either way).
    decouple_extraction: bool = Field(default=True, alias="DECOUPLE_EXTRACTION")

    # Durable channel-media persistence flag.
    # When True, ``MediaProcessor`` persists the raw downloaded bytes of
    # channel attachments to the durable ``channel_media`` GridFS bucket at
    # extraction time (best-effort, never blocks extraction). Default ON —
    # the stored copy is what the read-through proxy serves once the platform
    # CDN URL rots (Discord signed URLs expire; Slack/Mattermost/Teams URLs
    # need a live bot token forever). Flip OFF to revert to link-only media.
    channel_media_persist: bool = Field(
        default=True,
        alias="CHANNEL_MEDIA_PERSIST",
        description=(
            "Persist channel media bytes to the durable channel_media GridFS "
            "bucket at extraction time."
        ),
    )

    # Read-through serving flag for channel media.
    # When True, ``/api/files/proxy`` and ``/api/media/proxy`` check the
    # durable blob store first (by url_key) and stream stored bytes on a hit,
    # falling back to the platform CDN via the bridge on a miss. Default ON —
    # turning it OFF serves every request straight from the platform CDN
    # (the legacy behavior) regardless of what is stored.
    channel_media_read_through: bool = Field(
        default=True,
        alias="CHANNEL_MEDIA_READ_THROUGH",
        description=(
            "Serve channel media from the durable blob store when available, "
            "falling back to the platform CDN."
        ),
    )

    # Byte backend selector for channel media. The refs / dedup / url_key
    # metadata ALWAYS stays in Mongo regardless of this setting; only the raw
    # bytes move. 'gridfs' (default) keeps the zero-infra OSS path; 'minio'
    # points bytes at an S3-compatible store (MinIO locally, real AWS S3 for
    # the EE/AWS tier via the CHANNEL_MEDIA_MINIO_* fields below).
    channel_media_backend: str = Field(
        default="gridfs",
        alias="CHANNEL_MEDIA_BACKEND",
        description=(
            "Byte backend for channel media: 'gridfs' (default, zero-infra OSS) "
            "or 'minio' (S3-compatible, EE/AWS tier). Refs always stay in Mongo."
        ),
    )
    channel_media_minio_endpoint: str = Field(
        default="",
        alias="CHANNEL_MEDIA_MINIO_ENDPOINT",
        description=(
            "MinIO S3 endpoint URL e.g. http://localhost:9000. Empty -> use the "
            "real AWS S3 endpoint (EE on AWS)."
        ),
    )
    channel_media_minio_access_key: str = Field(
        default="",
        alias="CHANNEL_MEDIA_MINIO_ACCESS_KEY",
        description="MinIO/S3 access key (= AWS_ACCESS_KEY_ID on AWS).",
    )
    channel_media_minio_secret_key: str = Field(
        default="",
        alias="CHANNEL_MEDIA_MINIO_SECRET_KEY",
        description="MinIO/S3 secret key (= AWS_SECRET_ACCESS_KEY on AWS).",
    )
    channel_media_minio_bucket: str = Field(
        default="atlas-media",
        alias="CHANNEL_MEDIA_MINIO_BUCKET",
        description="Private bucket for channel media bytes; created on startup if absent.",
    )
    channel_media_minio_region: str = Field(
        default="us-east-1",
        alias="CHANNEL_MEDIA_MINIO_REGION",
        description=(
            "botocore requires a region even though MinIO ignores it; us-east-1 "
            "avoids the CreateBucketConfiguration constraint."
        ),
    )
    channel_media_minio_secure: bool = Field(
        default=False,
        alias="CHANNEL_MEDIA_MINIO_SECURE",
        description=(
            "https (True) vs http (False). MUST match the endpoint scheme; "
            "default False for local MinIO, True for AWS."
        ),
    )

    # Tuning knobs (worker tick interval, stale-recovery window, max
    # retries, breaker cooldown, LLM failover enablement, fallback
    # model map) intentionally NOT env-configurable. They live as
    # module constants near the code that uses them
    # (``services/extraction_worker.py``, ``services/circuit_breaker.py``,
    # ``llm/provider.py``). Operator-tunable env vars are reserved for
    # behavior that an on-call would actually flip during an incident
    # — capacity planning belongs in reviewed PRs.

    # Per-page wiki page-store flag.
    # When True, ``WikiCache.get_page`` reads from the ``wiki_pages``
    # collection (one document per (channel_id, target_lang, page_id)).
    # When False, falls back to the legacy ``wiki_cache`` flat-pages-subdoc
    # schema. Writes always go to the new collection so flipping the flag
    # back to OFF after a soak doesn't lose page edits made under the new
    # path. Default OFF — staging soak (48 h) before flipping in production.
    # Per-page incremental update via WikiMaintainer requires PER_PAGE_WIKI=True.
    per_page_wiki: bool = Field(default=False, alias="PER_PAGE_WIKI")

    # WikiMaintainer mode.
    # ``manual``: maintainer marks affected pages is_dirty=True on
    # extraction events; user clicks "Maintain Wiki" to drain the
    # dirty queue on demand. Default — conservative for soak.
    # ``auto``: Karpathy-style — maintainer auto-fires per-page LLM
    # rewrite on every extraction event. Flip to ``auto`` only after
    # the 2-week A/B comparison confirms incremental quality matches
    # full-regenerate quality on three real channels.
    wiki_maintenance_mode: str = Field(default="manual", alias="WIKI_MAINTENANCE_MODE")

    # Auto-trigger an initial wiki build the first time a channel crosses
    # the fact-count threshold under ``WIKI_MAINTENANCE_MODE=auto``. Without
    # this, a brand-new channel sits at "no wiki yet" until the user
    # manually clicks Generate — surprising for the Karpathy-style "wiki
    # is alive" framing. The maintainer is incremental-only and cannot
    # produce the initial structure plan; this flag bridges that gap.
    # Disable to revert to the manual-first-build flow.
    wiki_auto_initial_build: bool = Field(default=True, alias="WIKI_AUTO_INITIAL_BUILD")

    # Minimum extracted-fact count before auto-initial-build fires. A
    # one-fact wiki is worse than no wiki — wait for enough signal to
    # produce a useful structure plan. The check runs on every
    # ``on_extraction_done`` event for channels with no wiki, so the build
    # fires on the first event that crosses this threshold.
    wiki_auto_initial_build_threshold: int = Field(
        default=10, alias="WIKI_AUTO_INITIAL_BUILD_THRESHOLD"
    )

    # ``sync-pipeline-feedback-and-auto-wiki`` Phase 2.
    # Auto-build the channel-overview wiki page once the first extraction
    # batch wave finishes for a channel. The subscriber lives in
    # ``services/auto_overview_subscriber.py`` and runs INDEPENDENTLY of
    # ``WIKI_MAINTENANCE_MODE`` so the "Channel Wiki" tab no longer shows
    # "No Wiki Yet" forever on a fresh sync regardless of maintainer mode.
    # Default True for fresh installs; the lifespan auto-detects upgrades
    # (existing rows in ``wiki_pages`` with ``page_type=overview``) and
    # flips this OFF so existing operators are not surprised by an
    # auto-build on the first post-upgrade sync. The operator override
    # ``AUTO_OVERVIEW_WIKI=true|false`` always wins. Read fresh on every
    # event so a runtime flip takes effect on the very next sync.
    auto_overview_wiki: bool = Field(default=True, alias="AUTO_OVERVIEW_WIKI")

    # Wiki page-voice drift A/B comparator.
    # When True, every successful ``WikiMaintainer.apply_update`` ALSO
    # schedules a fire-and-forget ``compare_apply_update_vs_regenerate``
    # task that builds the regenerate-from-scratch wiki page for the same
    # ``(channel_id, page_id, target_lang)`` and emits a structured
    # ``wiki_drift_report`` log line + persists a row to the
    # ``wiki_drift_reports`` Mongo collection. Default OFF — the comparator
    # doubles LLM cost on the affected page so we only enable it during the
    # 2-week soak that gates flipping ``WIKI_MAINTENANCE_MODE=auto`` to
    # default ON. The maintainer's primary path is unaffected when this
    # flag is OFF.
    wiki_drift_ab: bool = Field(default=False, alias="WIKI_DRIFT_AB")

    # WikiMaintainer per-page debounce window (seconds).
    # On every ``ExtractionWorker.on_extraction_done`` event the maintainer
    # routes facts to affected pages and accumulates them into an in-memory
    # dirty-set, then schedules ONE flush task that sleeps this many seconds
    # before draining + issuing per-page LLM rewrites. A burst of N events
    # touching the same page within the window collapses to a single rewrite
    # (carrying every event's facts). Default 60s — see design D3 in
    # ``openspec/changes/sync-pipeline-feedback-and-auto-wiki/design.md``.
    # Set to 0 to flush immediately (useful in unit tests). The dirty-set is
    # in-memory only; if the worker process crashes mid-window, pending
    # rewrites are lost and the next extraction event re-routes the affected
    # pages to a fresh dirty-set (worst-case loss = one window).
    # P0: lowered from 60s → 10s. The debounce coalesces multiple
    # memory_changed/memory_settled events into a single flush. With
    # the new memory-then-wiki gate, settlement only fires once per
    # channel-drain anyway, so a 60s coalescing window is pure dead
    # time at sync end. 10s preserves coalescing for any straggler
    # events without wasting the full minute waiting on nothing.
    wiki_maintainer_debounce_seconds: int = Field(
        default=60, alias="WIKI_MAINTAINER_DEBOUNCE_SECONDS"
    )

    # WikiMaintainer settle-path debounce window (seconds).
    # Used ONLY by ``on_memory_settled`` (the terminal event that fires once
    # per channel-drain). Unlike ``wiki_maintainer_debounce_seconds`` (60s,
    # designed to coalesce rapid mid-sync events), this window only needs to
    # cover a tiny grace period in case extraction barely missed the
    # queue-drain check. Default 5s. Set to 0 for immediate flush (unit tests).
    wiki_maintainer_settle_debounce_seconds: int = Field(
        default=5, alias="WIKI_MAINTAINER_SETTLE_DEBOUNCE_SECONDS"
    )

    # Per-(channel, page) rate-limit window for the drift comparator.
    # The maintainer skips a comparator invocation when the same
    # ``(channel_id, page_id)`` was last compared less than this many
    # seconds ago. 60s default keeps soak data dense without doubling LLM
    # cost on a busy channel; soak operators may tune to 30s (denser
    # samples) or 300s (cheaper) without redeploy.
    wiki_drift_ab_rate_limit_seconds: int = Field(
        default=60, alias="WIKI_DRIFT_AB_RATE_LIMIT_SECONDS"
    )

    # LLM-native wiki redesign (change ``wiki-llm-native-redesign``).
    # When True, the WikiMaintainer dispatches per-page-kind synthesis
    # prompts (topic / entity / decisions / faq / action_items) instead
    # of the legacy single ``_render_apply_update_prompt`` template,
    # parses ``[[wikilink]]`` cross-references, and emits per-kind
    # ``kind_schema`` payloads for the MCP read tools. When False, the
    # maintainer falls through to the legacy single-prompt path —
    # behaviour is byte-identical to pre-redesign so existing installs
    # are unaffected. Default OFF; flips ON in fresh-install ``.env.example``
    # only after the soak runbook closes (see §9.3 of the change tasks).
    wiki_llm_native_redesign: bool = Field(default=False, alias="WIKI_LLM_NATIVE_REDESIGN")

    # ``llm-wiki-folder-structure`` Phase B+ — enables the structure
    # planner pass that decides folder boundaries between gather and
    # compile. Default ON so the wiki tree always groups topics into
    # navigable folders rather than degenerating to a flat 50-item
    # sidebar after a regular Update. The planner is skipped automatically
    # for sparse channels via ``wiki_min_topics_for_folders``, and the
    # ``?mode=reorganize`` query still forces a re-plan from scratch
    # regardless of this flag. Set ``WIKI_FOLDER_PLANNER=false`` to
    # restore the legacy flat behaviour.
    wiki_folder_planner: bool = Field(default=True, alias="WIKI_FOLDER_PLANNER")

    # Below this many topic clusters the planner skips folder creation
    # entirely — sparse channels read better as a flat list, and the
    # heuristic candidate signals don't accumulate enough evidence to
    # be reliable. Operators can lower it for testing on small
    # channels but the default is conservative.
    wiki_min_topics_for_folders: int = Field(default=6, alias="WIKI_MIN_TOPICS_FOR_FOLDERS")

    # Per-kind drift-A/B sample rate (§8.1). The legacy ``WIKI_DRIFT_AB``
    # rate-limiter applies only to the legacy single-prompt comparison;
    # this knob governs the redesign-vs-legacy A/B that runs alongside.
    # 0.05 = 5% of apply_update calls trigger the per-kind comparison —
    # statistically sufficient given typical page-touch frequency, and
    # avoids doubling LLM cost on every rewrite. 0.0 disables the
    # per-kind sampler entirely.
    wiki_drift_ab_per_kind_sample_rate: float = Field(
        default=0.05, alias="WIKI_DRIFT_AB_PER_KIND_SAMPLE_RATE"
    )

    # Fact-overlap threshold for the page-merge proposal pass. The
    # maintainer compares ``last_facts_seen`` between every pair of
    # pages on each ``on_extraction_done`` and surfaces a merge proposal
    # when Jaccard similarity exceeds this threshold. Operator approves
    # via the curation UI (no auto-merge — proposals only). 0.70 starts
    # conservative; tune up if false-positive rate exceeds 1/week.
    wiki_page_merge_threshold: float = Field(default=0.70, alias="WIKI_PAGE_MERGE_THRESHOLD")

    wiki_topic_compile_parallelism: int = Field(
        default=6,
        ge=1,
        le=16,
        alias="WIKI_TOPIC_COMPILE_PARALLELISM",
        description="Max concurrent topic page LLM compilations. Default 6 matches Gemini Flash RPM ceiling. Set to 16 for ultra-large channels (50+ topics) on paid tiers.",
    )

    # Single-tenant compatibility mode for the v1.0 OSS launch. When True,
    # any authenticated user principal is granted access to channels whose
    # owning PlatformConnection has ``owner_principal_id`` set to the shared
    # sentinel ``"legacy:shared"`` (or missing) — this preserves today's
    # behaviour for solo / operator deployments that never assigned per-user
    # ownership. Post-v1.0 this default flips to ``False`` so multi-tenant
    # operators must explicitly backfill ownership on legacy rows (see
    # ``stores.platform_store.PlatformStore.backfill_legacy_owners``).
    beever_single_tenant: bool = Field(default=True, alias="BEEVER_SINGLE_TENANT")

    # delete-channel-v2 Wave 0 — channel hard-purge reaper. A periodic
    # scheduler job that re-invokes the purge for any ``channel_purge_locks``
    # row whose ``started_at`` is older than ``channel_purge_reaper_threshold_s``
    # (a crashed / partial purge). The threshold MUST exceed the max expected
    # purge duration so a slow-but-succeeding purge is never double-run; the
    # store-level default ``PURGE_LOCK_STALE_AFTER_S`` (900s) is the writer-guard
    # staleness bound and this reaper threshold matches it. Set
    # CHANNEL_PURGE_REAPER_ENABLED=false on processes that don't run the
    # scheduler (e.g. bare worker replicas) to avoid duplicate reapers.
    channel_purge_reaper_enabled: bool = Field(default=True, alias="CHANNEL_PURGE_REAPER_ENABLED")
    channel_purge_reaper_threshold_s: float = Field(
        default=900.0, alias="CHANNEL_PURGE_REAPER_THRESHOLD_S"
    )
    channel_purge_reaper_interval_s: int = Field(
        default=300, alias="CHANNEL_PURGE_REAPER_INTERVAL_S"
    )

    # Media extractor content-hash cache (P0-2).
    # When True, ImageExtractor / VideoExtractor / AudioExtractor skip the
    # Gemini vision/audio call and return the cached description when the same
    # file bytes have been seen before (SHA-256 keyed, stored in MongoDB
    # ``media_cache``). Set MEDIA_CACHE_ENABLED=false to disable globally.
    media_cache_enabled: bool = Field(default=True, alias="MEDIA_CACHE_ENABLED")
    # Bump MEDIA_CACHE_VERSION to invalidate all cached descriptions — e.g.
    # after a Gemini model upgrade. The version is mixed into the hash so old
    # entries become unreachable without requiring a manual collection drop.
    media_cache_version: int = Field(default=1, alias="MEDIA_CACHE_VERSION")

    @property
    def neo4j_user(self) -> str:
        return self.neo4j_auth.split("/")[0]

    @property
    def neo4j_password(self) -> str:
        parts = self.neo4j_auth.split("/", 1)
        return parts[1] if len(parts) > 1 else ""

    # Set of legacy fields that have already emitted a deprecation warning
    # this process. Populated by ``_bridge_legacy_jina_aliases`` so the
    # warning fires exactly once per ``(field, env-var)`` rather than on
    # every Settings re-instantiation in tests.
    _DEPRECATED_LEGACY_WARNED: "ClassVar[set[str]]" = set()

    @model_validator(mode="after")
    def _bridge_legacy_jina_aliases(self) -> "Settings":
        """Copy legacy ``JINA_*`` env values into the generic ``embedding_*``
        fields when the new env vars are unset.

        Detection:
          We can't tell from field values alone whether ``embedding_model``
          equals its default because the operator set it to that, or because
          it fell through. So we check ``os.environ`` directly for the
          ``EMBEDDING_*`` form. If it's absent AND the legacy ``JINA_*`` form
          is present, the bridge copies + warns once.

        Warning policy:
          One WARN line per ``(legacy_var → new_var)`` pair per process.
          Tests that re-instantiate Settings won't spam the log.

        Removed in v0.3 — at which point the legacy fields can also be
        deleted from this Settings class and `.env.example`.
        """
        import os

        bridges = (
            ("EMBEDDING_API_BASE", "JINA_API_URL", "embedding_api_base", "jina_api_url"),
            ("EMBEDDING_MODEL", "JINA_MODEL", "embedding_model", "jina_model"),
            ("EMBEDDING_DIMENSIONS", "JINA_DIMENSIONS", "embedding_dimensions", "jina_dimensions"),
            ("EMBEDDING_RPM", "JINA_RPM", "embedding_rpm", "jina_rpm"),
        )

        for new_env, legacy_env, new_attr, legacy_attr in bridges:
            new_in_env = new_env in os.environ
            legacy_in_env = legacy_env in os.environ

            # Only bridge when the new field is still at its class default —
            # protects explicit constructor kwargs from being clobbered (test
            # setups, programmatic overrides). Operators using only env vars
            # always hit this branch because the new field defaulted in.
            new_default = Settings.model_fields[new_attr].default
            new_at_default = getattr(self, new_attr) == new_default

            if not new_in_env and legacy_in_env and new_at_default:
                legacy_value = getattr(self, legacy_attr)
                setattr(self, new_attr, legacy_value)
                marker = f"{legacy_env}->{new_env}"
                if marker not in Settings._DEPRECATED_LEGACY_WARNED:
                    Settings._DEPRECATED_LEGACY_WARNED.add(marker)
                    logger.warning(
                        "config: %s is deprecated, mapped → %s=%r (remove from .env in v0.3)",
                        legacy_env,
                        new_env,
                        legacy_value,
                    )
            elif new_in_env and legacy_in_env:
                marker = f"{legacy_env}+{new_env}"
                if marker not in Settings._DEPRECATED_LEGACY_WARNED:
                    Settings._DEPRECATED_LEGACY_WARNED.add(marker)
                    logger.warning(
                        "config: both %s (deprecated) and %s set — using %s",
                        legacy_env,
                        new_env,
                        new_env,
                    )
        return self

    @model_validator(mode="after")
    def _validate_production(self) -> "Settings":
        if self.allow_bridge_as_user:
            logger.warning(
                "config: BEEVER_ALLOW_BRIDGE_AS_USER=true — emergency override "
                "active. The internal bridge key is accepted on user-facing "
                "routes, which reopens security finding H4. Turn this off as "
                "soon as the downstream integration is fixed."
            )

        # Boot-time disjoint-key assertion (D2): MCP keys must not overlap
        # with user keys or the bridge key. An overlap would allow an MCP
        # token to authenticate on user/bridge routes (or vice versa), which
        # breaks the principal-separation model.
        validate_keys_disjoint(
            api_keys=self.api_keys,
            bridge_api_key=self.bridge_api_key,
            mcp_api_keys=self.beever_mcp_api_keys,
        )

        problems: list[str] = []
        key = self.credential_master_key or ""
        hex_ok = len(key) == 64 and all(c in "0123456789abcdefABCDEF" for c in key)
        if not hex_ok:
            problems.append("CREDENTIAL_MASTER_KEY must be 64 hex chars (AES-256-GCM)")
        elif key.lower() == _INSECURE_PLACEHOLDER_KEY:
            # Issue #41 — reject the well-known dev placeholder. It IS valid
            # 64-char hex but it's published in `.env.example`, so any
            # encrypted credential under this key is effectively plaintext to
            # anyone who reads the repo. Production raises (existing logic at
            # L442-445); dev/test logs the same problem string at WARNING
            # severity — the INSECURE/PLAINTEXT keywords carry the loudness;
            # do NOT escalate to logger.critical/error to keep severity
            # consistent with the rest of `_validate_production`.
            problems.append(
                "CREDENTIAL_MASTER_KEY is the INSECURE well-known placeholder "
                "from .env.example — encryption is effectively PLAINTEXT. "
                'Regenerate with: python -c "import secrets; '
                'print(secrets.token_hex(32))"'
            )
        if self.neo4j_password in {"beever_atlas_dev", ""}:
            problems.append("NEO4J_AUTH password is a dev default or empty")
        if self.nebula_password == "nebula":
            problems.append("NEBULA_PASSWORD is the default 'nebula'")
        if not (self.bridge_api_key or "").strip():
            problems.append("BRIDGE_API_KEY is empty")
        if not (self.api_keys or "").strip():
            problems.append("BEEVER_API_KEYS is empty")
        if not (self.admin_token or "").strip():
            problems.append("BEEVER_ADMIN_TOKEN is empty")

        # Issue #89 — `LOADER_TOKEN_SECRET` empty in production WARNS but
        # does not fail. While `BEEVER_LOADER_RAW_KEY_FALLBACK=true` (the
        # migration default), raw-key matching still authenticates loader
        # requests, so an unset secret degrades signed-token issuance to
        # a no-op rather than breaking image rendering. The follow-up PR
        # that flips fallback to False will also harden this to fail.
        if self.beever_env == "production" and not (self.loader_token_secret or "").strip():
            logger.warning(
                "config: LOADER_TOKEN_SECRET is empty in production — "
                "signed-token issuance is disabled; loader endpoints will "
                "rely on `?access_token=` raw-key fallback. Provision a "
                "32+ byte secret to enable HMAC-signed loader tokens."
            )

        if self.beever_env == "production" and problems:
            raise ValueError("Production config invalid: " + "; ".join(problems))
        if problems:
            for p in problems:
                logger.warning("config: %s (dev-mode warning only)", p)
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached Settings instance. Raises ValidationError if invalid."""
    return Settings()
