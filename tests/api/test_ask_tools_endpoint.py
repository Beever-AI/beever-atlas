"""Tests for GET /api/ask/tools endpoint."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.agents.tools import QA_TOOLS
from beever_atlas.agents.tools.orchestration_tools import ORCHESTRATION_TOOLS
from beever_atlas.server.app import app


def _tool_name(tool) -> str:
    return (
        getattr(tool, "__name__", None)
        or getattr(tool, "name", None)
        or getattr(getattr(tool, "func", None), "__name__", "")
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_get_tools_returns_full_catalog(client):
    resp = await client.get("/api/ask/tools")
    assert resp.status_code == 200
    data = resp.json()
    assert "tools" in data
    # Retrieval tools (QA_TOOLS) + deep-mode orchestration tools.
    assert len(data["tools"]) == len(QA_TOOLS) + len(ORCHESTRATION_TOOLS)

    returned_names = {t["name"] for t in data["tools"]}
    registry_names = {_tool_name(t) for t in QA_TOOLS} | {
        _tool_name(t) for t in ORCHESTRATION_TOOLS
    }
    assert returned_names == registry_names


@pytest.mark.anyio
async def test_all_categories_present(client):
    resp = await client.get("/api/ask/tools")
    assert resp.status_code == 200
    data = resp.json()
    categories = {t["category"] for t in data["tools"]}
    for required in ("wiki", "memory", "graph", "external", "orchestration"):
        assert required in categories
