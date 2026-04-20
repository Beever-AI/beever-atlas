"""Tests for config system and LiteLLM model routing."""


class TestSettings:
    def test_config_loads_defaults(self):
        from beever_atlas.infra.config import Settings

        settings = Settings()
        assert settings.weaviate_url == "http://localhost:8080"
        assert settings.neo4j_uri == "bolt://localhost:7687"
        assert settings.mongodb_uri == "mongodb://localhost:27017/beever_atlas"
        assert settings.redis_url == "redis://localhost:6379"

    def test_config_loads_from_env(self, monkeypatch):
        from beever_atlas.infra.config import Settings

        monkeypatch.setenv("WEAVIATE_URL", "http://weaviate:9999")
        monkeypatch.setenv("NEO4J_URI", "bolt://neo4j:7777")
        settings = Settings()
        assert settings.weaviate_url == "http://weaviate:9999"
        assert settings.neo4j_uri == "bolt://neo4j:7777"

    def test_neo4j_user_password_parsing(self):
        from beever_atlas.infra.config import Settings

        settings = Settings(neo4j_auth="admin/secretpass")
        assert settings.neo4j_user == "admin"
        assert settings.neo4j_password == "secretpass"

    def test_all_api_key_fields_exist(self):
        from beever_atlas.infra.config import Settings

        settings = Settings()
        assert hasattr(settings, "google_api_key")
        assert hasattr(settings, "jina_api_key")
        assert hasattr(settings, "tavily_api_key")


# TestLiteLLMConfig removed — beever_atlas.infra.litellm_config replaced by beever_atlas.llm
