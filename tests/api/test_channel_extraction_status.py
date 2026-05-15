"""Tests for ``GET /api/channels/{channel_id}/extraction-status`` (PR-B.4).

Backs the frontend's "Enriching: X of Y messages complete" progress
display that replaces the wall-of-503 banner once
``DECOUPLE_EXTRACTION`` is ON. The endpoint zero-fills missing statuses
and aggregates via a single MongoDB pipeline (the partial-filter index
on ``(extraction_status, next_attempt_at)`` keeps the query cheap).

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/extraction-worker/``
→ "Requirement: Extraction-status API endpoint".
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.server.app import app


@pytest.fixture
async def client(mock_stores):  # noqa: ARG001 — dependency wires the stores
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_extraction_status_zero_filled_when_no_messages(
    client: AsyncClient, mock_stores
) -> None:
    """Spec scenario: ``Channel has no rows yet → all statuses = 0``."""
    mock_stores.mongodb.count_channel_messages_by_status = AsyncMock(
        return_value={"pending": 0, "extracting": 0, "done": 0, "failed": 0}
    )
    resp = await client.get("/api/channels/C_MOCK_GENERAL/extraction-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["channel_id"] == "C_MOCK_GENERAL"
    assert body["counts"] == {"pending": 0, "extracting": 0, "done": 0, "failed": 0}
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_extraction_status_returns_aggregated_counts(
    client: AsyncClient, mock_stores
) -> None:
    """Spec scenario: ``Channel has mixed extraction states``."""
    mock_stores.mongodb.count_channel_messages_by_status = AsyncMock(
        return_value={"pending": 30, "extracting": 4, "done": 60, "failed": 6}
    )
    resp = await client.get("/api/channels/C_MOCK_GENERAL/extraction-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counts"] == {"pending": 30, "extracting": 4, "done": 60, "failed": 6}
    assert body["total"] == 100


@pytest.mark.asyncio
async def test_extraction_status_uses_assert_channel_access(
    client: AsyncClient, mock_stores
) -> None:
    """Endpoint MUST call ``assert_channel_access`` before reading counts.

    Verified by patching the shared dependency to record the call — the
    test suite's mock_stores grants access to all channels, so a positive
    sentinel (rather than a denial) is the correct verification shape.
    """
    mock_stores.mongodb.count_channel_messages_by_status = AsyncMock(
        return_value={"pending": 0, "extracting": 0, "done": 0, "failed": 0}
    )
    seen: list[str] = []

    async def _fake_assert_access(principal, channel_id, *args, **kwargs):
        seen.append(channel_id)

    from unittest.mock import patch

    with patch(
        "beever_atlas.api.channels.assert_channel_access",
        side_effect=_fake_assert_access,
    ):
        resp = await client.get("/api/channels/C_AUTH_CHECK/extraction-status")
    assert resp.status_code == 200
    assert seen == ["C_AUTH_CHECK"]


@pytest.mark.asyncio
async def test_extraction_status_total_is_sum_of_counts(client: AsyncClient, mock_stores) -> None:
    """``total`` MUST be the sum of all status counts even when one is high."""
    mock_stores.mongodb.count_channel_messages_by_status = AsyncMock(
        return_value={"pending": 1234, "extracting": 0, "done": 0, "failed": 0}
    )
    resp = await client.get("/api/channels/C_MOCK_GENERAL/extraction-status")
    body = resp.json()
    assert body["total"] == 1234
