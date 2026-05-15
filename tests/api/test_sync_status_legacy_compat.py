"""Backwards-compat contract for ``/sync/status`` (Phase 3 / Task 4.2.7).

Verifies that:
  * every legacy field is still present after the Phase 3 extension,
  * the new fields are additive — a Pydantic model that knows ONLY the
    legacy fields can deserialize the response without choking on the
    additions (``model_config = {"extra": "ignore"}``),
  * the legacy field semantics (``processed_messages``, ``total_messages``,
    ``current_stage``, ``current_batch``, ``total_batches``, ``status``,
    ``errors`` etc.) are unchanged.

Spec: ``openspec/changes/sync-pipeline-feedback-and-auto-wiki/specs/
sync-progress-feedback/spec.md`` → "Backwards-compatible payload shape".
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict

from beever_atlas.server.app import app


class LegacyOnlySyncStatus(BaseModel):
    """A frozen-snapshot of the pre-Phase-3 ``/sync/status`` schema.

    Old clients only know these fields. The model has
    ``extra="ignore"`` so a payload with extra new fields still
    parses cleanly — that's the contract.
    """

    model_config = ConfigDict(extra="ignore")

    state: str
    job_id: str
    total_messages: int
    parent_messages: int
    processed_messages: int
    current_batch: int
    total_batches: int
    batches_completed: int
    current_stage: str | None
    stage_timings: dict[str, Any]
    stage_details: dict[str, Any]
    batch_results: list[dict[str, Any]]
    errors: list[str]
    started_at: str | None
    completed_at: str | None
    batch_job_state: str | None
    batch_job_elapsed_seconds: float | None


def _make_job() -> SimpleNamespace:
    return SimpleNamespace(
        id="job-legacy",
        status="running",
        total_messages=200,
        parent_messages=200,
        processed_messages=120,
        current_batch=2,
        total_batches=4,
        batches_completed=2,
        current_stage="extraction",
        stage_timings={"extraction": 12.4},
        stage_details={"sub_batch_size": 25},
        batch_results=[{"batch_id": 1, "rows": 50}],
        errors=[],
        started_at=datetime.now(tz=UTC),
        completed_at=None,
        batch_job_state=None,
        batch_job_elapsed_seconds=None,
    )


@pytest.fixture
async def client(mock_stores):  # noqa: ARG001
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _stub_sync_runner(monkeypatch):
    monkeypatch.setattr(
        "beever_atlas.api.sync.get_sync_runner",
        lambda: SimpleNamespace(has_active_sync=lambda _ch: True),
    )


@pytest.fixture(autouse=True)
def _reset_singletons():
    import beever_atlas.services.extraction_worker as ew_mod
    import beever_atlas.services.auto_overview_subscriber as aov_mod

    saved_w = ew_mod._worker_instance
    saved_s = aov_mod._subscriber_instance
    ew_mod._worker_instance = None
    aov_mod._subscriber_instance = None
    try:
        yield
    finally:
        ew_mod._worker_instance = saved_w
        aov_mod._subscriber_instance = saved_s


def _wire_mongo(mock_stores, job: SimpleNamespace) -> None:
    mock_stores.mongodb.get_sync_status = AsyncMock(return_value=job)
    mock_stores.mongodb.count_channel_messages_by_status = AsyncMock(
        return_value={"pending": 50, "extracting": 30, "done": 120, "failed": 0}
    )
    mock_stores.mongodb.count_channel_messages_failure_subtypes = AsyncMock(
        return_value={"retrying": 0, "abandoned": 0}
    )

    class _NoOverview:
        async def find_one(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return None

    mock_stores.mongodb.db = {"wiki_pages": _NoOverview()}


@pytest.mark.asyncio
async def test_response_contains_all_legacy_fields(client: AsyncClient, mock_stores) -> None:
    """Every legacy field MUST be present and unchanged."""
    job = _make_job()
    _wire_mongo(mock_stores, job)
    resp = await client.get("/api/channels/C_LEGACY/sync/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    expected_legacy_fields = {
        "state",
        "job_id",
        "total_messages",
        "parent_messages",
        "processed_messages",
        "current_batch",
        "total_batches",
        "batches_completed",
        "current_stage",
        "stage_timings",
        "stage_details",
        "batch_results",
        "errors",
        "started_at",
        "completed_at",
        "batch_job_state",
        "batch_job_elapsed_seconds",
    }
    missing = expected_legacy_fields - set(body.keys())
    assert not missing, f"Legacy fields missing from response: {missing}"

    # Semantics unchanged.
    assert body["job_id"] == "job-legacy"
    assert body["total_messages"] == 200
    assert body["processed_messages"] == 120
    assert body["current_batch"] == 2
    assert body["total_batches"] == 4
    assert body["current_stage"] == "extraction"


@pytest.mark.asyncio
async def test_legacy_only_model_can_deserialize_new_payload(
    client: AsyncClient, mock_stores
) -> None:
    """A pre-Phase-3 client with ``extra='ignore'`` must parse the new
    payload without raising — the additive fields land in extras and
    are silently dropped."""
    job = _make_job()
    _wire_mongo(mock_stores, job)
    resp = await client.get("/api/channels/C_LEGACY/sync/status")
    body = resp.json()

    # Sanity: the new fields ARE present on the wire.
    assert "phases" in body
    assert "recent_events" in body
    assert "smoothed_eta_seconds" in body
    assert "retrying" in body
    assert "abandoned" in body

    # Old-only model parses cleanly.
    legacy = LegacyOnlySyncStatus.model_validate(body)
    assert legacy.job_id == "job-legacy"
    assert legacy.total_messages == 200
    assert legacy.current_batch == 2


@pytest.mark.asyncio
async def test_idle_state_response_unchanged(client: AsyncClient, mock_stores) -> None:
    """The ``no job → idle`` short-circuit must keep its minimal shape."""
    mock_stores.mongodb.get_sync_status = AsyncMock(return_value=None)
    resp = await client.get("/api/channels/C_IDLE/sync/status")
    assert resp.status_code == 200
    body = resp.json()
    # Pre-Phase-3 idle response was just ``{"state": "idle"}``.
    assert body == {"state": "idle"}
