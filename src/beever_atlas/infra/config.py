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
    anthropic_api_key: str = Field(default="")

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
    sync_batch_timeout_seconds: int = Field(default=180)

    # Jina embeddings
    jina_api_url: str = Field(default="https://api.jina.ai/v1/embeddings")
    jina_model: str = Field(default="jina-embeddings-v4")
    jina_dimensions: int = Field(default=2048)

    # Reconciler
    reconciler_interval_minutes: int = Field(default=15)

    # Application
    beever_api_url: str = Field(default="http://localhost:8000")
    cors_origins: str = Field(default="http://localhost:5173,http://localhost:3000")

    # Bridge (bot service)
    bridge_url: str = Field(default="http://localhost:3001")
    bridge_api_key: str = Field(default="")

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
