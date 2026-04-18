"""Task 5a.5: contract test for job-ownership attribution.

Verifies that a dashboard-created sync_jobs record is stamped with its
owner's principal_id so that:

1. The owning user can read it via ``capabilities.jobs.get_job_status``.
2. A different user (and, more importantly, an MCP principal) sees
   ``JobNotFound`` — never leaking that the job exists.

Uses in-memory mocks rather than touching real MongoDB so it runs in CI
without fixtures. The real ``get_job_status`` reads from
``stores.mongodb._sync_jobs.find_one({"id": job_id})`` and transforms the
raw document via ``_build_status``; this mock matches that shape exactly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import beever_atlas.stores as stores_mod
from beever_atlas.capabilities import jobs as jobs_mod
from beever_atlas.capabilities.errors import JobNotFound


def _make_record(*, job_id: str, owner: str | None, status: str = "running") -> dict:
    """Build a raw sync_jobs MongoDB document with the schema the real
    collection uses (key is ``id``, not ``job_id``)."""
    return {
        "id": job_id,
        "kind": "sync",
        "status": status,
        "owner_principal_id": owner,
        "channel_id": "ch-a",
        "processed_messages": 10,
        "total_messages": 20,
        "current_stage": "ingesting",
        "started_at": None,
        "completed_at": None,
        "errors": None,
    }


def _patch_stores(monkeypatch, records: list[dict]):
    by_id = {rec["id"]: rec for rec in records}

    async def _fake_find_one(query):
        return by_id.get(query.get("id"))

    fake_sync_jobs = SimpleNamespace(find_one=_fake_find_one)
    fake_mongodb = SimpleNamespace(_sync_jobs=fake_sync_jobs)
    fake_stores = SimpleNamespace(mongodb=fake_mongodb)

    monkeypatch.setattr(stores_mod, "get_stores", lambda: fake_stores)


# ---------------------------------------------------------------------------
# Contract: principal owning the job can read its status.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_principal_can_read_job(monkeypatch):
    owner = "user:abc123def456ab01"
    job_id = "job-aaaaaaaa"
    _patch_stores(monkeypatch, [_make_record(job_id=job_id, owner=owner)])

    result = await jobs_mod.get_job_status(owner, job_id)
    assert result["job_id"] == job_id
    assert result["status"] == "running"
    assert result["target"] == {"channel_id": "ch-a"}


# ---------------------------------------------------------------------------
# Contract: different user sees JobNotFound — never the real status.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_user_sees_job_not_found(monkeypatch):
    job_id = "job-bbbbbbbb"
    _patch_stores(
        monkeypatch,
        [_make_record(job_id=job_id, owner="user:owner_principal_aaa", status="done")],
    )
    other_user = "user:different_user_bbb"
    with pytest.raises(JobNotFound):
        await jobs_mod.get_job_status(other_user, job_id)


# ---------------------------------------------------------------------------
# Contract: MCP principals cannot read dashboard-owned jobs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_principal_cannot_read_dashboard_job(monkeypatch):
    job_id = "job-cccccccc"
    _patch_stores(
        monkeypatch,
        [_make_record(job_id=job_id, owner="user:dashboard_user_aaa")],
    )
    mcp_principal = "mcp:agent_principal_bb"
    with pytest.raises(JobNotFound):
        await jobs_mod.get_job_status(mcp_principal, job_id)


# ---------------------------------------------------------------------------
# Contract: legacy:shared rows are invisible to MCP principals but visible
# to user principals (legacy single-tenant fallback).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_principal_cannot_read_legacy_shared_job(monkeypatch):
    job_id = "job-dddddddd"
    _patch_stores(
        monkeypatch,
        [_make_record(job_id=job_id, owner="legacy:shared", status="done")],
    )
    mcp_principal = "mcp:agent_principal_bb"
    with pytest.raises(JobNotFound):
        await jobs_mod.get_job_status(mcp_principal, job_id)


@pytest.mark.asyncio
async def test_user_principal_can_read_legacy_shared_job(monkeypatch):
    """User principals retain the legacy fallback for pre-migration rows."""
    job_id = "job-eeeeeeee"
    _patch_stores(
        monkeypatch,
        [_make_record(job_id=job_id, owner="legacy:shared", status="done")],
    )
    user_principal = "user:any_user_principal"
    result = await jobs_mod.get_job_status(user_principal, job_id)
    assert result["job_id"] == job_id


# ---------------------------------------------------------------------------
# Contract: non-existent job also raises JobNotFound (no disclosure).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_job_raises_job_not_found(monkeypatch):
    _patch_stores(monkeypatch, [])
    with pytest.raises(JobNotFound):
        await jobs_mod.get_job_status("user:any_principal", "nonexistent")
