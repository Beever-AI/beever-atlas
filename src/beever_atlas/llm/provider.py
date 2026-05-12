"""Centralized LLM model selection with per-agent configuration."""

from __future__ import annotations

import logging
import time
from typing import Any

from beever_atlas.infra.config import Settings
from beever_atlas.llm.model_resolver import (
    AGENT_NAMES,
    DEFAULT_AGENT_MODELS,
    is_ollama_model,
    resolve_model_object,
)

logger = logging.getLogger(__name__)

_MODEL_ALIASES: dict[str, str] = {
    # Gemini 2.0 Flash Lite is retired for new users.
    "gemini-2.0-flash-lite": "gemini-2.5-flash-lite-preview-06-17",
    "gemini/gemini-2.0-flash-lite": "gemini-2.5-flash-lite-preview-06-17",
    # Keep older fast/quality defaults working across existing local .env files.
    "gemini-2.0-flash": "gemini-2.5-flash",
    "gemini/gemini-2.0-flash": "gemini-2.5-flash",
}

# Ollama fallback model when local service is unreachable
_OLLAMA_FALLBACK = "gemini-2.5-flash-lite"

# Ollama health-check cache TTL — see design D8. A fixed cached "down" used to
# stick forever; the TTL lets a restarted daemon recover within the window
# without an Atlas restart. ``dispatch_completion`` can also force-invalidate
# the cache via :meth:`LLMProvider.invalidate_ollama_cache` on a connect error.
_OLLAMA_TTL_SECONDS: float = 30.0

# Provider failover — out of OSS scope per the architecture doc.
# Hardcoded to disabled. Enterprise tier flips ``_FAILOVER_ENABLED`` to
# True and populates ``_FALLBACK_MAP`` with their multi-provider routing
# (e.g. ``"gemini-2.5-pro": "claude-3-5-sonnet"``). The map shape uses
# string keys so model resolution stays plumbing-free.
_FAILOVER_ENABLED: bool = False
_FALLBACK_MAP: dict[str, str] = {}


