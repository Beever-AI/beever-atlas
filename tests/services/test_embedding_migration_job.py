"""Tests for the shared re-embed migration job registry + helpers.

Covers:
  * ``spawn_reembed_job`` dedupes against an already-running task.
  * ``migration_status_snapshot`` reflects the in-process registry + the
    ``reembed_state`` checkpoint doc.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from beever_atlas.services import embedding_migration_job as job


def _make_stores(*, checkpoint: dict | None = None) -> Any:
    reembed_state = AsyncMock()
    reembed_state.find_one = AsyncMock(return_value=checkpoint)
    db = {"reembed_state": reembed_state}
    mongodb = SimpleNamespace(db=db)
    return SimpleNamespace(mongodb=mongodb)


@pytest.fixture(autouse=True)
def _reset_registry():
    job._active_migration["task"] = None
    job._active_migration["job_id"] = None
    job._active_migration["started_at"] = None
    job._active_migration["error"] = None
    yield
    job._active_migration["task"] = None
    job._active_migration["job_id"] = None
    job._active_migration["started_at"] = None
    job._active_migration["error"] = None


def test_spawn_dedupes_against_running_task(monkeypatch):
    """A second spawn while a task is in-flight returns the existing job."""
    monkeypatch.setattr(job, "get_stores", lambda: _make_stores())

    started = asyncio.Event()
    finished = asyncio.Event()

    async def slow_work():
        started.set()
        await finished.wait()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        existing_task = loop.create_task(slow_work())
        job._active_migration["task"] = existing_task
        job._active_migration["job_id"] = "EXISTING-JOB"

        job_id, status = job.spawn_reembed_job()
        assert status == "running_existing"
        assert job_id == "EXISTING-JOB"
    finally:
        finished.set()
        loop.run_until_complete(existing_task)
        loop.close()


def test_spawn_starts_new_job_when_idle(monkeypatch):
    monkeypatch.setattr(job, "get_stores", lambda: _make_stores())

    async def _run_test():
        # Stub the re-embed main so spawning doesn't run a real job.
        async def fake_main(*, stores=None):  # noqa: ANN001
            return None

        import scripts.reembed_facts as reembed_mod

        monkeypatch.setattr(reembed_mod, "main", fake_main)

        job_id, status = job.spawn_reembed_job()
        assert status == "running"
        assert isinstance(job_id, str) and job_id
        task = job._active_migration["task"]
        assert task is not None
        await task  # let the fire-and-forget task complete

    asyncio.run(_run_test())


def test_status_snapshot_idle(monkeypatch):
    monkeypatch.setattr(job, "get_stores", lambda: _make_stores(checkpoint=None))

    snap = asyncio.run(job.migration_status_snapshot())
    assert snap["running"] is False
    assert snap["job_id"] is None
    assert snap["stage"] is None
    assert snap["error"] is None


def test_status_snapshot_reflects_checkpoint_and_registry(monkeypatch):
    cp = {
        "_id": "reembed_state",
        "stage": "weaviate_embed",
        "processed": 42,
        "total": 100,
        "updated_at": "2026-05-12T00:00:00Z",
    }
    monkeypatch.setattr(job, "get_stores", lambda: _make_stores(checkpoint=cp))

    job._active_migration["job_id"] = "JOB-1"
    job._active_migration["started_at"] = "2026-05-12T00:00:00Z"

    snap = asyncio.run(job.migration_status_snapshot())
    assert snap["job_id"] == "JOB-1"
    assert snap["stage"] == "weaviate_embed"
    assert snap["processed"] == 42
    assert snap["total"] == 100
    assert snap["started_at"] == "2026-05-12T00:00:00Z"
    # No task object → not running, no finished_at.
    assert snap["running"] is False
