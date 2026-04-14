"""Shared rate limiters.

Kept in its own module so limiter instances can be imported by both
`server/app.py` and route modules (e.g. `api/ask.py`) without causing a
circular import.

Also exports ``GEMINI_LIMITER`` and ``JINA_LIMITER`` — lazy AsyncLimiter
singletons used by the ingestion pipeline to respect provider RPM caps.
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from beever_atlas.infra.config import get_settings

_settings = get_settings()
_storage_uri = _settings.redis_url or "memory://"

limiter = Limiter(key_func=get_remote_address, storage_uri=_storage_uri)

# ---------------------------------------------------------------------------
# Provider rate limiters (aiolimiter)
# ---------------------------------------------------------------------------

_gemini_limiter = None
_jina_limiter = None


def _get_gemini_limiter():
    global _gemini_limiter
    if _gemini_limiter is None:
        from aiolimiter import AsyncLimiter
        _gemini_limiter = AsyncLimiter(max_rate=get_settings().gemini_rpm, time_period=60)
    return _gemini_limiter


def _get_jina_limiter():
    global _jina_limiter
    if _jina_limiter is None:
        from aiolimiter import AsyncLimiter
        _jina_limiter = AsyncLimiter(max_rate=get_settings().jina_rpm, time_period=60)
    return _jina_limiter


class _LazyLimiter:
    """Proxy that forwards ``async with`` to the lazily created real limiter."""

    def __init__(self, factory):
        self._factory = factory

    async def __aenter__(self):
        return await self._factory().__aenter__()

    async def __aexit__(self, *args):
        return await self._factory().__aexit__(*args)


GEMINI_LIMITER: _LazyLimiter = _LazyLimiter(_get_gemini_limiter)
JINA_LIMITER: _LazyLimiter = _LazyLimiter(_get_jina_limiter)
