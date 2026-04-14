"""Integration tests for Ask share endpoints (Phase 2).

Exercises the full stack against a real MongoDB (the test fixtures already
route `BEEVER_CHAT_HISTORY_DB=beever_atlas_test`). Covers ownership checks,
rotate semantics, re-snapshot, visibility tiers, scrubber tripwire, and
concurrent rotation.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from beever_atlas.infra.auth import require_user
from beever_atlas.infra.config import get_settings
from beever_atlas.server.app import app
from beever_atlas.services.share_snapshot import build_share_snapshot
from beever_atlas.stores.chat_history_store import ChatHistoryStore
from beever_atlas.services.share_store import ShareStore

OWNER_ID = "user:owner1"
STRANGER_ID = "user:stranger"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _prime_api_keys_cache(_auth_bypass):
    """The `BEEVER_API_KEYS=test-key` env set by _auth_bypass only takes
    effect if the lru_cached `get_settings()` sees it. Clear the cache so
    `require_user_optional` picks up the test key for auth/owner tiers.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def as_owner(_auth_bypass):
    """Override auth to act as OWNER_ID (runs AFTER the autouse _auth_bypass)."""
    app.dependency_overrides[require_user] = lambda: OWNER_ID
    yield OWNER_ID


@pytest.fixture
def as_stranger(_auth_bypass):
    app.dependency_overrides[require_user] = lambda: STRANGER_ID
    yield STRANGER_ID


