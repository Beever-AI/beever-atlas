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

    def test_public_bot_base_empty_when_unset(self, monkeypatch):
        from beever_atlas.infra.config import Settings

        # Force-empty via the env source (which outranks the dotenv source) so a
        # developer's local .env PUBLIC_BOT_URL=<live tunnel> can't leak in. This
        # exercises the empty/unconfigured branch of public_bot_base.
        monkeypatch.setenv("PUBLIC_BOT_URL", "")
        assert Settings().public_bot_base == ""

    def test_public_bot_base_strips_trailing_slash(self, monkeypatch):
        from beever_atlas.infra.config import Settings

        monkeypatch.setenv("PUBLIC_BOT_URL", "https://abc.ngrok-free.app/")
        assert Settings().public_bot_base == "https://abc.ngrok-free.app"

    def test_public_bot_url_reads_alias_env(self, monkeypatch):
        from beever_atlas.infra.config import Settings

        monkeypatch.setenv("PUBLIC_BOT_URL", "https://host.example.com")
        assert Settings().public_bot_base == "https://host.example.com"


class TestConnectivityEndpoint:
    """`/api/config/connectivity` surfaces the exact inbound-webhook URLs to the
    Settings UI so users know what to paste into Slack / Teams."""

    async def test_returns_computed_webhook_urls_when_configured(self, monkeypatch):
        import beever_atlas.api.config as cfg
        from beever_atlas.infra.config import Settings

        monkeypatch.setenv("PUBLIC_BOT_URL", "https://abc.ngrok-free.app/")
        monkeypatch.setattr(cfg, "get_settings", Settings)
        result = await cfg.get_connectivity()
        assert result["configured"] is True
        assert result["public_bot_url"] == "https://abc.ngrok-free.app"
        assert result["webhooks"]["slack"] == "https://abc.ngrok-free.app/api/slack"
        assert result["webhooks"]["teams"] == "https://abc.ngrok-free.app/api/teams"

    async def test_empty_when_unconfigured(self, monkeypatch):
        import beever_atlas.api.config as cfg
        from beever_atlas.infra.config import Settings

        # Force-empty via the env source (outranks dotenv) so a developer's local
        # .env PUBLIC_BOT_URL can't leak in and falsely mark this configured.
        monkeypatch.setenv("PUBLIC_BOT_URL", "")
        monkeypatch.setattr(cfg, "get_settings", Settings)
        result = await cfg.get_connectivity()
        assert result["configured"] is False
        assert result["public_bot_url"] == ""
        assert result["webhooks"] == {"slack": "", "teams": ""}


