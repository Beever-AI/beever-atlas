"""Deprecation-header dependency for the legacy LLM-config routes.

``/api/settings/embedding/*`` and ``/api/settings/models/*`` are superseded by
``/api/settings/endpoints/*`` + ``/api/settings/assignments/*`` (the unified
Endpoint+Assignment model). They keep working for one release; this dependency
stamps the standard ``Sunset`` + ``Deprecation`` response headers (RFC 8594 /
draft-ietf-httpapi-deprecation-header) so external consumers get a heads-up,
and emits a single WARN log per process so operators see it too.

See ``openspec/changes/agent-llm-provider-pluggable/`` PR-E.2 + the Phase-5
cleanup change (legacy removal) tracked in ``tasks.md`` §11.
"""

from __future__ import annotations

import logging

from fastapi import Response

logger = logging.getLogger(__name__)

# Process-local: only WARN once per route group, not once per request.
_warned: set[str] = set()


def deprecated_route(replacement: str):  # noqa: ANN201 — returns a FastAPI dependency
    """Build a dependency that marks a route group deprecated.

    ``replacement`` is the human-pointer string surfaced in the
    ``Deprecation`` header value and the WARN log (e.g.
    ``"/api/settings/endpoints + /api/settings/assignments"``).
    """

    async def _dep(response: Response) -> None:
        # ``Sunset`` per RFC 8594 — no fixed date yet (removal is a future
        # release after the deprecation window), so use ``Sunset: true`` to
        # signal "this endpoint will go away" without committing to a date.
        response.headers["Sunset"] = "true"
        response.headers["Deprecation"] = "true"
        response.headers["Link"] = f'<{replacement}>; rel="successor-version"'
        if replacement not in _warned:
            _warned.add(replacement)
            logger.warning(
                "deprecated route hit: superseded by %s — see docs/runbooks/ai-setup.md. "
                "These routes will be removed in a future release.",
                replacement,
            )

    return _dep


__all__ = ["deprecated_route"]
