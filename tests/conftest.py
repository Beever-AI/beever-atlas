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
    """One connected Slack connection — enough to satisfy channels.py flows.

    `selected_channels` includes the MockAdapter channel ids so the RES-177
    `_assert_channel_access` guard admits `user:test` (via the
    single-tenant ``legacy:shared`` fallback) on the mock workspace.
    Tests that exercise cross-user denial install their own connection.
    """
    return PlatformConnection(
        id=connection_id,
        platform="slack",
        source="env",
        display_name="mock-workspace",
        status="connected",
        selected_channels=["C_MOCK_GENERAL", "C_MOCK_ENGINEERING", "C_MOCK_RANDOM"],
        encrypted_credentials=b"",
        credential_iv=b"",
        credential_tag=b"",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        # Shared sentinel → single-tenant fallback admits `user:test` on any
        # channel this connection advertises. Tests that exercise cross-user
        # denial set an explicit owner instead.
        owner_principal_id="legacy:shared",
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


@pytest.fixture(autouse=True)
def _auth_bypass(monkeypatch):
    """Bypass the global `require_user` dependency for endpoint tests.

    PR #2 added a FastAPI `require_user` dependency to protected routers.
    Tests that construct `TestClient`/`AsyncClient` without Authorization
    headers would otherwise get 401. We install a FastAPI
    `dependency_overrides` entry that returns a static test user, and also
    set `BEEVER_API_KEYS` so tests that exercise the real dependency (e.g.
    `test_auth.py`) can still pass explicit Bearer tokens.
    """
    monkeypatch.setenv("BEEVER_API_KEYS", "test-key")
    monkeypatch.setenv("BEEVER_ENV", "test")

    try:
        from beever_atlas.infra.auth import Principal, require_user
        from beever_atlas.server.app import app
    except Exception:
        yield
        return

    def _fake_user() -> Principal:
        # Return a proper Principal — RES-177 H1 adds code paths (notably
        # `infra.channel_access.assert_channel_access`) that inspect
        # `.kind` and `.id`. Plain strings still work because Principal
        # subclasses str, but the guard's single-tenant fallback needs
        # `kind == "user"`.
        return Principal("user:test", kind="user")

    saved = app.dependency_overrides.get(require_user)
    app.dependency_overrides[require_user] = _fake_user
    try:
        yield
    finally:
        if saved is None:
            app.dependency_overrides.pop(require_user, None)
        else:
            app.dependency_overrides[require_user] = saved


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer header matching the `BEEVER_API_KEYS=test-key` set in `_auth_bypass`."""
    return {"Authorization": "Bearer test-key"}
