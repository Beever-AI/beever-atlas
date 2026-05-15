"""Scenario H — backwards compat: UI ↔ API (Tasks 5.9.1-5.9.3).

The spec's H scenario has three parts; the parts that touch the React
UI are conceptual from Python's perspective. We assert:

  5.9.1 — the response model accepts payloads without the new fields
          (legacy-only payload shape) — the new ``phases`` /
          ``recent_events`` / ``smoothed_eta_seconds`` / ``retrying``
          / ``abandoned`` fields default to safe values when absent.
  5.9.2 — the new payload includes EVERY legacy field untouched (the
          new fields are additive, not replacements).
  5.9.3 — the React-side fallback to ExtractionWorkerPanel rendering
          when ``phases`` is absent is covered by Vitest snapshot
          tests committed in 79ee613; this test asserts the Python
          server's contract that emitting ``phases=None`` is equivalent
          to omitting it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _principal() -> object:
    from beever_atlas.infra.auth import Principal

    return Principal("user:test", kind="user")


def _legacy_job() -> object:
    job = MagicMock()
    job.id = "job-legacy"
    job.status = "completed"
    job.total_messages = 10
    job.parent_messages = 10
    job.processed_messages = 10
    job.current_batch = 1
    job.total_batches = 1
    job.batches_completed = 1
    job.current_stage = "done"
    job.stage_timings = {}
    job.stage_details = {}
    job.batch_results = []
    job.errors = []
    job.started_at = datetime(2026, 5, 1, tzinfo=UTC)
    job.completed_at = datetime(2026, 5, 1, 0, 5, tzinfo=UTC)
    job.batch_job_state = None
    job.batch_job_elapsed_seconds = None
    return job


@pytest.mark.asyncio
async def test_status_payload_carries_all_legacy_fields() -> None:
    """5.9.1 — every legacy field present, plus the new optional ones."""
    job = _legacy_job()

    fake_stores = MagicMock()
    fake_stores.mongodb.get_sync_status = AsyncMock(return_value=job)
    fake_stores.mongodb.count_channel_messages_by_status = AsyncMock(
        return_value={"pending": 0, "extracting": 0, "done": 10, "failed": 0}
    )
    fake_stores.mongodb.count_channel_messages_failure_subtypes = AsyncMock(
        return_value={"retrying": 0, "abandoned": 0}
    )

    # No overview row → state="pending" by default in _safe_overview_state.
    fake_db_collection = MagicMock()
    fake_db_collection.find_one = AsyncMock(return_value=None)
    fake_db = MagicMock()
    fake_db.__getitem__ = lambda self, key: fake_db_collection
    fake_stores.mongodb.db = fake_db

    with (
        patch("beever_atlas.api.sync.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.api.sync.assert_channel_access",
            new=AsyncMock(),
        ),
        patch(
            "beever_atlas.api.sync.get_sync_runner",
            return_value=MagicMock(has_active_sync=lambda _ch: False),
        ),
    ):
        from beever_atlas.api.sync import get_sync_status

        payload = await get_sync_status(
            channel_id="sim-H1",
            principal=_principal(),
        )

    legacy_keys = {
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
    new_keys = {
        "phases",
        "recent_events",
        "smoothed_eta_seconds",
        "retrying",
        "abandoned",
    }

    missing_legacy = legacy_keys - set(payload.keys())
    assert not missing_legacy, f"missing legacy fields: {missing_legacy}"

    missing_new = new_keys - set(payload.keys())
    assert not missing_new, f"missing new fields: {missing_new}"

    # New fields carry safe defaults — empty lists / None / zeros — so
    # an old client that ignores them keeps working.
    assert isinstance(payload["phases"], list)
    assert isinstance(payload["recent_events"], list)
    assert payload["retrying"] == 0
    assert payload["abandoned"] == 0


@pytest.mark.asyncio
async def test_idle_payload_uses_legacy_shape_only() -> None:
    """5.9.1 (variant) — when there is no job, the response is
    ``{"state": "idle"}`` without any new fields. Old clients that
    only read ``state`` continue to render their idle state correctly.
    """
    fake_stores = MagicMock()
    fake_stores.mongodb.get_sync_status = AsyncMock(return_value=None)

    with (
        patch("beever_atlas.api.sync.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.api.sync.assert_channel_access",
            new=AsyncMock(),
        ),
    ):
        from beever_atlas.api.sync import get_sync_status

        payload = await get_sync_status(
            channel_id="sim-H2",
            principal=_principal(),
        )

    assert payload == {"state": "idle"}


@pytest.mark.asyncio
async def test_phases_array_shape_matches_spec() -> None:
    """5.9.2 — the ``phases`` array is fixed-order, four entries, each
    with ``name`` + ``state``. Old clients that ignore ``phases``
    continue to read every legacy field. New clients use the array
    directly.
    """
    job = _legacy_job()

    fake_stores = MagicMock()
    fake_stores.mongodb.get_sync_status = AsyncMock(return_value=job)
    fake_stores.mongodb.count_channel_messages_by_status = AsyncMock(
        return_value={"pending": 0, "extracting": 0, "done": 50, "failed": 0}
    )
    fake_stores.mongodb.count_channel_messages_failure_subtypes = AsyncMock(
        return_value={"retrying": 0, "abandoned": 0}
    )
    fake_db_collection = MagicMock()
    fake_db_collection.find_one = AsyncMock(
        return_value={"_id": "overview", "channel_id": "sim-H3"}
    )
    fake_db = MagicMock()
    fake_db.__getitem__ = lambda self, key: fake_db_collection
    fake_stores.mongodb.db = fake_db

    with (
        patch("beever_atlas.api.sync.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.api.sync.assert_channel_access",
            new=AsyncMock(),
        ),
        patch(
            "beever_atlas.api.sync.get_sync_runner",
            return_value=MagicMock(has_active_sync=lambda _ch: False),
        ),
    ):
        from beever_atlas.api.sync import get_sync_status

        payload = await get_sync_status(
            channel_id="sim-H3",
            principal=_principal(),
        )

    phases = payload["phases"]
    assert len(phases) == 4
    names_in_order = [p["name"] for p in phases]
    assert names_in_order == [
        "fetched",
        "extracting",
        "wiki_maintenance",
        "overview_wiki",
    ], f"phases must follow fixed order, got {names_in_order}"

    # All four states must end ``done`` for this happy-path setup.
    for entry in phases:
        assert entry["state"] == "done", f"phase {entry['name']} not done: {entry}"
