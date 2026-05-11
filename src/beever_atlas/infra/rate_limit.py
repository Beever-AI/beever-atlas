"""Shared rate limiters.

Kept in its own module so limiter instances can be imported by both
`server/app.py` and route modules (e.g. `api/ask.py`) without causing a
circular import.

Exports the lazy AsyncLimiter singletons used by the ingestion pipeline:
  * ``GEMINI_LIMITER``    — chat / extraction calls.
  * ``EMBEDDING_LIMITER`` — provider-agnostic embedding calls (built on
    LiteLLM via ``llm.embeddings.embed_texts``).
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
_embedding_limiter = None


def _get_gemini_limiter():
    global _gemini_limiter
    if _gemini_limiter is None:
        from aiolimiter import AsyncLimiter

        _gemini_limiter = AsyncLimiter(max_rate=get_settings().gemini_rpm, time_period=60)
    return _gemini_limiter


def _get_embedding_limiter():
    global _embedding_limiter
    if _embedding_limiter is None:
        from aiolimiter import AsyncLimiter

        # ``embedding_rpm`` defaults to the legacy jina_rpm via the
        # config-level alias bridge, so existing installs keep their
        # configured rate without an .env change.
        _embedding_limiter = AsyncLimiter(
            max_rate=get_settings().embedding_rpm,
            time_period=60,
        )
    return _embedding_limiter


class _LazyLimiter:
    """Proxy that forwards ``async with`` to the lazily created real limiter."""

    def __init__(self, factory):
        self._factory = factory

    async def __aenter__(self):
        return await self._factory().__aenter__()

    async def __aexit__(self, *args):
        return await self._factory().__aexit__(*args)


GEMINI_LIMITER: _LazyLimiter = _LazyLimiter(_get_gemini_limiter)
EMBEDDING_LIMITER: _LazyLimiter = _LazyLimiter(_get_embedding_limiter)