class LLMProvider:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._logged_deprecations: set[str] = set()
        # Per-agent model overrides loaded from MongoDB (empty until reload)
        self._agent_overrides: dict[str, str] = {}
        # Ollama health cache: (reachable, monotonic_timestamp). ``None`` = never
        # probed (or force-invalidated). See _OLLAMA_TTL_SECONDS for refresh window.
        self._ollama_cache: tuple[bool, float] | None = None

    def _resolve_alias(self, model: str, context: str) -> str:
        resolved = _MODEL_ALIASES.get(model, model)
        if resolved != model:
            logger.warning(
                "LLMProvider: remapping deprecated model %s -> %s for %s",
                model,
                resolved,
                context,
            )
        return resolved

    def get_model(self, tier: str = "fast") -> str:
        if tier == "fast":
            model = self._settings.llm_fast_model
        elif tier == "quality":
            model = self._settings.llm_quality_model
        else:
            raise ValueError(f"Unknown tier: {tier}")
        return self._resolve_alias(model, f"tier={tier}")

    def resolve_model(self, agent_name: str) -> Any:
        """Resolve the model for a specific agent.

        Priority: MongoDB override → default map → LLM_FAST_MODEL env var.
        Returns a string (Gemini) or LiteLlm instance (Ollama).

        Provider failover seam: when ``_FAILOVER_ENABLED=True`` AND the
        global CircuitBreaker is open AND the resolved model has a
        ``_FALLBACK_MAP`` entry, the call is re-mapped to the fallback
        model. Out of OSS scope by default — enterprise enablement flips
        the module constants in code (NO env var since failover requires
        multi-provider key management OSS doesn't ship).
        """
        # 1. Check MongoDB overrides
        model_str = self._agent_overrides.get(agent_name)
        # 2. Fall back to default map
        if not model_str:
            model_str = DEFAULT_AGENT_MODELS.get(agent_name)
        # 3. Fall back to env var
        if not model_str:
            model_str = self._settings.llm_fast_model

        model_str = self._resolve_alias(model_str, f"agent={agent_name}")

        # Provider failover seam.
        # Out of OSS scope per docs/architecture/oss-pipeline.md — multi-
        # provider failover requires a second-provider key (Claude /
        # OpenAI) which OSS doesn't ship. The seam is preserved as code
        # so an enterprise tier can flip ``_FAILOVER_ENABLED = True`` and
        # populate ``_FALLBACK_MAP`` with cross-provider entries. NO env
        # var — operators don't get a half-wired feature they can't
        # actually use.
        if _FAILOVER_ENABLED and _FALLBACK_MAP:
            try:
                from beever_atlas.services.circuit_breaker import get_circuit_breaker

                breaker = get_circuit_breaker()
                if breaker.is_open():
                    fallback = _FALLBACK_MAP.get(model_str)
                    if fallback:
                        logger.warning(
                            "LLMProvider: breaker open — failing over agent=%s "
                            "primary=%s fallback=%s",
                            agent_name,
                            model_str,
                            fallback,
                        )
                        model_str = fallback
            except Exception as exc:  # noqa: BLE001 — failover must not crash resolution
                logger.warning(
                    "LLMProvider: failover seam raised, using primary: %s",
                    exc,
                )

        # Ollama fallback: if model is Ollama but service is unreachable
        if is_ollama_model(model_str):
            if not self._check_ollama_cached():
                logger.warning(
                    "LLMProvider: Ollama unreachable for agent '%s', falling back to '%s'",
                    agent_name,
                    _OLLAMA_FALLBACK,
                )
                return _OLLAMA_FALLBACK

        return resolve_model_object(model_str)

    def get_model_string(self, agent_name: str) -> str:
        """Get the raw model string for an agent (without LiteLlm wrapping).

        Useful for API responses and display.
        """
        model_str = self._agent_overrides.get(agent_name)
        if not model_str:
            model_str = DEFAULT_AGENT_MODELS.get(agent_name)
        if not model_str:
            model_str = self._settings.llm_fast_model
        return self._resolve_alias(model_str, f"agent={agent_name}")

    def get_all_model_strings(self) -> dict[str, str]:
        """Get the effective model string for every known agent."""
        from beever_atlas.llm.model_resolver import AGENT_NAMES

        return {name: self.get_model_string(name) for name in AGENT_NAMES}

    def _check_ollama_cached(self) -> bool:
        """Check Ollama availability with a 30s TTL cache.

        Returns the cached value when fresh; re-probes ``/api/tags`` otherwise.
        Force-invalidation (e.g. on a dispatch-detected connect error) flips the
        cache to ``None`` so the next call re-probes immediately.
        """
        now = time.monotonic()
        if self._ollama_cache is not None:
            value, ts = self._ollama_cache
            if now - ts < _OLLAMA_TTL_SECONDS:
                return value

        if not self._settings.ollama_enabled:
            self._ollama_cache = (False, now)
            return False

        try:
            import httpx

            resp = httpx.get(
                f"{self._settings.ollama_api_base}/api/tags",
                timeout=3,
            )
            value = resp.status_code == 200
        except Exception:
            value = False

        self._ollama_cache = (value, now)
        return value

    def invalidate_ollama_cache(self) -> None:
        """Force a re-probe on the next ``_check_ollama_cached`` call.

        Called from ``services.llm_dispatch.dispatch_completion`` when a
        connect error is detected against ``OLLAMA_API_BASE``. Lets the cache
        recover from a transient outage faster than the 30s TTL.
        """
        self._ollama_cache = None

    def reload(self, overrides: dict[str, str] | None = None) -> None:
        """Refresh per-agent model overrides.

        Args:
            overrides: If provided, use directly. Otherwise caller should
                       pass data from MongoDB.
        """
        if overrides is not None:
            self._agent_overrides = dict(overrides)
        # Reset Ollama cache so next resolve re-checks
        self._ollama_cache = None
        logger.info(
            "LLMProvider: reloaded with %d agent overrides",
            len(self._agent_overrides),
        )

    async def reload_from_db(self) -> None:
        """Load per-agent model config from MongoDB."""
        try:
            from beever_atlas.stores import get_stores

            doc = await get_stores().mongodb.get_agent_model_config()
            overrides = doc.get("models", {}) if doc else {}
            self.reload(overrides)
        except Exception:
            logger.warning("LLMProvider: failed to load model config from MongoDB", exc_info=True)

    @property
    def fast(self) -> str:
        return self.get_model("fast")

    @property
    def quality(self) -> str:
        return self.get_model("quality")

    @property
    def embedding_model(self) -> str:
        """Effective embedding model identifier (provider/model not included).

        Reads the generic ``embedding_model`` field. The Settings layer
        bridges legacy ``JINA_MODEL`` into ``embedding_model`` at boot, so
        existing installs that only set the legacy env still get the right
        value here.
        """
        return self._settings.embedding_model

    @property
    def embedding_dimensions(self) -> int:
        """Configured embedding dimension (e.g. 2048 for Jina v4)."""
        return self._settings.embedding_dimensions

    @property
    def embedding_provider(self) -> str:
        """LiteLLM provider prefix (e.g. ``jina_ai``, ``openai``)."""
        return self._settings.embedding_provider


