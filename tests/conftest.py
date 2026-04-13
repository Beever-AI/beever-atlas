"""Shared test fixtures.

Tests that exercise FastAPI endpoints via httpx's `ASGITransport` do not
trigger the app's lifespan hook, so `beever_atlas.stores.get_stores()`
would raise `RuntimeError: Stores not initialized`. This fixture wires
up a lightweight mock `StoreClients` per test module that opts in via
the `mock_stores` fixture.

Integration tests that need real stores should override this by
depending on `mock_stores_disabled`.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.models.platform_connection import PlatformConnection

# Route ChatHistoryStore writes to an isolated test DB so tests that exercise
# the real Ask endpoint don't pollute the dev sidebar (`beever_atlas.chat_history`).
# Set at import time so any `ChatHistoryStore(...)` constructed during test
# collection / fixtures picks it up.
os.environ.setdefault("BEEVER_CHAT_HISTORY_DB", "beever_atlas_test")


@pytest.fixture(scope="session", autouse=True)
def _drop_chat_history_test_db():
    """Drop the test chat_history DB at session end to keep it empty."""
    yield
    try:
        from pymongo import MongoClient

        from beever_atlas.infra.config import get_settings

        uri = get_settings().mongodb_uri
        db_name = os.environ.get("BEEVER_CHAT_HISTORY_DB", "beever_atlas_test")
        if db_name == "beever_atlas":
            return  # never drop the real DB
        MongoClient(uri, serverSelectionTimeoutMS=1000).drop_database(db_name)
    except Exception:
        pass


def _build_mock_connection(connection_id: str = "conn-mock") -> PlatformConnection:
    """One connected Slack connection — enough to satisfy channels.py flows."""
    return PlatformConnection(
        id=connection_id,
        platform="slack",
        source="env",
        display_name="mock-workspace",
        status="connected",
        selected_channels=[],
        encrypted_credentials=b"",
        credential_iv=b"",
        credential_tag=b"",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def _reset_wiki_motor_singleton():
    """Clear the WikiCache Motor-client singleton between tests.

    Without this, a test that populates _motor_clients with a mock would
    bleed into the next test, causing it to skip re-initialization even when
    the next test patches AsyncIOMotorClient with a fresh mock.
    """
    import beever_atlas.wiki.cache as cache_mod

    original = cache_mod._motor_clients.copy()
    original_lock = cache_mod._motor_clients_lock
    cache_mod._motor_clients.clear()
    cache_mod._motor_clients_lock = None
    try:
        yield
    finally:
        cache_mod._motor_clients.clear()
        cache_mod._motor_clients.update(original)
        cache_mod._motor_clients_lock = original_lock


@pytest.fixture
def mock_stores():
    """Install a MagicMock StoreClients for the duration of the test.

    The mock provides just enough shape for the `/api/channels`,
    `/api/channels/{id}`, `/api/channels/{id}/messages` paths to
    succeed with the MockAdapter.

    Tests that don't need it can simply not depend on this fixture.
    """
    import beever_atlas.stores as stores_mod

    saved = stores_mod._stores

    fake = MagicMock(name="MockStoreClients")
    fake.platform = MagicMock()
    fake.platform.list_connections = AsyncMock(
        return_value=[_build_mock_connection()]
    )
    fake.mongodb = MagicMock()
    fake.mongodb.list_synced_channel_ids = AsyncMock(return_value=[])
    fake.mongodb.get_channel_display_name = AsyncMock(return_value=None)
    fake.mongodb.get_channel_sync_state = AsyncMock(return_value=None)

    stores_mod._stores = fake
    try:
        yield fake
    finally:
        stores_mod._stores = saved
