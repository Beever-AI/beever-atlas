"""Tests for ``GET /api/channels/{channel_id}/extraction-failures``.

Spec: ``openspec/changes/oss-redesign-production-wiring/specs/wiki-lint-and-tensions/``
→ "Requirement: Failed-extraction-batch viewer endpoint".

Backs the wiki UI's ``FailedBatchPanel`` drill-down. The endpoint MUST:
  - Return paginated rows with ``message_id``, ``next_attempt_at``,
    ``attempt_count``, and a server-truncated ``last_error`` (max 500
    chars, stack traces stripped).
  - Cap ``limit`` at 200 so a single request cannot drain the whole table.
  - Echo a ``next_cursor`` when more rows remain, ``null`` otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
async def test_failures_empty_returns_empty_items(client: AsyncClient, mock_stores) -> None:
    """Spec scenario: ``Channel with no failures``."""
    mock_stores.mongodb.list_failed_channel_messages = AsyncMock(return_value=([], None))
    resp = await client.get("/api/channels/C_MOCK_GENERAL/extraction-failures")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"items": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_failures_returns_rows_with_paging(client: AsyncClient, mock_stores) -> None:
    """Spec scenario: ``Channel with failures``."""
    rows = [
        {
            "message_id": f"msg-{i}",
            "next_attempt_at": datetime(2026, 5, 1, 12, i, tzinfo=UTC),
            "attempt_count": i,
            "last_error": f"ServerError: HTTP 503 attempt {i}",
        }
        for i in range(10)
    ]
    mock_stores.mongodb.list_failed_channel_messages = AsyncMock(return_value=(rows, "msg-10"))
    resp = await client.get("/api/channels/C_MOCK_GENERAL/extraction-failures?limit=10")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 10
    assert body["next_cursor"] == "msg-10"
    first = body["items"][0]
    assert first["message_id"] == "msg-0"
    assert first["attempt_count"] == 0
    # Datetime serialized as ISO string
    assert first["next_attempt_at"].startswith("2026-05-01T12:00")


@pytest.mark.asyncio
async def test_failures_strips_stack_traces_from_last_error(
    client: AsyncClient, mock_stores
) -> None:
    """Spec scenario: ``Stack trace stripped from last_error``."""
    raw_traceback = (
        "Traceback (most recent call last):\n"
        "  File 'fact_extractor.py', line 100, in extract\n"
        "    result = await llm.generate(prompt)\n"
        "  File 'provider.py', line 50, in generate\n"
        "    raise ServerError('HTTP 503 — provider unavailable')\n"
        "ServerError: HTTP 503 — provider unavailable"
    )
    mock_stores.mongodb.list_failed_channel_messages = AsyncMock(
        return_value=(
            [
                {
                    "message_id": "msg-1",
                    "next_attempt_at": None,
                    "attempt_count": 3,
                    "last_error": raw_traceback,
                }
            ],
            None,
        )
    )
    resp = await client.get("/api/channels/C_MOCK_GENERAL/extraction-failures")
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    # Stack frames stripped — only the final ``ExcClass: msg`` line survives.
    assert "Traceback" not in item["last_error"]
    assert "fact_extractor.py" not in item["last_error"]
    assert "ServerError" in item["last_error"]
    # Truncated to ≤ 500 chars
    assert len(item["last_error"]) <= 500


@pytest.mark.asyncio
async def test_failures_caps_limit_at_200(client: AsyncClient, mock_stores) -> None:
    """Limit param cannot drain the whole failure table in one call."""
    seen_limits: list[int] = []

    async def _list(channel_id, *, cursor=None, limit=50):  # noqa: ARG001
        seen_limits.append(limit)
        return [], None

    mock_stores.mongodb.list_failed_channel_messages = AsyncMock(side_effect=_list)
    resp = await client.get("/api/channels/C_MOCK_GENERAL/extraction-failures?limit=10000")
    assert resp.status_code == 200
    assert seen_limits == [200]