_provider: LLMProvider | None = None


def _validate_model_resolution(provider: LLMProvider) -> None:
    """Fail fast when ANY configured ADK model cannot be resolved.

    Runs at app startup so a typo in ``LLM_FAST_MODEL`` or a per-agent override
    pointing at an unresolvable LiteLLM prefix surfaces immediately instead of
    during a background sync job. Loops over every agent in ``AGENT_NAMES`` —
    the legacy ``fast``/``quality`` tier check is included (those still feed
    into agents whose default is the fast tier).
    """
    from google.adk.models.registry import LLMRegistry

    # Tier-level sanity check kept for callers that read ``provider.fast`` /
    # ``provider.quality`` directly outside the per-agent path.
    for tier, model_name in (
        ("fast", provider.fast),
        ("quality", provider.quality),
    ):
        try:
            LLMRegistry.resolve(model_name)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Invalid LLM config: tier=%s model=%s cannot be resolved by ADK. "
                "Ensure LiteLLM is installed (litellm>=1.75.5) and model names are valid."
                % (tier, model_name)
            ) from exc
        logger.info("LLMProvider: validated tier=%s model=%s", tier, model_name)

    # Per-agent resolution check — catches misconfigured DB overrides or any
    # agent whose default points at a model the registry can't resolve.
    for agent_name in AGENT_NAMES:
        model_name = provider.get_model_string(agent_name)
        try:
            LLMRegistry.resolve(model_name)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Invalid LLM config: agent={agent_name} model={model_name} "
                f"cannot be resolved by ADK. Ensure LiteLLM is installed "
                f"(litellm>=1.75.5) and the model prefix is supported."
            ) from exc
        logger.debug("LLMProvider: validated agent=%s model=%s", agent_name, model_name)


def init_llm_provider(settings: Settings) -> None:
    """Initialise both the chat-side LLMProvider and the embedding runtime.

    Order matters:
      1. Configure LiteLLM globals + bridge ``JINA_API_KEY`` →
         ``JINA_AI_API_KEY`` so any subsequent embedding call has the right
         env var visible. Must run before model resolution because chat
         models can also flow through LiteLLM (Ollama path).
      2. Resolve chat-tier models so a misconfigured ``LLM_FAST_MODEL``
         fails fast at boot rather than mid-sync.

    The embedding dimension guard runs separately (``run_embedding_dim_guard``
    below) because it needs ``StoreClients`` initialised first — the guard is
    invoked from the FastAPI startup hook in ``server/app.py`` after
    ``init_stores``.
    """
    from beever_atlas.llm.embeddings import initialize_embedding_runtime

    global _provider
    provider = LLMProvider(settings)
    initialize_embedding_runtime(settings)
    _validate_model_resolution(provider)
    _provider = provider


async def run_embedding_dim_guard(settings: Settings) -> None:
    """Run the boot-time embedding probe + dimension-mismatch guard.

    Separated from ``init_llm_provider`` so the caller controls ordering
    against ``StoreClients.startup``. Raises
    :class:`EmbeddingDimensionMismatch` on a fatal mismatch unless
    ``settings.embedding_dim_guard`` is False (in which case the failure
    downgrades to a loud WARN).
    """
    from beever_atlas.llm.embedding_health import probe_and_validate
    from beever_atlas.stores import get_stores

    await probe_and_validate(settings, get_stores())


def get_llm_provider() -> LLMProvider:
    if _provider is None:
        raise RuntimeError(
            "LLM provider not initialized. Call init_llm_provider() during app startup."
        )
    return _provider
