"""Single-funnel wrappers around ``litellm.acompletion`` / ``litellm.aembedding``.

Every LLM call in the codebase routes through these helpers so the
:class:`~beever_atlas.services.llm_throttle.LLMThrottle` is the single
point of rate-limit accounting. Callers pass the provider explicitly —
this module never tries to infer the provider from the model string,
because LiteLLM accepts both prefixed (``gemini/...``) and bare
(``gpt-4o``) forms and the calling layer always knows which is which.

Side benefit: the singular dispatch path makes future cross-provider
features (failover, cost accounting, prompt-cache integration) trivial
to layer in without re-touching every call site.
"""

from __future__ import annotations

import logging
from typing import Any

from beever_atlas.services.llm_throttle import get_llm_throttle

logger = logging.getLogger(__name__)


def _estimate_completion_tokens(messages: Any) -> int:
    """Rough token estimate for a chat-completion request.

    LiteLLM exposes ``token_counter`` but it imports tiktoken eagerly and
    bumps cold-start by ~200ms. The 4-chars-per-token heuristic is
    conservative enough for throttle accounting (we'd rather over-budget
    by 2x than miss a 429 by under-budgeting). Floor of 1000 to cover
    response tokens which the bucket should account for too.
    """
    try:
        return max(len(str(messages)) // 4, 1000)
    except Exception:  # noqa: BLE001 — never crash on a token guess
        return 1000


def _estimate_embedding_tokens(payload: str | list[Any]) -> int:
    """Rough token estimate for an embedding request."""
    try:
        if isinstance(payload, str):
            return max(len(payload) // 4, 100)
        # list of strings (or mixed)
        total = sum(len(str(s)) for s in payload)
        return max(total // 4, 100)
    except Exception:  # noqa: BLE001
        return 100


def normalize_litellm_model(model_string: str) -> str:
    """Return a LiteLLM-compatible model identifier.

    Bare ``gemini-*`` strings (the legacy format read from ``LLM_FAST_MODEL`` /
    ``agent_model_config``) get normalised to ``gemini/<model>``. Already-prefixed
    strings (``openai/...``, ``anthropic/...``, ``ollama_chat/...``) pass through.

    Mirrors the inline normalisation inside
    :func:`beever_atlas.llm.model_resolver.resolve_model_object` so call sites
    that build their own ``dispatch_completion`` request can share the same
    prefix logic without depending on the resolver's LiteLlm wrapping path.
    """
    if "/" in model_string:
        return model_string
    if model_string.startswith("gemini-"):
        return f"gemini/{model_string}"
    return model_string


def sniff_provider(model_string: str) -> str:
    """Extract the LiteLLM provider prefix from a model identifier.

    For prefixed strings (``openai/gpt-4o-mini``) returns ``openai``. For bare
    ``gemini-*`` strings returns ``gemini``. Other unprefixed strings default
    to ``gemini`` (the historical Atlas default; this matches the behaviour of
    the throttle which keyed on provider name before the per-Endpoint rework
    in PR-B).
    """
    if "/" in model_string:
        return model_string.split("/", 1)[0]
    if model_string.startswith("gemini-"):
        return "gemini"
    return "gemini"


def _is_ollama_connect_error(exc: BaseException, provider: str) -> bool:
    """Detect a connection failure against an Ollama Endpoint.

    Triggers a force-invalidation of ``LLMProvider._check_ollama_cached`` so
    a restarted daemon recovers without waiting the full 30s TTL window.
    See `agent-llm-provider-pluggable` design D8.
    """
    if not provider.startswith("ollama"):
        return False
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout))


def _is_429(exc: BaseException) -> bool:
    """Detect rate-limit errors from LiteLLM and from raw HTTP responses.

    LiteLLM wraps provider errors in ``litellm.RateLimitError``. Some
    providers (Gemini via the genai client path) surface 429 as a
    ``google.api_core.exceptions.ResourceExhausted`` with no LiteLLM
    wrapping; we still want the throttle to learn from those, so we
    sniff the exception's ``status_code`` / ``code`` attribute and the
    error message as a backstop.
    """
    try:
        import litellm  # type: ignore[import-untyped]

        if isinstance(exc, litellm.RateLimitError):
            return True
    except Exception:  # noqa: BLE001 — litellm import-time issues
        pass
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code == 429:
        return True
    msg = str(exc).lower()
    # ``rate_concurrency_limit_exceeded`` is Jina's per-tier cap on
    # in-flight requests (returned as a non-429 HTTP status with a
    # provider-specific code). It is functionally a rate-limit signal
    # — wait for in-flight requests to complete, then retry. Without
    # this branch the re-embed migration died silently the moment we
    # exceeded the free-tier ceiling of 2 concurrent embed calls.
    rate_limit_phrases = (
        "429",
        "rate limit",
        "rate_limit",
        "rate-limit",
        "concurrency limit",
        "concurrency_limit",
        "concurrency-limit",
        "too many requests",
        "rate_concurrency",
    )
    return any(phrase in msg for phrase in rate_limit_phrases)


async def dispatch_completion(
    *,
    provider: str,
    model: str,
    messages: list[Any],
    endpoint_id: str | None = None,
    **kwargs: Any,
) -> Any:
    """Throttle-gated wrapper around ``litellm.acompletion``.

    Caller passes ``provider`` explicitly (the static provider prefix —
    e.g. ``"gemini"``, ``"openai"``, ``"ollama"``) so the throttle keys
    on the rate-limited entity rather than the LiteLLM model string.

    ``endpoint_id`` is optional — when provided, the throttle bucket key
    becomes ``f"{provider}:{endpoint_id}"`` so two same-provider Endpoints
    get independent rate-limit state. Backward-compatible with PR-A callers
    that pass only ``(provider, model, messages)``.
    """
    import litellm  # type: ignore[import-untyped]

    throttle = get_llm_throttle()
    est_tokens = _estimate_completion_tokens(messages)
    async with throttle.acquire(provider, est_tokens, endpoint_id=endpoint_id):
        try:
            response = await litellm.acompletion(model=model, messages=messages, **kwargs)
        except BaseException as exc:
            if _is_429(exc):
                throttle.report_429(provider, endpoint_id=endpoint_id)
            elif _is_ollama_connect_error(exc, provider):
                # Force-invalidate the Ollama health cache so the next
                # ``resolve_model`` re-probes immediately rather than waiting
                # out the 30s TTL. Defensive: never let this crash dispatch.
                try:
                    from beever_atlas.llm.provider import get_llm_provider

                    get_llm_provider().invalidate_ollama_cache()
                except Exception:  # noqa: BLE001
                    pass
            # Per-Endpoint circuit breaker: a non-429 failure is a real
            # outage signal — record it so the Endpoint's breaker can trip
            # and the resolver can route Assignments with a fallback away
            # from it. 429s are rate-limit, not outage — handled by the
            # throttle cooldown above, not the breaker. Defensive.
            if endpoint_id and not _is_429(exc):
                await _record_breaker_failure(endpoint_id, exc)
            raise
        # Some providers return a 429 inline on the response body without
        # raising. Sniff status_code on the response just in case.
        status_code = getattr(response, "status_code", None)
        if status_code == 429:
            throttle.report_429(provider, endpoint_id=endpoint_id)
        elif endpoint_id:
            # Clean response — record success so a recovering Endpoint's
            # half-open probe closes the breaker.
            await _record_breaker_success(endpoint_id)
        return response


async def _record_breaker_failure(endpoint_id: str, exc: BaseException) -> None:
    """Record a failure against the per-Endpoint circuit breaker. Never raises."""
    try:
        from beever_atlas.services.circuit_breaker import get_breaker_for_endpoint

        await get_breaker_for_endpoint(endpoint_id).record_failure(exc)
    except Exception:  # noqa: BLE001
        pass


async def _record_breaker_success(endpoint_id: str) -> None:
    """Record a success against the per-Endpoint circuit breaker. Never raises."""
    try:
        from beever_atlas.services.circuit_breaker import get_breaker_for_endpoint

        await get_breaker_for_endpoint(endpoint_id).record_success()
    except Exception:  # noqa: BLE001
        pass


async def dispatch_assignment(
    *,
    assignment: Any,
    messages: list[Any],
    **call_kwargs: Any,
) -> Any:
    """Preferred dispatch path for the new Endpoint+Assignment data model.

    Accepts a :class:`beever_atlas.llm.assignments.ResolvedAssignment` (typed as
    ``Any`` here to avoid a circular import) and forwards every per-call param
    to ``dispatch_completion``:

      * ``provider`` ← ``assignment.provider``
      * ``model`` ← ``assignment.litellm_model``
      * ``endpoint_id`` ← ``assignment.endpoint_id`` (per-Endpoint throttle key)
      * ``api_base`` ← ``assignment.base_url`` when set
      * ``api_key`` ← ``assignment.api_key`` when set (otherwise LiteLLM reads
        the provider-default env var)
      * ``aws_credentials`` / ``vertex_credentials`` ← when ``auth_type``
        is ``aws_iam`` / ``google_sa``
      * ``extra_headers`` ← merged from Endpoint + Assignment
      * ``temperature`` / ``max_tokens`` / ``response_format`` ← when set

    Caller-supplied ``call_kwargs`` override Assignment defaults (preserves
    agent code that needs tight control — e.g. an agent with its own JSON
    schema overrides the Assignment's ``response_format``).
    """
    kwargs: dict[str, Any] = {}
    if assignment.base_url:
        kwargs["api_base"] = assignment.base_url
    if assignment.api_key:
        kwargs["api_key"] = assignment.api_key
    if getattr(assignment, "aws_credentials", None):
        kwargs["aws_access_key_id"] = assignment.aws_credentials["access_key_id"]
        kwargs["aws_secret_access_key"] = assignment.aws_credentials["secret_access_key"]
        kwargs["aws_region_name"] = assignment.aws_credentials["region"]
    if getattr(assignment, "vertex_credentials", None):
        kwargs["vertex_credentials"] = assignment.vertex_credentials["sa_json"]
    if assignment.extra_headers:
        kwargs["extra_headers"] = dict(assignment.extra_headers)
    if assignment.temperature is not None:
        kwargs["temperature"] = assignment.temperature
    if assignment.max_tokens is not None:
        kwargs["max_tokens"] = assignment.max_tokens
    if assignment.response_format is not None:
        # OpenAI shape: ``{"type": "json_object"}`` for JSON mode, ``{"type": "text"}`` otherwise.
        kwargs["response_format"] = {"type": (
            "json_object" if assignment.response_format == "json" else "text"
        )}

    # Caller kwargs win — they're the most specific intent.
    kwargs.update(call_kwargs)

    return await dispatch_completion(
        provider=assignment.provider,
        model=assignment.litellm_model,
        messages=messages,
        endpoint_id=assignment.endpoint_id,
        **kwargs,
    )


async def dispatch_embedding(
    *,
    provider: str,
    model: str,
    input: str | list[Any],
    **kwargs: Any,
) -> Any:
    """Throttle-gated wrapper around ``litellm.aembedding``."""
    import litellm  # type: ignore[import-untyped]

    throttle = get_llm_throttle()
    est_tokens = _estimate_embedding_tokens(input)
    async with throttle.acquire(provider, est_tokens):
        try:
            response = await litellm.aembedding(model=model, input=input, **kwargs)
        except BaseException as exc:
            if _is_429(exc):
                throttle.report_429(provider)
            raise
        status_code = getattr(response, "status_code", None)
        if status_code == 429:
            throttle.report_429(provider)
        return response


__all__ = ["dispatch_completion", "dispatch_embedding"]
