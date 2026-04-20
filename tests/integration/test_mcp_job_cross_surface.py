"""Phase 5b task 5.6: Cross-surface job-ownership consistency tests.

Verifies that job records created by one principal are NOT visible to other
principals, regardless of whether the job was created via the MCP surface or
the dashboard surface.

All tests mock ``sync_runner.start_sync`` and ``stores.mongodb._sync_jobs``
so no real MongoDB / Weaviate / Neo4j infrastructure is required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.capabilities.errors import JobNotFound

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CHANNEL_ID = "ch-test-xsurf"
_JOB_ID_MCP = "job-mcp-001"
_JOB_ID_DASH = "job-dash-001"

_MCP_PRINCIPAL = "mcp:abc123hash"
_DASH_PRINCIPAL = "user:dashboard-user"
_OTHER_MCP_PRINCIPAL = "mcp:other456hash"


def _make_job_doc(job_id: str, owner: str, channel_id: str = _CHANNEL_ID) -> dict:
    """Dict matching the SyncJob pydantic model.

    Fields omitted rely on SyncJob defaults (errors=[], started_at=auto).
    """
    return {
        "id": job_id,
        "channel_id": channel_id,
        "status": "queued",
        "kind": "sync",
        "owner_principal_id": owner,
        "processed_messages": 0,
        "total_messages": 0,
        "current_stage": None,
    }


def _mock_stores(job_doc: dict | None):
    """Build a mock stores object whose ``mongodb.get_sync_job`` returns a
    ``SyncJob`` built from *job_doc*, or ``None`` if *job_doc* is ``None``."""
    from beever_atlas.models.persistence import SyncJob

    returned = SyncJob(**job_doc) if job_doc is not None else None

    mongodb_mock = MagicMock()
    mongodb_mock.get_sync_job = AsyncMock(return_value=returned)

    stores_mock = MagicMock()
    stores_mock.mongodb = mongodb_mock
    return stores_mock


# ---------------------------------------------------------------------------
# Scenario 1: Dashboard-created job is invisible to MCP principal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_job_invisible_to_mcp_principal():
    """A job created by a dashboard (user:) principal returns JobNotFound for MCP callers.

    Dashboard user creates job J1 → owner_principal_id = 'user:dashboard-user'.
    MCP principal queries J1 via capabilities.jobs.get_job_status → JobNotFound.
    """
    from beever_atlas.capabilities import jobs as jobs_cap

    job_doc = _make_job_doc(_JOB_ID_DASH, owner=_DASH_PRINCIPAL)
    stores_mock = _mock_stores(job_doc)

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        with pytest.raises(JobNotFound) as exc_info:
            await jobs_cap.get_job_status(_MCP_PRINCIPAL, _JOB_ID_DASH)

    assert exc_info.value.job_id == _JOB_ID_DASH


# ---------------------------------------------------------------------------
# Scenario 2: MCP-created job is invisible to dashboard user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_job_invisible_to_dashboard_principal():
    """A job owned by an MCP principal returns JobNotFound for dashboard users.

    MCP principal creates job J2 → owner_principal_id = 'mcp:abc123hash'.
    Dashboard user queries J2 via capabilities.jobs.get_job_status → JobNotFound.

    NOTE: The current get_job_status implementation allows non-MCP principals
    to read legacy/unowned rows but NOT explicitly MCP-owned rows. A job with
    owner_principal_id='mcp:abc123hash' is explicitly owned, so a dashboard
    user (user:*) querying it gets JobNotFound because the ownership check
    fails: owner != principal_id and owner is not in (None, 'legacy:shared').
    """
    from beever_atlas.capabilities import jobs as jobs_cap

    job_doc = _make_job_doc(_JOB_ID_MCP, owner=_MCP_PRINCIPAL)
    stores_mock = _mock_stores(job_doc)

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        with pytest.raises(JobNotFound) as exc_info:
            await jobs_cap.get_job_status(_DASH_PRINCIPAL, _JOB_ID_MCP)

    assert exc_info.value.job_id == _JOB_ID_MCP


# ---------------------------------------------------------------------------
# Scenario 3: Round-trip — MCP creates job, same MCP principal reads it OK,
#             different MCP principal gets JobNotFound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_job_round_trip_owner_can_read():
    """The owning MCP principal can read the job it created."""
    from beever_atlas.capabilities import jobs as jobs_cap

    job_doc = _make_job_doc(_JOB_ID_MCP, owner=_MCP_PRINCIPAL)
    stores_mock = _mock_stores(job_doc)

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        result = await jobs_cap.get_job_status(_MCP_PRINCIPAL, _JOB_ID_MCP)

    assert result["job_id"] == _JOB_ID_MCP
    assert result["status"] == "queued"
    assert result["target"]["channel_id"] == _CHANNEL_ID


@pytest.mark.asyncio
async def test_mcp_job_invisible_to_different_mcp_principal():
    """A different MCP principal cannot read another principal's job."""
    from beever_atlas.capabilities import jobs as jobs_cap

    job_doc = _make_job_doc(_JOB_ID_MCP, owner=_MCP_PRINCIPAL)
    stores_mock = _mock_stores(job_doc)

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        with pytest.raises(JobNotFound) as exc_info:
            await jobs_cap.get_job_status(_OTHER_MCP_PRINCIPAL, _JOB_ID_MCP)

    assert exc_info.value.job_id == _JOB_ID_MCP