class TestEmbeddingSettings:
    """PR-B: ``EmbeddingSettings`` defaults preserve legacy Jina behavior and
    legacy ``JINA_*`` env vars map into the new generic fields with a one-shot
    deprecation warning per field.
    """

    @staticmethod
    def _restore_propagation(monkeypatch):
        """``server/app.py`` flips ``beever_atlas`` logger propagate=False at
        import time. Once another test pulls that module into the session,
        caplog (which attaches to the root logger) can no longer see records
        emitted under ``beever_atlas.infra.config``. Force-enable
        propagation for the duration of this test so caplog can observe."""
        import logging

        bea_logger = logging.getLogger("beever_atlas")
        monkeypatch.setattr(bea_logger, "propagate", True)

    @staticmethod
    def _clear_env(monkeypatch):
        """Strip every embedding-related env var so each test starts clean
        regardless of the developer's local ``.env`` file."""
        for var in (
            "EMBEDDING_PROVIDER",
            "EMBEDDING_MODEL",
            "EMBEDDING_DIMENSIONS",
            "EMBEDDING_RPM",
            "EMBEDDING_API_BASE",
            "EMBEDDING_API_KEY",
            "EMBEDDING_TASK",
            "JINA_API_URL",
            "JINA_MODEL",
            "JINA_DIMENSIONS",
            "JINA_RPM",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_embedding_defaults_match_legacy_jina(self, monkeypatch):
        """Fresh install with no env overrides → defaults match legacy Jina v4."""
        from beever_atlas.infra.config import Settings

        self._clear_env(monkeypatch)
        s = Settings()
        assert s.embedding_provider == "jina_ai"
        assert s.embedding_model == "jina-embeddings-v4"
        assert s.embedding_dimensions == 2048
        assert s.embedding_rpm == 500
        assert s.embedding_task == "text-matching"
        assert s.embedding_api_base == ""
        assert s.embedding_dim_guard is True

    def test_legacy_jina_model_aliases_into_embedding_model(self, monkeypatch, caplog):
        """``JINA_MODEL`` alone populates ``embedding_model`` with one WARN.

        ``server/app.py`` sets ``propagate=False`` on the ``beever_atlas``
        logger when it's imported (transitively, by many other tests in this
        file's session), so we must attach caplog directly to the config
        module's logger or it won't see the records.
        """
        from beever_atlas.infra.config import Settings

        self._clear_env(monkeypatch)
        # Reset the per-process warn-tracker so the WARN actually fires.
        Settings._DEPRECATED_LEGACY_WARNED.clear()
        monkeypatch.setenv("JINA_MODEL", "jina-embeddings-v3")

        self._restore_propagation(monkeypatch)
        with caplog.at_level("WARNING", logger="beever_atlas.infra.config"):
            s = Settings()

        assert s.embedding_model == "jina-embeddings-v3"
        assert any("JINA_MODEL" in rec.message for rec in caplog.records), (
            f"Expected JINA_MODEL deprecation warn; got records: "
            f"{[rec.message for rec in caplog.records]}"
        )

    def test_legacy_jina_dimensions_aliases_into_embedding_dimensions(self, monkeypatch):
        from beever_atlas.infra.config import Settings

        self._clear_env(monkeypatch)
        Settings._DEPRECATED_LEGACY_WARNED.clear()
        monkeypatch.setenv("JINA_DIMENSIONS", "1024")
        s = Settings()
        assert s.embedding_dimensions == 1024

    def test_legacy_jina_rpm_aliases_into_embedding_rpm(self, monkeypatch):
        from beever_atlas.infra.config import Settings

        self._clear_env(monkeypatch)
        Settings._DEPRECATED_LEGACY_WARNED.clear()
        monkeypatch.setenv("JINA_RPM", "200")
        s = Settings()
        assert s.embedding_rpm == 200

    def test_new_env_wins_over_legacy_when_both_set(self, monkeypatch, caplog):
        """``EMBEDDING_MODEL`` wins when both env vars are present + warns."""
        from beever_atlas.infra.config import Settings

        self._clear_env(monkeypatch)
        Settings._DEPRECATED_LEGACY_WARNED.clear()
        monkeypatch.setenv("JINA_MODEL", "jina-embeddings-v3")
        monkeypatch.setenv("EMBEDDING_MODEL", "jina-embeddings-v4")

        self._restore_propagation(monkeypatch)
        with caplog.at_level("WARNING", logger="beever_atlas.infra.config"):
            s = Settings()

        assert s.embedding_model == "jina-embeddings-v4"
        assert any("JINA_MODEL" in rec.message and "using" in rec.message for rec in caplog.records)

    def test_deprecation_warning_fires_once_per_process(self, monkeypatch, caplog):
        """``_DEPRECATED_LEGACY_WARNED`` keeps the WARN to one fire per pair."""
        from beever_atlas.infra.config import Settings

        self._clear_env(monkeypatch)
        Settings._DEPRECATED_LEGACY_WARNED.clear()
        monkeypatch.setenv("JINA_MODEL", "jina-embeddings-v3")

        self._restore_propagation(monkeypatch)
        with caplog.at_level("WARNING", logger="beever_atlas.infra.config"):
            Settings()
            Settings()
            Settings()

        warns = [rec for rec in caplog.records if "JINA_MODEL" in rec.message]
        assert len(warns) == 1, f"Expected exactly 1 warn, got {len(warns)}"

    def test_explicit_kwarg_beats_legacy_env(self, monkeypatch):
        """Programmatic override (``Settings(embedding_model=...)``) wins over
        ``JINA_MODEL`` env — protects test setups + ``init_llm_provider`` from
        being clobbered."""
        from beever_atlas.infra.config import Settings

        self._clear_env(monkeypatch)
        Settings._DEPRECATED_LEGACY_WARNED.clear()
        monkeypatch.setenv("JINA_MODEL", "jina-embeddings-v3")

        s = Settings(embedding_model="text-embedding-3-large")
        assert s.embedding_model == "text-embedding-3-large"


# TestLiteLLMConfig removed — beever_atlas.infra.litellm_config replaced by beever_atlas.llm
