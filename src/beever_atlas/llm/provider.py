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

# Provider failover — wired through per-Assignment ``fallback_endpoint_id``
# in the new Endpoint+Assignment data model (PR-B/H). The dead
# ``_FAILOVER_ENABLED`` / ``_FALLBACK_MAP`` module constants from the
# pre-cutover OSS code path have been removed; their job is now done by
# :meth:`LLMProvider.resolve_for_call` consulting the Assignment + the
# global circuit breaker.


class CircuitBreakerOpenForBothPrimaryAndFallback(RuntimeError):
    """Raised when the circuit breaker is open AND the Assignment's fallback
    Endpoint is also in a failure state. Surfaces as a fast-fail to the caller
    instead of a slow timeout. See design D14.
    """

    def __init__(self, consumer: str, primary_id: str, fallback_id: str | None) -> None:
        self.consumer = consumer
        self.primary_id = primary_id
        self.fallback_id = fallback_id
        super().__init__(
            f"Circuit open for both primary ({primary_id}) and fallback ({fallback_id}) "
            f"for consumer {consumer!r}"
        )


class LLMProvider:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._logged_deprecations: set[str] = set()
        # Per-agent model overrides loaded from MongoDB (empty until reload)
        self._agent_overrides: dict[str, str] = {}
        # PR-ν.1: per-agent endpoint_id (populated from llm_assignments). Used by
        # ``resolve_model`` to fetch the endpoint's credential + base_url and
        # forward them to LiteLLM, so agent calls against custom endpoints
        # (Z.AI, OpenRouter, etc.) actually authenticate.
        self._agent_endpoint_overrides: dict[str, str] = {}
        # Cached endpoint metadata keyed by id, populated alongside
        # ``_agent_endpoint_overrides`` so ``resolve_model`` doesn't have to
        # round-trip MongoDB on every agent call.
        self._endpoint_meta: dict[str, dict[str, Any]] = {}
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

        Priority: MongoDB Assignment → legacy MongoDB override → default map → LLM_FAST_MODEL.
        Returns a string (Gemini bare strings, flag-off path) or a
        ``LiteLlm`` instance (every other path, including Gemini when
        ``LLM_USE_LITELLM_FOR_GEMINI=True``).

        PR-ν.1: when the agent has an Assignment, also looks up the
        Assignment's endpoint to fetch its base_url + runtime credential
        and forwards both to the ``LiteLlm`` wrapper. Without this, an
        agent on a custom endpoint (Z.AI, OpenRouter, custom proxy) ends
        up calling LiteLLM with no api_key and 401s with
        ``AuthenticationError`` while LiteLLM falls back to
        ``OPENAI_API_KEY`` from env.
        """
        # 1. Check MongoDB overrides (Assignment-derived + legacy)
        model_str = self._agent_overrides.get(agent_name)
        # 2. Fall back to default map
        if not model_str:
            model_str = DEFAULT_AGENT_MODELS.get(agent_name)
        # 3. Fall back to env var
        if not model_str:
            model_str = self._settings.llm_fast_model

        model_str = self._resolve_alias(model_str, f"agent={agent_name}")

        # Ollama fallback: if model is Ollama but service is unreachable
        if is_ollama_model(model_str):
            if not self._check_ollama_cached():
                logger.warning(
                    "LLMProvider: Ollama unreachable for agent '%s', falling back to '%s'",
                    agent_name,
                    _OLLAMA_FALLBACK,
                )
                return _OLLAMA_FALLBACK

        # Look up the Assignment's endpoint credential + base_url. Optional —
        # for agents driven by the legacy ``agent_model_config`` map (no
        # ``_agent_endpoint_overrides`` row) we pass through with no extras,
        # preserving the historical behaviour where LiteLLM picks up
        # provider-default env vars.
        api_key: str | None = None
        api_base: str | None = None
        ep_id = self._agent_endpoint_overrides.get(agent_name)
        if ep_id:
            ep_meta = self._endpoint_meta.get(ep_id)
            if ep_meta and isinstance(ep_meta.get("base_url"), str):
                api_base = ep_meta["base_url"]
            try:
                from beever_atlas.llm.agent_credentials import get_runtime_credential

                cred = get_runtime_credential(ep_id)
                if isinstance(cred, str):
                    api_key = cred
            except Exception as exc:  # noqa: BLE001
                # Credential cache miss is non-fatal — LiteLLM will surface
                # the resulting 401 with a clear message via the recent-calls
                # debug surface; better than crashing the agent build.
                #
                # NEVER pass ``exc_info=True`` here — ``api_key`` and ``cred``
                # live in this scope and a structured log handler (JSON,
                # Sentry, etc.) would serialise the locals and leak the
                # credential. Log the exception class + agent + ep only.
                # WARNING (not DEBUG) — operator needs a visible signal when
                # credential resolution fails for an explicit Assignment;
                # the subsequent upstream 401 then makes sense.
                logger.warning(
                    "LLMProvider.resolve_model: credential lookup for agent=%s ep=%s failed (%s)",
                    agent_name,
                    ep_id,
                    type(exc).__name__,
                )

        # Defer the (provider, model, drop_base_url) decision to the
        # canonical router in ``services/llm_dispatch.route_for_endpoint`` —
        # the same rules ``dispatch_completion`` uses. Without this, the
        # ADK agent path would diverge from the dispatch path on every
        # OpenAI-compat shim (Gemini ``/openai/``, Ollama ``/v1``, vLLM,
        # LM Studio, OpenRouter, …) and produce broken URLs like
        # ``http://localhost:11434/v1/api/generate`` (preset=ollama with
        # native handler ``ollama/`` + shim base) or
        # ``…/v1beta/openai//models/gemini-2.5-flash:generateContent``
        # (preset=google_ai with native ``gemini/`` + shim base).
        preset = ""
        if ep_id:
            preset = self._endpoint_meta.get(ep_id, {}).get("preset", "")
            try:
                from beever_atlas.services.llm_dispatch import route_for_endpoint

                bare_model = model_str.split("/", 1)[1] if "/" in model_str else model_str
                routed_provider, routed_model, drop_base_url = route_for_endpoint(
                    preset=preset,
                    base_url=api_base or "",
                    model=bare_model,
                )
                # ``route_for_endpoint`` returns ``model`` either bare
                # (when provider==openai shim path) or already prefixed
                # (when native LiteLLM provider). Make sure the final
                # model string carries the provider prefix LiteLLM needs.
                if "/" not in routed_model:
                    model_str = f"{routed_provider}/{routed_model}"
                else:
                    model_str = routed_model
                if drop_base_url:
                    api_base = None
            except Exception as exc:  # noqa: BLE001
                # ``route_for_endpoint`` raises for embedding-only presets
                # (jina_ai, voyage, cohere) — a chat consumer pointed at one
                # of those is operator error; fall through with the
                # un-routed model string so LiteLLM surfaces a clear error.
                # No ``exc_info=True`` — ``api_key`` is in this scope and a
                # structured log handler would leak it via traceback locals.
                logger.warning(
                    "LLMProvider.resolve_model: route_for_endpoint failed "
                    "for agent=%s preset=%r (%s) — using preset default",
                    agent_name,
                    preset,
                    type(exc).__name__,
                )

        # Credential fallback for two cases the per-Endpoint cache can't
        # cover on its own:
        #
        # (a) Pre-migration installs where the operator's API key still
        #     lives in env vars (GOOGLE_API_KEY, OPENAI_API_KEY, …) and the
        #     Endpoint document was synthesised without an encrypted_key.
        #     Without this fallback, switching qa_agent to Gemini surfaces
        #     a 401 even though the env key is right there.
        # (b) ``auth_type=none`` presets (Ollama, local vLLM/LM Studio)
        #     routed through the OpenAI-compat shim — LiteLLM's openai SDK
        #     400s client-side without an api_key set, even when the
        #     upstream ignores the value. ``dispatch_assignment`` uses the
        #     same placeholder; mirror that here so the ADK agent path
        #     doesn't 400 differently from the dispatch path.
        #
        # SECURITY: ``placeholder-no-auth`` MUST NOT leak to a real auth'd
        # upstream (it would send an ``Authorization: Bearer placeholder-
        # no-auth`` header and burn rate-limit on a meaningless probe).
        # Gate the fallback to known no-auth presets only. For any other
        # preset that ends up missing a credential, return None and let
        # LiteLLM raise a clear ``api_key client option must be set``
        # error — operator-actionable, no accidental real-upstream call.
        _NO_AUTH_PRESETS = frozenset({"ollama", "ollama_chat", "vllm", "lmstudio"})
        if api_key is None and model_str.startswith("openai/"):
            import os

            if preset == "google_ai":
                api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
            if api_key is None and preset in _NO_AUTH_PRESETS:
                # ``placeholder-no-auth`` matches dispatch_assignment's
                # behaviour for ``auth_type=none`` OpenAI-compat endpoints
                # (Ollama, local vLLM/LM Studio). The upstream ignores it;
                # the openai SDK just needs *something* in the field to
                # skip its client-side check.
                api_key = "placeholder-no-auth"

        return resolve_model_object(model_str, api_key=api_key, api_base=api_base)

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

    async def resolve_for_call(self, consumer: str, stores: Any = None) -> Any:
        """Return a :class:`ResolvedAssignment` for ``consumer``, applying
        circuit-breaker-driven failover when configured.

        Resolution order:
          1. Look up the Assignment for ``consumer`` in ``llm_assignments``.
          2. Load the primary Endpoint by ``endpoint_id``.
          3. When the global circuit breaker is open AND the Assignment has
             a ``fallback_endpoint_id``, load the fallback Endpoint and
             return a ``ResolvedAssignment`` pointing at it instead.
          4. When the breaker is open AND no fallback is configured, raise
             :class:`CircuitBreakerOpenForBothPrimaryAndFallback`.

        Returns ``None`` when no Assignment exists for ``consumer`` (caller
        falls back to legacy ``resolve_model`` or env defaults).

        ``stores`` is the StoreClients instance; passed in to avoid a circular
        import. When ``None``, fetches the global instance.
        """
        from beever_atlas.llm.agent_credentials import get_runtime_credential
        from beever_atlas.llm.assignments import AssignmentStore, ResolvedAssignment
        from beever_atlas.llm.endpoints import EndpointStore
        from beever_atlas.services.circuit_breaker import get_breaker_for_endpoint
        from beever_atlas.services.llm_dispatch import route_for_endpoint

        if stores is None:
            from beever_atlas.stores import get_stores

            stores = get_stores()

        assignment = await AssignmentStore(stores.mongodb).get(consumer)
        if assignment is None:
            return None

        endpoint_store = EndpointStore(stores.mongodb)
        primary = await endpoint_store.get(assignment.endpoint_id)
        if primary is None:
            return None

        # Failover decision — per-Endpoint breaker drives the per-Assignment
        # fallback. The primary Endpoint's own breaker being open means "this
        # Endpoint is in a failure state"; we then route to the fallback
        # Endpoint unless ITS breaker is also open (or there's no fallback),
        # in which case we fast-fail rather than wait out a timeout.
        target_endpoint = primary
        try:
            if get_breaker_for_endpoint(primary.id).is_open():
                fallback = (
                    await endpoint_store.get(assignment.fallback_endpoint_id)
                    if assignment.fallback_endpoint_id
                    else None
                )
                if fallback is not None and not get_breaker_for_endpoint(fallback.id).is_open():
                    logger.warning(
                        "LLMProvider: breaker open — failover consumer=%s "
                        "primary=%s -> fallback=%s",
                        consumer,
                        primary.id,
                        fallback.id,
                    )
                    target_endpoint = fallback
                else:
                    # No fallback configured, fallback missing, OR fallback's
                    # breaker is also open — fast-fail.
                    raise CircuitBreakerOpenForBothPrimaryAndFallback(
                        consumer, primary.id, assignment.fallback_endpoint_id
                    )
        except CircuitBreakerOpenForBothPrimaryAndFallback:
            raise
        except Exception as exc:  # noqa: BLE001 — breaker lookup must never crash resolve
            # SECURITY: ``resolve_for_call`` decrypts credentials a few lines
            # below (``credential = get_runtime_credential(...)``); today
            # this except runs BEFORE that assignment so no plaintext is
            # on the stack, but defense-in-depth — never exc_info=True in
            # a function that holds api_keys later in the same scope.
            logger.warning(
                "LLMProvider: circuit breaker check failed (%s: %s)",
                type(exc).__name__,
                exc,
            )

        # Build the ResolvedAssignment carrying every dispatch-time param.
        credential = get_runtime_credential(target_endpoint.id)
        api_key: str | None = credential if isinstance(credential, str) else None
        aws_creds: dict[str, str] | None = (
            credential if isinstance(credential, dict) and "access_key_id" in credential else None
        )
        vertex_creds: dict[str, str] | None = (
            credential if isinstance(credential, dict) and "sa_json" in credential else None
        )

        # Pick the LiteLLM provider + model id from the Endpoint's
        # ``(preset, base_url, model)`` tuple. ``route_for_endpoint`` is the
        # single source of truth shared with the Test Connection probe so the
        # two paths can't disagree (operator sees Test pass / dispatch 404).
        # Embedding-only presets raise ValueError there; fall back to the
        # legacy prefix logic for the ``embedding`` consumer so existing
        # callers keep working.
        try:
            provider_prefix, litellm_model, drop_base_url = route_for_endpoint(
                target_endpoint.preset,
                target_endpoint.base_url or None,
                assignment.model,
            )
        except ValueError:
            # Embedding-only preset — chat dispatch isn't valid for this
            # ResolvedAssignment, but we still need to construct the record
            # (the embedding consumer reads it). Use the legacy shape.
            from beever_atlas.llm.endpoints import preset_to_provider as _p2p

            provider_prefix = _p2p(target_endpoint.preset)
            litellm_model = (
                assignment.model
                if "/" in assignment.model
                else f"{provider_prefix}/{assignment.model}"
            )
            drop_base_url = False

        resolved_base_url = None if drop_base_url else (target_endpoint.base_url or None)

        return ResolvedAssignment(
            consumer=consumer,
            endpoint_id=target_endpoint.id,
            provider=provider_prefix,
            litellm_model=litellm_model,
            base_url=resolved_base_url,
            api_key=api_key,
            aws_credentials=aws_creds,
            vertex_credentials=vertex_creds,
            extra_headers={**target_endpoint.headers, **assignment.extra_headers},
            temperature=assignment.temperature,
            max_tokens=assignment.max_tokens,
            response_format=assignment.response_format,
            dimensions=assignment.dimensions,
            task=assignment.task,
        )

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
        """Load per-agent model config from MongoDB.

        PR-ν: merges TWO sources, with the NEW one taking precedence:

          1. ``llm_assignments`` collection (new Endpoint+Assignment data
             model — this is what the Settings UI writes to). Each row maps
             ``consumer`` → ``endpoint_id`` + ``model``; we resolve the
             endpoint's preset+base_url into the ``<provider>/<model>``
             form ``resolve_model`` expects.
          2. ``agent_model_config`` collection (legacy doc, still set by
             a few internal flows). Used only for consumers without an
             Assignment row.

        Previously this only read source #2 — which is why operators saw
        Settings → Agent models save successfully (#1) but agent code
        kept dispatching the OLD model (it only read #2). The bug is
        invisible until the operator actually inspects the dispatched
        model string, e.g. via the new "Last call" indicator.
        """
        try:
            from beever_atlas.stores import get_stores

            stores = get_stores()
            overrides: dict[str, str] = {}

            # Source 2 first — legacy doc — so source 1 (Assignments)
            # overwrites on collisions.
            try:
                doc = await stores.mongodb.get_agent_model_config()
                if doc:
                    legacy = doc.get("models", {}) or {}
                    if isinstance(legacy, dict):
                        overrides.update(legacy)
            except Exception as exc:  # noqa: BLE001
                # Legacy doc has no credentials, but keep the no-exc_info
                # pattern consistent across this whole function so future
                # refactors don't accidentally widen the leak surface.
                logger.debug(
                    "LLMProvider.reload_from_db: legacy agent_model_config read failed (%s: %s)",
                    type(exc).__name__,
                    exc,
                )

            # Source 1: llm_assignments × endpoints. Build the full
            # ``<provider>/<model>`` string the resolver needs.
            try:
                assignments = await stores.mongodb.db["llm_assignments"].find({}).to_list(None)
                # SECURITY: ``endpoints`` docs include ``encrypted_key`` blobs
                # (ciphertext + IV + tag). Keep them in this scope only as
                # long as needed; the wrapper try/except below uses class +
                # message logging (not exc_info=True) so the blobs do not
                # bleed into log aggregators on failure.
                endpoints = await stores.mongodb.db["endpoints"].find({}).to_list(None)
                ep_by_id = {e.get("id"): e for e in endpoints if e.get("id")}
                from beever_atlas.llm.endpoints import preset_to_provider

                # Reset endpoint maps so a deleted Assignment stops being
                # honoured after the next reload.
                self._agent_endpoint_overrides.clear()
                self._endpoint_meta.clear()
                for ep in endpoints:
                    if ep.get("id"):
                        self._endpoint_meta[ep["id"]] = {
                            "preset": ep.get("preset"),
                            "base_url": ep.get("base_url"),
                        }
                for a in assignments:
                    consumer = a.get("consumer")
                    model = a.get("model")
                    ep_id = a.get("endpoint_id")
                    if not (consumer and model and ep_id):
                        continue
                    if consumer == "embedding":
                        # Embedding has its own runtime + dispatch path.
                        continue
                    ep = ep_by_id.get(ep_id)
                    if ep is None:
                        continue
                    provider = preset_to_provider(ep.get("preset", "openai"))
                    bare = model.split("/", 1)[-1] if "/" in model else model
                    overrides[consumer] = f"{provider}/{bare}"
                    self._agent_endpoint_overrides[consumer] = ep_id
            except Exception as exc:  # noqa: BLE001
                # SECURITY: this scope holds raw ``endpoints`` docs from
                # MongoDB which contain ``encrypted_key`` envelopes. Log
                # class + message only — never exc_info=True.
                logger.warning(
                    "LLMProvider.reload_from_db: llm_assignments hydration failed "
                    "non-fatal — legacy overrides still applied (%s: %s)",
                    type(exc).__name__,
                    exc,
                )

            self.reload(overrides)
            logger.info(
                "LLMProvider: hydrated %d agent model overrides from "
                "llm_assignments + agent_model_config",
                len(overrides),
            )
        except Exception as exc:  # noqa: BLE001
            # SECURITY: outer wrapper of reload_from_db — same scope holds
            # raw endpoint docs with encrypted_key blobs. Class + message
            # only.
            logger.warning(
                "LLMProvider: failed to load model config from MongoDB (%s: %s)",
                type(exc).__name__,
                exc,
            )

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
    """Fail fast when any agent's *seed-default* model cannot be resolved.

    SCOPE — this runs from :func:`init_llm_provider` BEFORE ``reload_from_db``
    in the lifespan, so ``provider._agent_overrides`` is empty here.
    :meth:`LLMProvider.get_model_string` therefore returns:
      * the agent's entry in :data:`DEFAULT_AGENT_MODELS` (e.g.
        ``gemini-2.5-flash`` for the Gemini-default agents), or
      * the ``LLM_FAST_MODEL`` env default for agents without a static map
        entry — the wizard's canonical ``gemini/gemini-2.5-flash``.

    DB-stored Assignment overrides are validated LAZILY by ``resolve_model``
    on first dispatch — moving validation post-reload would turn a typo in
    an Assignment row into a fatal boot loop, which is worse than the
    current behaviour where one bad agent surfaces a clear error on its
    first call while the rest of the system keeps working.

    Mirrors :func:`resolve_model_object` so validator and runtime can't
    drift on the resolution rules themselves:
      * String result → confirm ADK's ``LLMRegistry`` can resolve it (native
        Gemini path under the flag-off rollback).
      * ``LiteLlm`` instance → trust it; LiteLLM validates the provider
        lazily on first call. Boot-time can guarantee "wrapper constructed",
        not "upstream reachable".

    The legacy tier-level loop (``provider.fast`` / ``provider.quality``) was
    removed: no runtime code reads those properties — every consumer flows
    through ``get_model_string(agent_name)`` / ``resolve_model(agent_name)``.

    Caveat — Ollama-default agents (``document_digester``, ``image_describer``)
    are validated against their ``ollama_chat/*`` model string. The runtime
    fallback to :data:`_OLLAMA_FALLBACK` when Ollama is unreachable
    (``resolve_model`` lines 128-135) is NOT separately validated here; the
    fallback ``gemini-2.5-flash-lite`` is implicitly covered by every other
    Gemini-default agent.
    """
    from google.adk.models.registry import LLMRegistry

    for agent_name in AGENT_NAMES:
        model_name = provider.get_model_string(agent_name)
        try:
            resolved = resolve_model_object(model_name)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Invalid LLM config: agent={agent_name} model={model_name} "
                f"failed to construct via resolve_model_object. "
                f"Ensure LiteLLM is installed (litellm>=1.75.5) and the model "
                f"prefix is supported."
            ) from exc
        if isinstance(resolved, str):
            try:
                LLMRegistry.resolve(resolved)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Invalid LLM config: agent={agent_name} model={model_name} "
                    f"cannot be resolved by ADK's native registry."
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