@pytest.fixture
async def seeded_session():
    """Insert a chat_history session owned by OWNER_ID and clean up after."""
    session_id = f"test-share-{uuid.uuid4()}"
    settings = get_settings()
    store = ChatHistoryStore(settings.mongodb_uri)
    await store.startup()
    try:
        # Write directly — ChatHistoryStore.create_session_v2 doesn't set title.
        await store._collection.insert_one(
            {
                "session_id": session_id,
                "user_id": OWNER_ID,
                "title": "My test chat",
                "messages": [
                    {
                        "role": "user",
                        "content": "hello",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "user_id": "LEAK",
                        "embedding": [0.1, 0.2],
                        "raw_prompt": "leak",
                    },
                    {
                        "role": "assistant",
                        "content": "hi there",
                        "timestamp": datetime.now(UTC).isoformat(),
                        "access_token": "LEAK",
                        "source_id": "LEAK",
                    },
                ],
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        )
        yield session_id
    finally:
        # Cleanup chat_history + any share docs
        await store._collection.delete_many({"session_id": session_id})
        store.close()
        s2 = ShareStore(settings.mongodb_uri)
        try:
            await s2._collection.delete_many({"source_session_id": session_id})
        finally:
            s2.close()


# ---------------------------------------------------------------------------
# Scrubber unit tests
# ---------------------------------------------------------------------------

class TestScrubber:
    def test_allowlist_only(self):
        messages = [
            {
                "role": "user",
                "content": "hi",
                "timestamp": "2024-01-01T00:00:00+00:00",
                "user_id": "LEAK",
                "_id": "LEAK",
                "source_id": "LEAK",
                "embedding": [0.1],
                "raw_prompt": "LEAK",
                "access_token": "LEAK",
                "api_key": "LEAK",
            }
        ]
        out = build_share_snapshot(messages)
        assert len(out) == 1
        assert set(out[0].keys()) == {"role", "content", "created_at"}
        assert out[0]["role"] == "user"
        assert out[0]["content"] == "hi"
        assert out[0]["created_at"] == "2024-01-01T00:00:00+00:00"

    def test_regex_tripwire(self):
        """No field name in scrubbed output may match the forbidden-regex."""
        forbidden = re.compile(r"^(embedding|raw_prompt|.*_token|.*_secret|.*_key)$")
        messages = [
            {
                "role": "assistant",
                "content": "ok",
                "timestamp": "2024-01-01T00:00:00+00:00",
                "embedding": [1],
                "raw_prompt": "x",
                "access_token": "x",
                "api_key": "x",
                "client_secret": "x",
            }
        ]

        def walk(node, bad: list[str]) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if forbidden.match(k):
                        bad.append(k)
                    walk(v, bad)
            elif isinstance(node, list):
                for item in node:
                    walk(item, bad)

        bad: list[str] = []
        walk(build_share_snapshot(messages), bad)
        assert bad == [], f"Forbidden field names leaked: {bad}"


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------

class TestShareCreate:
    @pytest.mark.anyio
    async def test_owner_creates_default_owner_visibility(
        self, client: AsyncClient, as_owner, seeded_session: str
    ):
        resp = await client.post(f"/api/ask/sessions/{seeded_session}/share")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["visibility"] == "owner"
        assert body["share_token"]
        assert body["url"].startswith("/ask/shared/")

    @pytest.mark.anyio
    async def test_stranger_forbidden(
        self, client: AsyncClient, as_stranger, seeded_session: str
    ):
        resp = await client.post(f"/api/ask/sessions/{seeded_session}/share")
        assert resp.status_code == 403, resp.text

    @pytest.mark.anyio
    async def test_post_without_rotate_returns_existing(
        self, client: AsyncClient, as_owner, seeded_session: str
    ):
        r1 = await client.post(f"/api/ask/sessions/{seeded_session}/share")
        r2 = await client.post(f"/api/ask/sessions/{seeded_session}/share")
        assert r1.status_code == r2.status_code == 200
        assert r1.json()["share_token"] == r2.json()["share_token"]

    @pytest.mark.anyio
    async def test_rotate_invalidates_old_token(
        self, client: AsyncClient, as_owner, seeded_session: str
    ):
        r1 = await client.post(f"/api/ask/sessions/{seeded_session}/share")
        old = r1.json()["share_token"]
        r2 = await client.post(
            f"/api/ask/sessions/{seeded_session}/share?rotate=true"
        )
        new = r2.json()["share_token"]
        assert old != new
        # Old token is gone.
        resp = await client.get(f"/api/ask/shared/{old}")
        assert resp.status_code == 404


class TestShareLifecycle:
    @pytest.mark.anyio
    async def test_put_resnapshot_stable_token(
        self, client: AsyncClient, as_owner, seeded_session: str
    ):
        r1 = await client.post(f"/api/ask/sessions/{seeded_session}/share")
        token = r1.json()["share_token"]
        updated1 = r1.json()["updated_at"]

        await asyncio.sleep(0.01)
        r2 = await client.put(f"/api/ask/sessions/{seeded_session}/share")
        assert r2.status_code == 200
        assert r2.json()["share_token"] == token
        assert r2.json()["updated_at"] != updated1

    @pytest.mark.anyio
    async def test_patch_visibility(
        self, client: AsyncClient, as_owner, seeded_session: str
    ):
        await client.post(f"/api/ask/sessions/{seeded_session}/share")
        r = await client.patch(
            f"/api/ask/sessions/{seeded_session}/share/visibility",
            json={"visibility": "auth"},
        )
        assert r.status_code == 200
        assert r.json()["visibility"] == "auth"

    @pytest.mark.anyio
    async def test_delete_then_get_404(
        self, client: AsyncClient, as_owner, seeded_session: str
    ):
        r1 = await client.post(f"/api/ask/sessions/{seeded_session}/share")
        token = r1.json()["share_token"]
        d = await client.delete(f"/api/ask/sessions/{seeded_session}/share")
        assert d.status_code == 204
        g = await client.get(f"/api/ask/shared/{token}")
        assert g.status_code == 404


class TestShareGetVisibility:
    @pytest.mark.anyio
    async def test_public_unauth_ok_headers_present(
        self, client: AsyncClient, as_owner, seeded_session: str
    ):
        await client.post(f"/api/ask/sessions/{seeded_session}/share")
        await client.patch(
            f"/api/ask/sessions/{seeded_session}/share/visibility",
            json={"visibility": "public"},
        )
        # Drop auth override to simulate unauth caller; public route bypasses
        # the global require_user dep via the public_router mount.
        r = await client.get(
            f"/api/ask/sessions/{seeded_session}/share"
            # We have to fetch the token via a fresh POST response
        )
        # The above endpoint doesn't exist as GET; fetch via repeat POST.
        r = await client.post(f"/api/ask/sessions/{seeded_session}/share")
        token = r.json()["share_token"]

        # Remove auth entirely for the shared endpoint call (public tier
        # should accept). Our dep-override doesn't affect the public router
        # because it's not in its dependency chain.
        g = await client.get(f"/api/ask/shared/{token}")
        assert g.status_code == 200, g.text
        assert g.headers.get("referrer-policy") == "no-referrer"
        assert "noindex" in g.headers.get("x-robots-tag", "")
        body = g.json()
        assert body["visibility"] == "public"
        assert body["owner_user_id"] == OWNER_ID
        # Scrubbed payload: messages have only allowlisted keys.
        for m in body["messages"]:
            assert set(m.keys()) == {"role", "content", "created_at"}

    @pytest.mark.anyio
    async def test_auth_tier_requires_any_bearer(
        self, client: AsyncClient, as_owner, seeded_session: str
    ):
        await client.post(f"/api/ask/sessions/{seeded_session}/share")
        await client.patch(
            f"/api/ask/sessions/{seeded_session}/share/visibility",
            json={"visibility": "auth"},
        )
        r = await client.post(f"/api/ask/sessions/{seeded_session}/share")
        token = r.json()["share_token"]

        # No bearer → 401.
        g1 = await client.get(f"/api/ask/shared/{token}")
        assert g1.status_code == 401
        # With any valid bearer → 200. `_auth_bypass` fixture has set
        # BEEVER_API_KEYS=test-key so "test-key" is accepted by
        # require_user_optional.
        g2 = await client.get(
            f"/api/ask/shared/{token}",
            headers={"Authorization": "Bearer test-key"},
        )
        assert g2.status_code == 200

    @pytest.mark.anyio
    async def test_owner_tier_rejects_wrong_user(
        self, client: AsyncClient, as_owner, seeded_session: str
    ):
        r = await client.post(f"/api/ask/sessions/{seeded_session}/share")
        token = r.json()["share_token"]
        # No bearer → 401.
        g1 = await client.get(f"/api/ask/shared/{token}")
        assert g1.status_code == 401
        # Wrong user (test-key maps to "user:test-k" — not OWNER_ID "user:owner1").
        g2 = await client.get(
            f"/api/ask/shared/{token}",
            headers={"Authorization": "Bearer test-key"},
        )
        assert g2.status_code == 403


class TestConcurrentRotation:
    @pytest.mark.anyio
    async def test_ten_parallel_rotations_yield_one_survivor(
        self, client: AsyncClient, as_owner, seeded_session: str
    ):
        # Seed the initial share.
        r0 = await client.post(f"/api/ask/sessions/{seeded_session}/share")
        assert r0.status_code == 200

        async def one() -> str | None:
            resp = await client.post(
                f"/api/ask/sessions/{seeded_session}/share?rotate=true"
            )
            if resp.status_code == 200:
                return resp.json()["share_token"]
            return None

        results = await asyncio.gather(*[one() for _ in range(10)])
        ok_tokens = [t for t in results if t]
        assert len(ok_tokens) == 10, "All rotations should have responded"
        unique_tokens = set(ok_tokens)
        # Each `find_one_and_update` emits a unique new token → 10 distinct values.
        assert len(unique_tokens) == 10

        # Only the final DB state's token is live; probe each one.
        alive = 0
        for t in unique_tokens:
            g = await client.get(
                f"/api/ask/shared/{t}",
                headers={"Authorization": "Bearer test-key"},
            )
            if g.status_code != 404:
                alive += 1
        assert alive == 1, f"Expected exactly one live token, got {alive}"
