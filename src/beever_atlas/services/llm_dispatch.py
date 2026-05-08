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
    return "429" in msg or "rate limit" in msg or "rate_limit" in msg or "rate-limit" in msg


async def dispatch_completion(
    *,
    provider: str,
    model: str,
    messages: list[Any],
    **kwargs: Any,
) -> Any:
    """Throttle-gated wrapper around ``litellm.acompletion``.

    Caller passes ``provider`` explicitly (the static provider prefix —
    e.g. ``"gemini"``, ``"openai"``, ``"ollama"``) so the throttle keys
    on the rate-limited entity rather than the LiteLLM model string.
    """
    import litellm  # type: ignore[import-untyped]

    throttle = get_llm_throttle()
    est_tokens = _estimate_completion_tokens(messages)
    async with throttle.acquire(provider, est_tokens):
        try:
            response = await litellm.acompletion(model=model, messages=messages, **kwargs)
        except BaseException as exc:
            if _is_429(exc):
                throttle.report_429(provider)
            raise
        # Some providers return a 429 inline on the response body without
        # raising. Sniff status_code on the response just in case.
        status_code = getattr(response, "status_code", None)
        if status_code == 429:
            throttle.report_429(provider)
        return response


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
