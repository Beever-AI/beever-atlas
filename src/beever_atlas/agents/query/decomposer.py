"""Query decomposer: classifies questions as simple or complex and decomposes them."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from beever_atlas.agents.query.prompts import DECOMPOSITION_PROMPT

logger = logging.getLogger(__name__)

# Patterns signaling a complex multi-part question
_COMPLEX_PATTERNS = re.compile(
    r"\b(and|vs\.?|versus|compare|also|additionally|furthermore|"
    r"as well as|not only|but also|both|between|difference between|"
    r"still|anymore|currently|nowadays|is it still|should we)\b",
    re.IGNORECASE,
)


@dataclass
class SubQuery:
    query: str
    focus: str
    is_external: bool = False


@dataclass
class QueryPlan:
    original: str
    is_simple: bool
    internal_queries: list[SubQuery] = field(default_factory=list)
    external_queries: list[SubQuery] = field(default_factory=list)


def _is_simple(question: str) -> bool:
    """Fast-path heuristic: returns True if question can skip decomposition.

    Simple questions: single entity/topic, no conjunctions, short.
    Cost: $0 (no LLM call).

    Complexity triggers (any one is sufficient to force LLM decomposition):
    - Existing ``_COMPLEX_PATTERNS`` regex (vs/compare/and/etc.)
    - Length > 10 words
    - Coordinating conjunctions: "and", "or"
    - Comma separating list items (e.g. "impact of X, Y, and Z")
    - Multiple question marks (compound question)
    - List-style enumeration (e.g. "X, Y, Z" within the question)
    """
    if _COMPLEX_PATTERNS.search(question):
        return False
    words = question.split()
    if len(words) > 10:
        return False
    # Coordinating conjunctions that signal multiple aspects
    lower = question.lower()
    if re.search(r"\band\b|\bor\b", lower):
        return False
    # Comma signals list/enumeration
    if "," in question:
        return False
    # Multiple question marks → compound question
    if question.count("?") > 1:
        return False
    return True


async def decompose(question: str) -> QueryPlan:
    """Classify a question and optionally decompose it into parallel sub-queries.

    Simple questions → fast path (no LLM call, $0 cost).
    Complex questions → decompose via qa_router (Flash Lite).
    Failure → fall back to original question as a single internal query.
    """
    if _is_simple(question):
        logger.debug("QueryDecomposer: fast path for %r", question[:60])
        return QueryPlan(
            original=question,
            is_simple=True,
            internal_queries=[SubQuery(query=question, focus="main")],
        )

    return await _decompose_complex(question)


async def _decompose_complex(question: str) -> QueryPlan:
    """LLM-based decomposition for complex questions."""
    try:
        from beever_atlas.llm.provider import get_llm_provider

        provider = get_llm_provider()
        prompt = DECOMPOSITION_PROMPT.format(question=question)

        # Prefer the Endpoint+Assignment path — pulls the qa_router's
        # api_key + api_base + per-call params from the Assignment row, so
        # a custom provider (Z.AI / GLM, OpenRouter, Anthropic, …) gets the
        # credentials it needs. Without this the previous
        # ``dispatch_completion(provider=…, model=…)`` path called LiteLLM
        # with the model id but no credentials, then LiteLLM fell back to
        # the provider-default env var (``OPENAI_API_KEY`` for ``openai/*``
        # ids — but Z.AI's GLM endpoint uses a *different* API key, so the
        # call 401s in ~20ms).
        resolved = await provider.resolve_for_call("qa_router")

        if resolved is not None:
            from beever_atlas.services.llm_dispatch import dispatch_assignment

            response = await asyncio.wait_for(
                dispatch_assignment(
                    assignment=resolved,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=10.0,
            )
        else:
            # No Assignment row — fall back to legacy ``resolve_model`` +
            # ``dispatch_completion`` (used by ingestion agents that haven't
            # been migrated, or in tests with a bare LLMProvider). LiteLLM
            # picks up the provider-default env var here, matching the
            # historical behaviour.
            from beever_atlas.services.llm_dispatch import (
                dispatch_completion,
                normalize_litellm_model,
                sniff_provider,
            )

            model_name = provider.get_model_string("qa_router")
            response = await asyncio.wait_for(
                dispatch_completion(
                    provider=sniff_provider(model_name),
                    model=normalize_litellm_model(model_name),
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=10.0,
            )
        text = (response.choices[0].message.content or "").strip()  # type: ignore[index, union-attr]

        # Strip markdown fences if present
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

        data = json.loads(text)

        internal_queries = [
            SubQuery(query=q["query"], focus=q.get("focus", ""), is_external=False)
            for q in data.get("internal_queries", [])[:4]  # max 4
        ]
        external_queries = [
            SubQuery(query=q["query"], focus=q.get("focus", ""), is_external=True)
            for q in data.get("external_queries", [])[:2]  # max 2
        ]

        if not internal_queries:
            internal_queries = [SubQuery(query=question, focus="main")]

        logger.debug(
            "QueryDecomposer: %d internal + %d external sub-queries for %r",
            len(internal_queries),
            len(external_queries),
            question[:60],
        )

        return QueryPlan(
            original=question,
            is_simple=False,
            internal_queries=internal_queries,
            external_queries=external_queries,
        )

    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "QueryDecomposer: decomposition timed out after 10s for %r (degraded, returning 1 query)",
            question[:80],
        )
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.warning(
            "QueryDecomposer: JSON parse failed for %r — %s (degraded, returning 1 query)",
            question[:80],
            exc,
        )
    except Exception:
        logger.warning(
            "QueryDecomposer: unexpected error for %r (degraded, returning 1 query)",
            question[:80],
            exc_info=True,
        )

    # Fallback: single internal query, no decomposition error surfaced to user
    return QueryPlan(
        original=question,
        is_simple=False,
        internal_queries=[SubQuery(query=question, focus="main")],
    )
