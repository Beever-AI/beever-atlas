"""Tests for Fix #5: MCP principals can read their own jobs and legacy
unowned rows in single-tenant mode; multi-tenant denies legacy rows to
MCP principals.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import beever_atlas.stores as stores_mod
from beever_atlas.capabilities import jobs as jobs_mod
from beever_atlas.capabilities.errors import JobNotFound
from beever_atlas.infra.config import get_settings


def _make_record(*, job_id: str, owner: str | None) -> dict:
    return {
        "id": job_id,
        "kind": "sync",
        "status": "running",
        "owner_principal_id": owner,
        "channel_id": "ch-a",
        "processed_messages": 0,
        "total_messages": 0,
        "current_stage": "ingesting",
    }


def _patch_stores(monkeypatch, records: list[dict]) -> None:
    from beever_atlas.models.persistence import SyncJob

    by_id = {rec["id"]: SyncJob(**rec) for rec in records}

    async def _fake_get_sync_job(job_id):
        return by_id.get(job_id)

    monkeypatch.setattr(
        stores_mod,
        "get_stores",
        lambda: SimpleNamespace(mongodb=SimpleNamespace(get_sync_job=_fake_get_sync_job)),
    )


@pytest.fixture
def single_tenant(monkeypatch):
    """Force single-tenant mode with a cleared settings cache."""
    get_settings.cache_clear()
    monkeypatch.setenv("BEEVER_SINGLE_TENANT", "true")
    yield
    get_settings.cache_clear()


@pytest.fixture
def multi_tenant(monkeypatch):
    """Force multi-tenant mode with a cleared settings cache."""
    get_settings.cache_clear()
    monkeypatch.setenv("BEEVER_SINGLE_TENANT", "false")
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Explicit ownership: always allowed regardless of kind or mode.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_principal_reads_own_job(monkeypatch, single_tenant):
    mcp = "mcp:agent_abc_123456"
    _patch_stores(monkeypatch, [_make_record(job_id="j1", owner=mcp)])
    result = await jobs_mod.get_job_status(mcp, "j1")
    assert result["job_id"] == "j1"


# ---------------------------------------------------------------------------
# Single-tenant fallback: user + mcp both admitted on legacy/None rows.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_tenant_user_reads_none_owner(monkeypatch, single_tenant):
    _patch_stores(monkeypatch, [_make_record(job_id="j2", owner=None)])
    result = await jobs_mod.get_job_status("user:operator1234567890", "j2")
    assert result["job_id"] == "j2"


@pytest.mark.asyncio
async def test_single_tenant_mcp_reads_none_owner(monkeypatch, single_tenant):
    _patch_stores(monkeypatch, [_make_record(job_id="j3", owner=None)])
    result = await jobs_mod.get_job_status("mcp:agent_abc_123456", "j3")
    assert result["job_id"] == "j3"


@pytest.mark.asyncio
async def test_single_tenant_mcp_reads_legacy_shared(monkeypatch, single_tenant):
    _patch_stores(monkeypatch, [_make_record(job_id="j4", owner="legacy:shared")])
    result = await jobs_mod.get_job_status("mcp:agent_abc_123456", "j4")
    assert result["job_id"] == "j4"


# ---------------------------------------------------------------------------
# Single-tenant mode does NOT grant access to another principal's job.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_tenant_mcp_cannot_read_other_principal_job(monkeypatch, single_tenant):
    _patch_stores(monkeypatch, [_make_record(job_id="j5", owner="user:other_dashboard_user")])
    with pytest.raises(JobNotFound):
        await jobs_mod.get_job_status("mcp:agent_abc_123456", "j5")


# ---------------------------------------------------------------------------
# Multi-tenant fallback: MCP DENIED on legacy rows; user still allowed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_tenant_mcp_denied_on_legacy_row(monkeypatch, multi_tenant):
    _patch_stores(monkeypatch, [_make_record(job_id="j6", owner=None)])
    with pytest.raises(JobNotFound):
        await jobs_mod.get_job_status("mcp:agent_abc_123456", "j6")


@pytest.mark.asyncio
async def test_multi_tenant_user_allowed_on_legacy_row(monkeypatch, multi_tenant):
    """Non-MCP principals retain the original legacy fallback in multi-tenant."""
    _patch_stores(monkeypatch, [_make_record(job_id="j7", owner=None)])
    result = await jobs_mod.get_job_status("user:operator1234567890", "j7")
    assert result["job_id"] == "j7"
