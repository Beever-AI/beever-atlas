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

    # Application
    beever_api_url: str = Field(default="http://localhost:8000")

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
