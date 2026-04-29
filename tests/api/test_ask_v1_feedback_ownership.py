"""Issue #45: the v1 `POST /api/channels/{channel_id}/ask/feedback`
endpoint must reject sessions not owned by the caller — same fix as
RES-202 / `test_ask_feedback_ownership.py` for the v2 endpoint.

Before this fix, any authenticated user could upsert ``qa_feedback``
keyed by `(session_id, message_id)` against another user's session_id
and overwrite their feedback.

Mirrors `test_ask_feedback_ownership.py` (v2) so the test shapes stay
in sync.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.config import get_settings
from beever_atlas.server.app import app
from beever_atlas.stores.chat_history_store import ChatHistoryStore

OWNER = Principal("user:owner-v1fb", kind="user")
STRANGER = Principal("user:stranger-v1fb", kind="user")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _install_principal(p: Principal):
    saved = app.dependency_overrides.get(require_user)
    app.dependency_overrides[require_user] = lambda: p

    def _restore() -> None:
        if saved is None:
            app.dependency_overrides.pop(require_user, None)
        else:
            app.dependency_overrides[require_user] = saved

    return _restore


@pytest.fixture
def as_owner(_auth_bypass):
    restore = _install_principal(OWNER)
    try:
        yield OWNER
    finally:
        restore()


@pytest.fixture
def as_stranger(_auth_bypass):
    restore = _install_principal(STRANGER)
    try:
        yield STRANGER
    finally:
        restore()


@pytest.fixture
async def owned_session():
    """Seed a chat_history session owned by OWNER and clean up after."""
    session_id = f"test-v1fb-{uuid.uuid4()}"
    settings = get_settings()
    store = ChatHistoryStore(settings.mongodb_uri)
    await store.startup()
    try:
        await store._collection.insert_one(
            {
                "session_id": session_id,
                "user_id": str(OWNER),
                "channel_id": "C_MOCK_GENERAL",
                "title": "v1-fb-test",
                "messages": [],
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        )
        yield session_id
    finally:
        await store._collection.delete_many({"session_id": session_id})
        store.close()
        from motor.motor_asyncio import AsyncIOMotorClient

        fb_client = AsyncIOMotorClient(settings.mongodb_uri)
        try:
            await fb_client["beever_atlas"].qa_feedback.delete_many({"session_id": session_id})
        finally:
            fb_client.close()


@pytest.mark.anyio
async def test_stranger_cannot_submit_feedback_on_owners_session(
    client: AsyncClient, as_stranger, owned_session: str
):
    """Issue #45 — v1 endpoint must 403 the stranger, matching v2 behaviour."""
    r = await client.post(
        "/api/channels/C_MOCK_GENERAL/ask/feedback",
        json={"session_id": owned_session, "message_id": "m1", "rating": "up"},
    )
    assert r.status_code == 403, r.text


@pytest.mark.anyio
async def test_unknown_session_returns_404(client: AsyncClient, as_owner):
    """A session_id that doesn't exist in chat_history must 404."""
    r = await client.post(
        "/api/channels/C_MOCK_GENERAL/ask/feedback",
        json={
            "session_id": f"does-not-exist-v1-{uuid.uuid4()}",
            "message_id": "m1",
            "rating": "down",
        },
    )
    assert r.status_code == 404, r.text


@pytest.mark.anyio
async def test_owner_happy_path_still_works(client: AsyncClient, as_owner, owned_session: str):
    """Owner must still be able to submit feedback on their own session."""
    r = await client.post(
        "/api/channels/C_MOCK_GENERAL/ask/feedback",
        json={
            "session_id": owned_session,
            "message_id": "m1",
            "rating": "up",
            "comment": "v1 fix works",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feedback"]["user_id"] == str(OWNER)
    assert body["feedback"]["rating"] == "up"
