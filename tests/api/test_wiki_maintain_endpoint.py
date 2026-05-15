"""Tests for ``POST /api/channels/{channel_id}/wiki/maintain``.

The manual-mode receiver for the "Maintain Wiki" button — drains
pages flagged dirty since the last maintenance run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.server.app import app


@pytest.fixture
async def client(mock_stores):  # noqa: ARG001
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_maintain_returns_zero_counters_when_maintainer_uninitialized(
    client: AsyncClient,
) -> None:
    """When the maintainer singleton is None, the endpoint reports a
    structured 'not initialized' response so the UI can distinguish
    "ran but had nothing to do" from "wasn't wired up". Important for
    deployments that haven't enabled the worker yet."""
    with patch(
        "beever_atlas.services.wiki_maintainer.get_wiki_maintainer",
        return_value=None,
    ):
        resp = await client.post("/api/channels/C_MOCK_GENERAL/wiki/maintain")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rewritten"] == 0
    assert body["errors"] == 0
    assert body.get("reason") == "maintainer_not_initialized"


@pytest.mark.asyncio
async def test_maintain_invokes_maintainer_now_with_channel_and_lang(
    client: AsyncClient,
) -> None:
    """Happy path: the endpoint calls maintainer.maintain_now with the
    channel id and resolved target lang, then returns its counters."""
    fake_maintainer = AsyncMock()
    fake_maintainer.maintain_now = AsyncMock(return_value={"rewritten": 7, "errors": 0})
    with patch(
        "beever_atlas.services.wiki_maintainer.get_wiki_maintainer",
        return_value=fake_maintainer,
    ):
        resp = await client.post("/api/channels/C_MOCK_GENERAL/wiki/maintain")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rewritten"] == 7
    assert body["errors"] == 0
    fake_maintainer.maintain_now.assert_awaited_once()


@pytest.mark.asyncio
async def test_maintain_reports_errors_in_counters_not_http_status(
    client: AsyncClient,
) -> None:
    """A degraded run (some pages failed to rewrite) returns 200 with
    errors > 0 — the operator gets a partial-progress report rather
    than a 500 that hides the partial work."""
    fake_maintainer = AsyncMock()
    fake_maintainer.maintain_now = AsyncMock(return_value={"rewritten": 3, "errors": 2})
    with patch(
        "beever_atlas.services.wiki_maintainer.get_wiki_maintainer",
        return_value=fake_maintainer,
    ):
        resp = await client.post("/api/channels/C_MOCK_GENERAL/wiki/maintain")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rewritten"] == 3
    assert body["errors"] == 2
