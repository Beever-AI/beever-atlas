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

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.models.platform_connection import PlatformConnection


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
