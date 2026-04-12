"""Application configuration loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Beever Atlas configuration — all values from env vars."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

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

    # Temporal fact lifecycle
    contradiction_confidence_threshold: float = Field(default=0.8)
    contradiction_flag_threshold: float = Field(default=0.5)

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
    fact_max_retries: int = Field(default=3)
    stale_job_threshold_hours: float = Field(default=1.0)

    # Ollama (local models)
    ollama_enabled: bool = Field(default=False)
    ollama_api_base: str = Field(default="http://localhost:11434")

    # QA agent configuration
    qa_confidence_threshold: float = Field(default=0.4, alias="QA_CONFIDENCE_THRESHOLD")
    external_mcp_servers: str = Field(default="", alias="EXTERNAL_MCP_SERVERS")

    # Credential encryption
    credential_master_key: str = Field(default="")

    @property
    def neo4j_user(self) -> str:
        return self.neo4j_auth.split("/")[0]

    @property
    def neo4j_password(self) -> str:
        parts = self.neo4j_auth.split("/", 1)
        return parts[1] if len(parts) > 1 else ""


@lru_cache
def get_settings() -> Settings:
    """Return cached Settings instance. Raises ValidationError if invalid."""
    return Settings()