# ---------------------------------------------------------------------------
# Scenario 4: Non-existent job returns JobNotFound for any principal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nonexistent_job_raises_job_not_found():
    """find_one returning None → JobNotFound for any principal."""
    from beever_atlas.capabilities import jobs as jobs_cap

    stores_mock = _mock_stores(None)  # job does not exist

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        with pytest.raises(JobNotFound):
            await jobs_cap.get_job_status(_MCP_PRINCIPAL, "job-does-not-exist")


# ---------------------------------------------------------------------------
# Scenario 5: Legacy job (owner_principal_id = 'legacy:shared') is readable
#             by non-MCP principals but NOT by MCP principals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_job_readable_by_non_mcp_principal():
    """Legacy rows (owner='legacy:shared') are readable by dashboard users."""
    from beever_atlas.capabilities import jobs as jobs_cap

    job_doc = _make_job_doc("job-legacy-001", owner="legacy:shared")
    stores_mock = _mock_stores(job_doc)

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        result = await jobs_cap.get_job_status(_DASH_PRINCIPAL, "job-legacy-001")

    assert result["job_id"] == "job-legacy-001"


@pytest.mark.asyncio
async def test_legacy_job_invisible_to_mcp_principal_multi_tenant(monkeypatch):
    """In multi-tenant mode, legacy rows (owner='legacy:shared') return
    JobNotFound for MCP principals. Fix #5 intentionally admits MCP in
    single-tenant mode — that single-tenant contract is covered in
    ``tests/capabilities/test_jobs_mcp_access.py``."""
    from beever_atlas.capabilities import jobs as jobs_cap
    from beever_atlas.infra.config import get_settings

    job_doc = _make_job_doc("job-legacy-001", owner="legacy:shared")
    stores_mock = _mock_stores(job_doc)

    get_settings.cache_clear()
    monkeypatch.setenv("BEEVER_SINGLE_TENANT", "false")
    try:
        with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
            with pytest.raises(JobNotFound):
                await jobs_cap.get_job_status(_MCP_PRINCIPAL, "job-legacy-001")
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Scenario 6: MCP tool layer translates JobNotFound → structured error payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_get_job_status_tool_returns_structured_error_on_not_found():
    """The MCP get_job_status tool returns {error: 'job_not_found'} not an exception."""
    from beever_atlas.api.mcp_server import build_mcp

    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": _MCP_PRINCIPAL}}
    ctx_mock = MagicMock()

    stores_mock = _mock_stores(None)

    with (
        patch(
            "fastmcp.server.dependencies.get_http_request",
            return_value=request_mock,
        ),
        patch(
            "beever_atlas.stores.get_stores",
            return_value=stores_mock,
        ),
    ):
        mcp = build_mcp()

        # Extract the tool fn directly
        fn = None
        for key, tool in mcp._local_provider._components.items():
            if key.startswith("tool:get_job_status@") or key == "tool:get_job_status":
                fn = tool.fn
                break
        assert fn is not None, "get_job_status tool not found in registry"

        result = await fn(job_id=_JOB_ID_MCP, ctx=ctx_mock)

    assert result["error"] == "job_not_found"
    assert result["job_id"] == _JOB_ID_MCP


@pytest.mark.asyncio
async def test_mcp_get_job_status_tool_returns_status_for_owner():
    """The MCP get_job_status tool returns the job dict when the caller owns it."""
    from beever_atlas.api.mcp_server import build_mcp

    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": _MCP_PRINCIPAL}}
    ctx_mock = MagicMock()

    job_doc = _make_job_doc(_JOB_ID_MCP, owner=_MCP_PRINCIPAL)
    stores_mock = _mock_stores(job_doc)

    with (
        patch(
            "fastmcp.server.dependencies.get_http_request",
            return_value=request_mock,
        ),
        patch(
            "beever_atlas.stores.get_stores",
            return_value=stores_mock,
        ),
    ):
        mcp = build_mcp()

        fn = None
        for key, tool in mcp._local_provider._components.items():
            if key.startswith("tool:get_job_status@") or key == "tool:get_job_status":
                fn = tool.fn
                break
        assert fn is not None

        result = await fn(job_id=_JOB_ID_MCP, ctx=ctx_mock)

    assert result["job_id"] == _JOB_ID_MCP
    assert result["status"] == "queued"
    # The status dict always has an "error" key; it is None when the job is healthy.
    assert result.get("error") is None
