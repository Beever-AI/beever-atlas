"""Integration tests for the session DELETE endpoint.

Verifies: POST (create) a chat session → DELETE → LIST → session not returned.
Also verifies: DELETE of a non-existent session_id returns 404.

After issue #31 Phase 3 migration, these tests patch the StoreClients
singleton (`beever_atlas.stores._stores`) instead of the per-request
`AsyncIOMotorClient` / `ChatHistoryStore` constructions, since the
endpoints now read from the shared singleton.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import beever_atlas.stores as stores_mod
from beever_atlas.server.app import app

SESSION_ID = "test-delete-session-abc123"
USER_ID = "user:test"  # conftest `_auth_bypass` principal id post-RES-177


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_session_doc(session_id: str, user_id: str) -> dict:
    return {
        "session_id": session_id,
        "user_id": user_id,
        "title": "Test session",
        "created_at": datetime.now(UTC),
        "messages": [
            {"role": "user", "content": "hello", "timestamp": datetime.now(UTC).isoformat()}
        ],
        "is_deleted": False,
        "pinned": False,
    }


def _install_fake_stores(*, matched_count: int, sessions: list[dict] | None = None):
    """Install a MagicMock StoreClients singleton with the chat_history.update_one
    and chat_history.list_sessions_global behavior the test needs. Returns the
    fake and a saved-original tuple for the test to restore."""
    saved = stores_mod._stores

    fake = MagicMock(name="FakeStoreClients")

    # delete_ask_session() reads `get_stores().mongodb.db.chat_history.update_one(...)`
    update_result = MagicMock()
    update_result.matched_count = matched_count
    update_call_capture: list = []

    async def _update_one(filt, update):
        update_call_capture.append((filt, update))
        return update_result

    fake.mongodb.db.chat_history.update_one = AsyncMock(side_effect=_update_one)

    # list_ask_sessions() reads `get_stores().chat_history.list_sessions_global(...)`
    fake.chat_history.list_sessions_global = AsyncMock(return_value=sessions or [])

    stores_mod._stores = fake
    return fake, update_call_capture, saved


class TestSessionDelete:
    """Backend DELETE handler: soft-deletes and excludes from LIST."""

    @pytest.mark.anyio
    async def test_delete_then_list_excludes_session(self, client: AsyncClient):
        """DELETE a session → LIST returns empty (session not present)."""
        _make_session_doc(SESSION_ID, USER_ID)

        fake, update_calls, saved = _install_fake_stores(matched_count=1)
        try:
            # DELETE the session
            resp = await client.delete(f"/api/ask/sessions/{SESSION_ID}")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
            assert resp.json() == {"status": "ok"}

            # update_one was called with correct filter including user_id
            assert len(update_calls) == 1
            call_filter = update_calls[0][0]
            assert call_filter["session_id"] == SESSION_ID
            assert call_filter["user_id"] == USER_ID

            # LIST sessions — store returns [] (session excluded after soft-delete)
            list_resp = await client.get("/api/ask/sessions")
            assert list_resp.status_code == 200
            sessions = list_resp.json().get("sessions", [])
            ids = [s["session_id"] for s in sessions]
            assert SESSION_ID not in ids, f"Deleted session still in LIST: {ids}"
        finally:
            stores_mod._stores = saved

    @pytest.mark.anyio
    async def test_delete_nonexistent_returns_404(self, client: AsyncClient):
        """DELETE a session that doesn't exist → 404."""
        _, _, saved = _install_fake_stores(matched_count=0)
        try:
            resp = await client.delete("/api/ask/sessions/nonexistent-session-xyz")
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
        finally:
            stores_mod._stores = saved
