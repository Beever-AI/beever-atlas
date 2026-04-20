"""Unit test: WikiCache must reuse the same AsyncIOMotorClient across instances."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_wiki_cache_shares_motor_client() -> None:
    """Two WikiCache instances with the same URI must share one Motor client."""
    import beever_atlas.wiki.cache as cache_mod

    # Reset singleton state so the test is hermetic.
    original_clients = cache_mod._motor_clients.copy()
    original_lock = cache_mod._motor_clients_lock
    cache_mod._motor_clients.clear()
    cache_mod._motor_clients_lock = None

    created_clients: list[MagicMock] = []

    def _make_mock_client(*args, **kwargs):
        client = MagicMock()
        # Support client[db_name] -> db mock
        db = MagicMock()
        db.__getitem__ = MagicMock(return_value=MagicMock())
        client.__getitem__ = MagicMock(return_value=db)
        created_clients.append(client)
        return client

    try:
        with patch("beever_atlas.wiki.cache.AsyncIOMotorClient", side_effect=_make_mock_client):
            # Also patch WikiVersionStore to avoid it creating its own client
            with patch("beever_atlas.wiki.cache.WikiVersionStore") as mock_vs:
                mock_vs.return_value = MagicMock()

                uri = "mongodb://localhost:27017"
                c1 = cache_mod.WikiCache(uri)
                c2 = cache_mod.WikiCache(uri)

                await c1._ensure_db()
                await c2._ensure_db()

        # Only one AsyncIOMotorClient should have been constructed.
        assert len(created_clients) == 1, (
            f"Expected 1 Motor client, got {len(created_clients)}. WikiCache is leaking clients."
        )

        # The underlying Motor client stored in the singleton dict must be
        # the same object for both cache instances.
        assert c1._db.client is c2._db.client or cache_mod._motor_clients[uri] is created_clients[0]

    finally:
        # Restore original singleton state.
        cache_mod._motor_clients.clear()
        cache_mod._motor_clients.update(original_clients)
        cache_mod._motor_clients_lock = original_lock


@pytest.mark.asyncio
async def test_wiki_cache_different_uris_get_separate_clients() -> None:
    """Two WikiCache instances with different URIs get separate Motor clients."""
    import beever_atlas.wiki.cache as cache_mod

    original_clients = cache_mod._motor_clients.copy()
    original_lock = cache_mod._motor_clients_lock
    cache_mod._motor_clients.clear()
    cache_mod._motor_clients_lock = None

    created_clients: list[MagicMock] = []

    def _make_mock_client(*args, **kwargs):
        client = MagicMock()
        db = MagicMock()
        db.__getitem__ = MagicMock(return_value=MagicMock())
        client.__getitem__ = MagicMock(return_value=db)
        created_clients.append(client)
        return client

    try:
        with patch("beever_atlas.wiki.cache.AsyncIOMotorClient", side_effect=_make_mock_client):
            with patch("beever_atlas.wiki.cache.WikiVersionStore") as mock_vs:
                mock_vs.return_value = MagicMock()

                c1 = cache_mod.WikiCache("mongodb://host1:27017")
                c2 = cache_mod.WikiCache("mongodb://host2:27017")

                await c1._ensure_db()
                await c2._ensure_db()

        assert len(created_clients) == 2, (
            f"Expected 2 Motor clients for 2 different URIs, got {len(created_clients)}."
        )

    finally:
        cache_mod._motor_clients.clear()
        cache_mod._motor_clients.update(original_clients)
        cache_mod._motor_clients_lock = original_lock
