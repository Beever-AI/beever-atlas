"""Provider-agnostic embedding shim built on ``litellm.aembedding``.

Single entry point for every embedding call in the codebase — pipeline,
entity registry, query-time hybrid search, backfill scripts. Replaces the
six raw ``httpx.post(jina_api_url, ...)`` blocks that used to live across
the project.

Behaviour intentionally preserved from the original ``embedder.py``:
  * 100-text chunking (Jina's effective batch ceiling; safe everywhere).
  * Retry on ``{429, 500, 502, 503, 504}`` plus transient httpx exceptions.
  * Exponential backoff ``(2 ** attempt) * uniform(0.8, 1.2)``, max 3 retries.
  * Each chunk wrapped in ``EMBEDDING_LIMITER`` so concurrent batches don't
    exceed ``settings.embedding_rpm``.
  * Structured ``cat=embed`` log lines via ``infra.logging.embed_log``.

Provider routing is delegated to LiteLLM. Unsupported provider-specific
kwargs (Jina's ``task=``, Cohere's ``input_type=``) are silently dropped by
``litellm.drop_params = True`` set during ``initialize_embedding_runtime``.

The ``JINA_API_KEY`` env var is bridged to ``JINA_AI_API_KEY`` at startup so
existing installations keep working when the default provider is Jina.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Any

import httpx

from beever_atlas.infra.config import Settings, get_settings
from beever_atlas.infra.logging import embed_log
from beever_atlas.infra.rate_limit import EMBEDDING_LIMITER
from beever_atlas.llm.known_embedding_models import (
    SUPPORTED_PROVIDERS,
    model_accepts_task,
)

logger = logging.getLogger(__name__)

# Public knobs — defaults preserve the legacy embedder.py behaviour.
_BATCH_SIZE = 100
_MAX_RETRIES = 3
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_DEFAULT_TIMEOUT_SECONDS = 60.0


class EmbeddingError(RuntimeError):
    """Base class for embedding shim errors."""


class EmbeddingProviderError(EmbeddingError):
    """Raised when the configured provider prefix is not recognised."""


class EmbeddingResponseError(EmbeddingError):
    """Raised when the provider response cannot be aligned with the input."""


# Process-wide flag so ``initialize_embedding_runtime`` is idempotent.
_runtime_initialised: bool = False

# Runtime API key resolved from MongoDB at boot (PR-E). Sits between the
# explicit ``EMBEDDING_API_KEY`` env override (highest priority) and the
# provider-default env vars LiteLLM falls back to (lowest). ``None`` means
# "no DB-stored key" — the shim falls through to env defaults.
_runtime_db_api_key: str | None = None


def set_runtime_db_api_key(value: str | None) -> None:
    """Set the DB-stored API key resolved at boot (or via UI write).

    Idempotent. ``None`` clears. Plaintext lives only inside this module's
    closure plus whatever LiteLLM sends to the provider.
    """
    global _runtime_db_api_key
    _runtime_db_api_key = value or None


def initialize_embedding_runtime(settings: Settings | None = None) -> None:
    """Configure the LiteLLM client + bridge legacy env vars.

    Safe to call multiple times — idempotent. Should run once during app
    startup, before any ``embed_texts`` call.

    Side effects:
      * ``litellm.drop_params = True`` — provider-specific kwargs (Jina's
        ``task``, Cohere's ``input_type``) flow through gracefully and are
        stripped for providers that reject them. Kept module-global because
        the alternative (passing ``drop_params=True`` per-call) silently
        breaks if any caller forgets it.
      * ``os.environ["JINA_AI_API_KEY"]`` set from ``JINA_API_KEY`` when the
        target is unset. ``setdefault`` semantics — never overrides an
        operator-supplied value.
    """
    global _runtime_initialised
    if _runtime_initialised:
        return

    cfg = settings or get_settings()

    # Bridge JINA_API_KEY → JINA_AI_API_KEY so existing installs keep working
    # without an .env edit. ``setdefault`` so a deliberately-set
    # JINA_AI_API_KEY wins.
    if cfg.jina_api_key and "JINA_AI_API_KEY" not in os.environ:
        os.environ["JINA_AI_API_KEY"] = cfg.jina_api_key

    # Configure LiteLLM. Imported lazily so ``init_llm_provider`` callers
    # don't pay the import cost on cold paths that never embed (e.g. wiki-only
    # admin tools). ADK already imports litellm so this is essentially free in
    # the hot path.
    import litellm  # type: ignore[import-untyped]

    litellm.drop_params = True
    # Suppress LiteLLM's own embedding telemetry / debug noise; we have our
    # own ``cat=embed`` channel and don't want litellm.suppress_debug_info
    # echoes dirtying logs.
    litellm.suppress_debug_info = True

    _runtime_initialised = True
    logger.debug(
        "embeddings: runtime initialised (provider=%s model=%s dim=%s drop_params=on)",
        cfg.embedding_provider,
        cfg.embedding_model,
        cfg.embedding_dimensions,
    )


def _resolve_api_key(cfg: Settings) -> str | None:
    """Return the API key to use for this call, or None to fall back to env.

    Precedence (matches design D4):
      1. ``settings.embedding_api_key`` — env override (`EMBEDDING_API_KEY`).
      2. ``_runtime_db_api_key`` — DB-stored, decrypted at boot (PR-E).
      3. ``None`` — let LiteLLM read the provider-default env var
         (``JINA_AI_API_KEY``, ``OPENAI_API_KEY``, …).
    """
    if cfg.embedding_api_key:
        return cfg.embedding_api_key
    if _runtime_db_api_key:
        return _runtime_db_api_key
    return None


def _resolve_model_string(cfg: Settings) -> str:
    """Build the LiteLLM-flavoured ``provider/model`` string.

    Validates against the small set of supported provider prefixes so a typo
    in env (``jian_ai/...``) surfaces as a fail-fast error rather than a
    confusing 404 from a random LiteLLM router.
    """
    provider = cfg.embedding_provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise EmbeddingProviderError(
            f"Unsupported embedding provider {cfg.embedding_provider!r}. "
            f"Supported: {', '.join(SUPPORTED_PROVIDERS)}. "
            "Set EMBEDDING_PROVIDER to one of those, or add a new entry "
            "to known_embedding_models.py."
        )
    return f"{provider}/{cfg.embedding_model}"


def _build_extra_kwargs(cfg: Settings, *, task: str) -> dict[str, Any]:
    """Per-call kwargs forwarded to ``litellm.aembedding``.

    Conservative: only forwards ``dimensions``, ``api_base``, and ``task``
    when applicable. ``litellm.drop_params=True`` strips ``task`` for
    providers that reject it, but we still gate explicitly so the request
    body shape is predictable for tests + telemetry.
    """
    kwargs: dict[str, Any] = {}
    if cfg.embedding_dimensions:
        kwargs["dimensions"] = cfg.embedding_dimensions
    if cfg.embedding_api_base:
        kwargs["api_base"] = cfg.embedding_api_base
    api_key = _resolve_api_key(cfg)
    if api_key:
        kwargs["api_key"] = api_key
    if task and model_accepts_task(cfg.embedding_provider, cfg.embedding_model):
        kwargs["task"] = task
    return kwargs


# Providers whose ``/v1`` URL is a genuine OpenAI-compatible shim path
# (``POST <api_base>/embeddings`` in OpenAI shape works). Routing these
# through LiteLLM's ``openai`` SDK is correct and sidesteps native-handler
# quirks (URL builders, missing kwargs, etc.).
#
# Cohere is NOT here even though its API has ``/v1`` — Cohere's ``/v1``
# is its OWN native shape with ``/embed`` (not ``/embeddings``), so we
# leave Cohere on the native LiteLLM handler. Cohere does expose an
# OpenAI-compat path at ``/compatibility/v1`` separately; operators who
# want that will choose a different shim explicitly.
_OPENAI_SHIM_PROVIDERS: frozenset[str] = frozenset(
    {
        "openai",  # canonical — the openai SDK is the openai handler
        "ollama",
        "ollama_chat",  # Ollama /v1 shim works for embeddings
        "jina_ai",  # Jina /v1/embeddings is genuinely OpenAI-shaped
    }
)


def _route_embedding_for_dispatch(
    provider: str, model: str, api_base: str | None
) -> tuple[str, str, bool]:
    """Pick ``(litellm_provider, litellm_model, drop_api_base)`` for embedding.

    Three-rule decision tree that covers every provider switch + base_url
    combination the UI can produce:

    1. **Gemini override** — ``provider == "gemini"`` → native handler,
       DROP api_base. Google's OpenAI-compat shim at
       ``/v1beta/openai/embeddings`` is broken (internally proxies to
       ``v1main`` where ``text-embedding-*`` models don't exist → 404).
       The native ``/v1beta/models/<model>:batchEmbedContents`` works
       reliably and auto-maps ``dimensions`` → ``outputDimensionality``.
       Applied regardless of how the operator's base_url looks.

    2. **OpenAI-compat shim** — ``provider in _OPENAI_SHIM_PROVIDERS``
       AND ``api_base`` ends in ``/v1`` → openai SDK with bare model,
       keep api_base. LiteLLM POSTs ``<api_base>/embeddings``. Covers
       Ollama ``/v1``, Jina ``/v1``, OpenAI itself, plus any operator-
       configured custom proxy whose preset maps to ``openai``.

    3. **Native default** — anything else → use LiteLLM's native handler
       for the provider with whatever api_base the operator set (or
       LiteLLM's default if none). Covers Cohere, Voyage, Mistral, and
       any native-only embedding path.

    Why this shape:
      * Generalises across providers: switching an embedding endpoint
        from Jina to OpenAI to Gemini to Voyage works without any
        per-provider downstream code paths.
      * Mirrors :func:`services.llm_dispatch.route_for_endpoint` (chat
        side) with the single documented Gemini-embedding exception. The
        architecture review explicitly required both lanes use the same
        decision tree.
      * Robust to operator misconfiguration: an unknown ``base_url`` or
        a wrong shim suffix gracefully falls through to native handling
        instead of producing an opaque 404.

    Returns ``(litellm_provider, litellm_model, drop_api_base)``. The
    caller MUST strip ``api_base`` from the dispatch kwargs when the
    third return value is True.
    """
    base = (api_base or "").rstrip("/")
    bare = model.split("/", 1)[1] if "/" in model else model

    # Rule 1: Gemini — always native, drop api_base.
    if provider == "gemini":
        return "gemini", f"gemini/{bare}", True

    # Rule 2: OpenAI-compat shim — use openai SDK.
    if provider in _OPENAI_SHIM_PROVIDERS and base.endswith("/v1"):
        return "openai", bare, False

    # Rule 3: Native handler with operator's api_base (or LiteLLM's default).
    return provider, model, False


async def _aembedding_call(
    *,
    model: str,
    chunk: list[str],
    extra_kwargs: dict[str, Any],
    provider: str = "",
) -> list[list[float]]:
    """One LiteLLM ``aembedding`` round trip — extracted for test patchability.

    Routes through :func:`beever_atlas.services.llm_dispatch.dispatch_embedding`
    so the per-provider rate-limit throttle gates the call. The ``provider``
    kwarg is the LiteLLM prefix (``jina_ai``, ``openai``, …) — extracted by
    the caller from the resolved ``provider/model`` model string.

    PR-ζ.3: applies :func:`_route_embedding_for_dispatch`. For Gemini, this
    means routing through LiteLLM's native embedding handler and DROPPING
    ``api_base`` from the dispatch kwargs (Google's OpenAI-compat shim is
    broken for embeddings; we use the native ``v1beta/models/*:embedContent``
    path instead).
    """
    from beever_atlas.services.llm_dispatch import dispatch_embedding

    eff_provider = provider or model.split("/", 1)[0]
    api_base = (
        extra_kwargs.get("api_base") if isinstance(extra_kwargs.get("api_base"), str) else None
    )
    routed_provider, routed_model, drop_api_base = _route_embedding_for_dispatch(
        eff_provider, model, api_base
    )
    # Mutate-safely: caller may reuse extra_kwargs across chunks.
    dispatch_kwargs = dict(extra_kwargs)
    if drop_api_base:
        dispatch_kwargs.pop("api_base", None)

    response = await dispatch_embedding(
        provider=routed_provider,
        model=routed_model,
        input=chunk,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        **dispatch_kwargs,
    )
    # LiteLLM normalises every provider to OpenAI shape:
    #   response["data"] = [{"embedding": [...], "index": ...}, ...]
    # ``response`` may be a pydantic model or a plain dict depending on
    # litellm version — handle both.
    raw = response if isinstance(response, dict) else response.model_dump()
    data = raw.get("data") or []
    return [item["embedding"] for item in data]


def _retry_status(response_status: int) -> bool:
    return response_status in _RETRYABLE_STATUS


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with ±20% jitter to decorrelate concurrent batches."""
    return (2**attempt) * (1 + random.uniform(-0.2, 0.2))


async def embed_texts(
    texts: list[str],
    *,
    task: str | None = None,
    settings: Settings | None = None,
) -> list[list[float]]:
    """Embed `texts` and return one vector per input, in input order.

    Production callers should pass ``settings=None`` so the shim picks up
    UI-driven overrides via :func:`embedding_runtime.get_effective_embedding_settings`.
    The migration job calls this same function with the
    :func:`embedding_runtime.set_migration_context` contextvar set, which
    bypasses the migration gate so it can re-embed the existing data
    without tripping its own block.

    Args:
        texts: Inputs to embed. Empty list returns ``[]`` without making a
            request.
        task: Provider hint. Default ``None`` resolves to
            ``settings.embedding_task`` (``"text-matching"`` for Jina). The
            kwarg is dropped automatically for providers that don't honour
            it (see ``known_embedding_models.model_accepts_task``).
        settings: Override Settings — primarily for tests, the boot-time
            probe, and the Test Connection endpoint. Production callers
            should pass ``None``.

    Raises:
        EmbeddingMigrationInProgress: a re-embed migration is in flight
            and the caller is NOT the migration job. Callers should
            degrade (BM25 fallback for queries, empty-vectors for
            ingestion).
        EmbeddingProviderError: configured provider prefix is unknown.
        EmbeddingResponseError: provider returned a different number of
            vectors than texts in a chunk.
        httpx.HTTPStatusError: non-retryable provider HTTP error, or
            retry budget exhausted.
        httpx.ConnectError / ReadTimeout / RemoteProtocolError: same.
    """
    if not texts:
        return []

    # Resolve effective config. Live overlay (env + DB) when in production;
    # explicit ``settings=`` kwarg wins for tests/probe.
    if settings is not None:
        cfg = settings
    else:
        from beever_atlas.llm.embedding_runtime import (
            EmbeddingMigrationInProgress,
            in_migration_context,
            is_migration_in_progress,
            resolve_effective_settings,
        )

        # Migration gate — bypassed only by the re-embed script itself.
        if not in_migration_context() and await is_migration_in_progress():
            raise EmbeddingMigrationInProgress(
                "Embedding migration is running; switch to BM25 fallback or retry after completion."
            )

        cfg = await resolve_effective_settings()

    if not _runtime_initialised:
        # Defensive — covers test paths that import the shim before app boot.
        initialize_embedding_runtime(cfg)

    effective_task = task or cfg.embedding_task or "text-matching"
    model = _resolve_model_string(cfg)
    extra_kwargs = _build_extra_kwargs(cfg, task=effective_task)

    total_chunks = (len(texts) - 1) // _BATCH_SIZE + 1
    out: list[list[float]] = []

    for chunk_index_zero, start in enumerate(range(0, len(texts), _BATCH_SIZE)):
        chunk = texts[start : start + _BATCH_SIZE]
        chunk_index = chunk_index_zero + 1
        attempt = 0
        chunk_started = time.monotonic()

        embed_log(
            logger,
            "chunk start",
            provider=cfg.embedding_provider,
            model=cfg.embedding_model,
            chunk=f"{chunk_index}/{total_chunks}",
            size=len(chunk),
        )

        while True:
            try:
                async with EMBEDDING_LIMITER:
                    vectors = await _aembedding_call(
                        model=model,
                        chunk=chunk,
                        extra_kwargs=extra_kwargs,
                        provider=cfg.embedding_provider,
                    )
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
            ) as transient_err:
                attempt += 1
                if attempt > _MAX_RETRIES:
                    embed_log(
                        logger,
                        "chunk failed",
                        level="error",
                        provider=cfg.embedding_provider,
                        model=cfg.embedding_model,
                        chunk=f"{chunk_index}/{total_chunks}",
                        error=type(transient_err).__name__,
                        attempts=attempt,
                    )
                    raise
                wait = _backoff_seconds(attempt)
                embed_log(
                    logger,
                    "chunk transient retry",
                    level="warning",
                    provider=cfg.embedding_provider,
                    model=cfg.embedding_model,
                    chunk=f"{chunk_index}/{total_chunks}",
                    error=type(transient_err).__name__,
                    retry_in=round(wait, 2),
                    attempt=attempt,
                    max_retries=_MAX_RETRIES,
                )
                await asyncio.sleep(wait)
                continue
            except httpx.HTTPStatusError as http_err:
                if not _retry_status(http_err.response.status_code):
                    raise
                attempt += 1
                if attempt > _MAX_RETRIES:
                    embed_log(
                        logger,
                        "chunk failed",
                        level="error",
                        provider=cfg.embedding_provider,
                        model=cfg.embedding_model,
                        chunk=f"{chunk_index}/{total_chunks}",
                        status=http_err.response.status_code,
                        attempts=attempt,
                    )
                    raise
                wait = _backoff_seconds(attempt)
                embed_log(
                    logger,
                    "chunk retryable status",
                    level="warning",
                    provider=cfg.embedding_provider,
                    model=cfg.embedding_model,
                    chunk=f"{chunk_index}/{total_chunks}",
                    status=http_err.response.status_code,
                    retry_in=round(wait, 2),
                    attempt=attempt,
                    max_retries=_MAX_RETRIES,
                )
                await asyncio.sleep(wait)
                continue
            except Exception as litellm_err:
                # litellm wraps provider rate-limits as
                # ``litellm.RateLimitError`` — NOT ``httpx.HTTPStatusError`` —
                # so the previous branch never caught Jina/Gemini 429s and
                # they propagated out, hanging the re-embed migration on
                # the first burst that tripped provider limits. Catch the
                # broad ``Exception`` and let the dispatch layer's
                # ``_is_429`` predicate decide if we should retry; any
                # non-429 ``Exception`` re-raises and surfaces the real
                # cause.
                try:
                    from beever_atlas.services.llm_dispatch import _is_429
                except Exception:  # noqa: BLE001
                    raise litellm_err
                if not _is_429(litellm_err):
                    raise
                attempt += 1
                if attempt > _MAX_RETRIES:
                    embed_log(
                        logger,
                        "chunk failed",
                        level="error",
                        provider=cfg.embedding_provider,
                        model=cfg.embedding_model,
                        chunk=f"{chunk_index}/{total_chunks}",
                        status=429,
                        error=type(litellm_err).__name__,
                        attempts=attempt,
                    )
                    raise
                wait = _backoff_seconds(attempt)
                embed_log(
                    logger,
                    "chunk retryable status",
                    level="warning",
                    provider=cfg.embedding_provider,
                    model=cfg.embedding_model,
                    chunk=f"{chunk_index}/{total_chunks}",
                    status=429,
                    error=type(litellm_err).__name__,
                    retry_in=round(wait, 2),
                    attempt=attempt,
                    max_retries=_MAX_RETRIES,
                )
                await asyncio.sleep(wait)
                continue

            if len(vectors) != len(chunk):
                # Strict alignment — silent truncation would corrupt search.
                raise EmbeddingResponseError(
                    f"Provider returned {len(vectors)} vectors for "
                    f"{len(chunk)} inputs (chunk {chunk_index}/{total_chunks})"
                )
            out.extend(vectors)
            embed_log(
                logger,
                "chunk done",
                provider=cfg.embedding_provider,
                model=cfg.embedding_model,
                chunk=f"{chunk_index}/{total_chunks}",
                embedded=len(vectors),
                elapsed_ms=int((time.monotonic() - chunk_started) * 1000),
            )
            break

    return out


__all__ = [
    "EmbeddingError",
    "EmbeddingProviderError",
    "EmbeddingResponseError",
    "embed_texts",
    "initialize_embedding_runtime",
    "set_runtime_db_api_key",
]
