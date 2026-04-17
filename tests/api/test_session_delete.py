"""Integration tests for the session DELETE endpoint.

Verifies: POST (create) a chat session → DELETE → LIST → session not returned.
Also verifies: DELETE of a non-existent session_id returns 404.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

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
        "messages": [{"role": "user", "content": "hello", "timestamp": datetime.now(UTC).isoformat()}],
        "is_deleted": False,
        "pinned": False,
    }


class TestSessionDelete:
    """Backend DELETE handler: soft-deletes and excludes from LIST."""

    @pytest.mark.anyio
    async def test_delete_then_list_excludes_session(self, client: AsyncClient):
        """DELETE a session → LIST returns empty (session not present)."""
        _make_session_doc(SESSION_ID, USER_ID)

        # Simulate the DB: update_one matches 1 doc; find returns nothing after delete.
        mock_update_result = MagicMock()
        mock_update_result.matched_count = 1

        # list_sessions_global patch: after delete, session is gone
        mock_store = MagicMock()
        mock_store.startup = AsyncMock()
        mock_store.close = MagicMock()
        mock_store.list_sessions_global = AsyncMock(return_value=[])

        with (
            patch(
                "motor.motor_asyncio.AsyncIOMotorClient",
                autospec=False,
            ) as mock_motor_cls,
            patch(
                "beever_atlas.stores.chat_history_store.ChatHistoryStore",
                return_value=mock_store,
            ),
        ):
            # Wire the motor mock so update_one returns matched_count=1
            # ask.py does: client["beever_atlas"].chat_history.update_one(...)
            mock_collection = MagicMock()
            mock_collection.update_one = AsyncMock(return_value=mock_update_result)
            mock_db = MagicMock()
            mock_db.chat_history = mock_collection
            mock_client_instance = MagicMock()
            mock_client_instance.__getitem__ = MagicMock(return_value=mock_db)
            mock_client_instance.close = MagicMock()
            mock_motor_cls.return_value = mock_client_instance

            # DELETE the session
            resp = await client.delete(f"/api/ask/sessions/{SESSION_ID}")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
            assert resp.json() == {"status": "ok"}

            # update_one was called with correct filter including user_id
            mock_collection.update_one.assert_called_once()
            call_filter = mock_collection.update_one.call_args[0][0]
            assert call_filter["session_id"] == SESSION_ID
            assert call_filter["user_id"] == USER_ID

        # LIST sessions — store returns [] (session excluded after soft-delete)
        with patch(
            "beever_atlas.stores.chat_history_store.ChatHistoryStore",
            return_value=mock_store,
        ):
            list_resp = await client.get("/api/ask/sessions")
            assert list_resp.status_code == 200
            sessions = list_resp.json().get("sessions", [])
            ids = [s["session_id"] for s in sessions]
            assert SESSION_ID not in ids, f"Deleted session still in LIST: {ids}"

    @pytest.mark.anyio
    async def test_delete_nonexistent_returns_404(self, client: AsyncClient):
        """DELETE a session that doesn't exist → 404."""
        mock_update_result = MagicMock()
        mock_update_result.matched_count = 0

        with patch(
            "motor.motor_asyncio.AsyncIOMotorClient",
            autospec=False,
        ) as mock_motor_cls:
            mock_collection = MagicMock()
            mock_collection.update_one = AsyncMock(return_value=mock_update_result)
            mock_db = MagicMock()
            mock_db.chat_history = mock_collection
            mock_client_instance = MagicMock()
            mock_client_instance.__getitem__ = MagicMock(return_value=mock_db)
            mock_client_instance.close = MagicMock()
            mock_motor_cls.return_value = mock_client_instance

            resp = await client.delete("/api/ask/sessions/nonexistent-session-xyz")
            assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
