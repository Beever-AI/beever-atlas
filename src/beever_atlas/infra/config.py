"""Application configuration loaded from environment variables."""

import logging
from functools import lru_cache
from typing import Literal

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

    # Pipeline config
    sync_batch_size: int = Field(default=10)
    sync_max_messages: int = Field(default=1000)
    quality_threshold: float = Field(default=0.5)
    entity_threshold: float = Field(default=0.6)
    max_facts_per_message: int = Field(default=2)
    sync_batch_timeout_seconds: int = Field(default=600)

    # Jina embeddings
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

    # Citation registry — enterprise citation architecture.
    # Enabled by default: QA tool outputs flow through SourceRegistry and
    # the LLM emits [src:xxx] tags rewritten to [N] at stream time. Set
    # CITATION_REGISTRY_ENABLED=false to revert to the legacy regex path.
    citation_registry_enabled: bool = Field(default=True)

    # Application
    beever_api_url: str = Field(default="http://localhost:8000")
    cors_origins: str = Field(default="http://localhost:5173,http://localhost:3000")

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
    batch_max_prompt_tokens: int = Field(default=6000)
    batch_time_window_seconds: int = Field(default=600)
    # Output-token ceiling for adaptive batching. Projects expected response
    # size per batch so we split BEFORE Gemini hits its max_output_tokens
    # ceiling (entity=65536, fact=131072). Default ~70% of entity ceiling
    # leaves headroom for schema overhead and estimator drift.
    # Set to 0 to disable output-aware batching (input-only, legacy behaviour).
    batch_max_output_tokens: int = Field(default=24000, alias="BATCH_MAX_OUTPUT_TOKENS")
    batch_max_messages: int = Field(default=30, ge=5, le=60, description="Hard cap on messages per batch. Derived from observed bench data — successful batches had ≤65 msgs; failure cluster started at 89. Prevents output truncation at the source.")
    llm_outage_breaker_threshold: int = Field(default=3, ge=1, le=10, description="After this many consecutive cross-batch Gemini 5xx, fail fast instead of burning per-batch retry budget.")
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

    # Multilingual memory & wiki/QA rendering (change: multilingual-native-memory).
    # When ON, ingestion detects BCP-47 source_lang per channel/message,
    # facts/entities are stored in source language, wiki/QA render in requested
    # target language. When OFF, everything hardcodes source_lang="en".
    # Default ON so multilang channels (zh-HK, ja, es, …) work without manual
    # env configuration. Set LANGUAGE_DETECTION_ENABLED=false to force English.
    language_detection_enabled: bool = Field(
        default=True, alias="LANGUAGE_DETECTION_ENABLED"
    )
    default_target_language: str = Field(
        default="en", alias="DEFAULT_TARGET_LANGUAGE"
    )
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
    allow_bridge_as_user: bool = Field(
        default=False, alias="BEEVER_ALLOW_BRIDGE_AS_USER"
    )

    # Single-tenant compatibility mode for the v1.0 OSS launch. When True,
    # any authenticated user principal is granted access to channels whose
    # owning PlatformConnection has ``owner_principal_id`` set to the shared
    # sentinel ``"legacy:shared"`` (or missing) — this preserves today's
    # behaviour for solo / operator deployments that never assigned per-user
    # ownership. Post-v1.0 this default flips to ``False`` so multi-tenant
    # operators must explicitly backfill ownership on legacy rows (see
    # ``stores.platform_store.PlatformStore.backfill_legacy_owners``).
    beever_single_tenant: bool = Field(
        default=True, alias="BEEVER_SINGLE_TENANT"
    )

    @property
    def neo4j_user(self) -> str:
        return self.neo4j_auth.split("/")[0]

    @property
    def neo4j_password(self) -> str:
        parts = self.neo4j_auth.split("/", 1)
        return parts[1] if len(parts) > 1 else ""

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
