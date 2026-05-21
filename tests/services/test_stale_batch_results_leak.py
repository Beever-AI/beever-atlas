"""Regression tests for the stale-``batch_results`` leak (sync chip strip
showing DONE batches a fraction of a second after triggering a fresh
sync).

Root cause (recap): The previous sync's ``sync_jobs`` row persists with
its ``batch_results`` array until the new sync's row lands (the runner
inserts it from a background task). During that window, the legacy
``get_sync_status`` query returned the most-recent row sorted by
``started_at``, which is still the *previous* run. The frontend then
ingested those batch_results into ``knownDoneBatchNums`` /
``stickyResultsRef`` and rendered Batches 1-4 as DONE before any new
work had been done.

These tests pin the post-fix contract:

  * ``get_sync_status`` prefers a ``status="running"`` row over the
    most-recent row regardless of ``started_at`` ordering.
  * The fallback path still returns the latest row when no running
    sync exists, so legacy cooldown callers keep working.
  * ``create_sync_job`` marks any prior ``status="running"`` row as
    ``"orphaned"`` and clears its ``batch_results`` + reset
    ``batches_completed`` — belt-and-suspenders for the brief window
    between insert ordering and runner stamping.

The store is exercised via the same ``__new__`` bypass + in-memory
collection stub used by ``test_mongo_batch_sync_state.py`` so the tests
run without a live MongoDB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from beever_atlas.stores.mongodb_store import MongoDBStore


class _SyncJobsCollectionStub:
    """In-memory stand-in for the ``_sync_jobs`` motor collection.

    Supports the subset of operations exercised by ``get_sync_status``
    and ``create_sync_job``:
      * ``find_one(filter, sort=[...])`` — emulates server-side filter
        and (single-field) sort; returns a deep-ish dict copy so the
        store's ``.pop("_id")`` mutation doesn't leak back.
      * ``update_many(filter, update)`` — applies ``$set`` to matching
        docs.
      * ``insert_one(doc)`` — appends a dict copy.
    """

    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self._docs: list[dict[str, Any]] = [dict(d) for d in (docs or [])]

    def _matches(self, doc: dict[str, Any], filt: dict[str, Any]) -> bool:
        return all(doc.get(k) == v for k, v in filt.items())

    async def find_one(
        self,
        filt: dict[str, Any],
        sort: list[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None:
        matches = [d for d in self._docs if self._matches(d, filt)]
        if sort:
            key, direction = sort[0]
            matches.sort(
                key=lambda d: d.get(key) or "",
                reverse=(direction == -1),
            )
        if not matches:
            return None
        return dict(matches[0])

    async def update_many(self, filt: dict[str, Any], update: dict[str, Any]) -> None:
        set_ops = update.get("$set", {})
        for d in self._docs:
            if self._matches(d, filt):
                d.update(set_ops)

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self._docs.append(dict(doc))


def _make_sync_job_doc(
    *,
    job_id: str,
    channel_id: str,
    status: str,
    started_at: datetime,
    batch_results: list[dict[str, Any]] | None = None,
    batches_completed: int = 0,
    kind: str = "sync",
) -> dict[str, Any]:
    """Build a ``sync_jobs`` doc shaped like ``SyncJob.model_dump()``.

    Mirrors the field set in ``models/persistence.py:22`` (SyncJob).
    Keep in sync if SyncJob gains required fields.
    """
    return {
        "id": job_id,
        "channel_id": channel_id,
        "status": status,
        "sync_type": "full",
        "total_messages": 0,
        "parent_messages": 0,
        "processed_messages": 0,
        "current_batch": 0,
        "total_batches": 0,
        "batches_completed": batches_completed,
        "current_stage": None,
        "batch_size": 10,
        "errors": [],
        "batch_results": list(batch_results or []),
        "stage_timings": {},
        "stage_details": {},
        "started_at": started_at,
        "completed_at": None,
        "batch_job_state": None,
        "batch_job_elapsed_seconds": None,
        "version": 0,
        "owner_principal_id": None,
        "kind": kind,
    }


def _store_with_jobs(docs: list[dict[str, Any]]) -> tuple[MongoDBStore, _SyncJobsCollectionStub]:
    """Build a ``MongoDBStore`` whose ``_sync_jobs`` collection is the
    in-memory stub. Bypass ``__init__`` to skip motor client construction."""
    coll = _SyncJobsCollectionStub(docs)
    store = MongoDBStore.__new__(MongoDBStore)
    store._sync_jobs = coll  # type: ignore[attr-defined]
    return store, coll


# ── Layer A tests ───────────────────────────────────────────────────────


async def test_get_sync_status_prefers_running_over_completed() -> None:
    """The completed row has earlier-started + populated batch_results.
    The running row is newer but empty. ``get_sync_status`` MUST return
    the running row — without the status filter the completed row wins
    on ``started_at`` ordering and pollutes the chip strip."""
    channel = "C_DEMO"
    now = datetime.now(tz=UTC)
    completed = _make_sync_job_doc(
        job_id="job-old",
        channel_id=channel,
        status="completed",
        started_at=now - timedelta(minutes=5),
        batch_results=[
            {"batch_num": 1, "facts_count": 10},
            {"batch_num": 2, "facts_count": 12},
            {"batch_num": 3, "facts_count": 11},
        ],
        batches_completed=3,
    )
    running = _make_sync_job_doc(
        job_id="job-new",
        channel_id=channel,
        status="running",
        started_at=now,
        batch_results=[],
        batches_completed=0,
    )
    # Insert completed first so its index would otherwise come up under
    # a plain "find newest" — but the running row must still win.
    store, _ = _store_with_jobs([completed, running])

    result = await store.get_sync_status(channel)

    assert result is not None
    assert result.id == "job-new"
    assert result.status == "running"
    assert result.batch_results == []
    assert result.batches_completed == 0


async def test_get_sync_status_falls_back_to_latest_when_no_running() -> None:
    """No running row exists — the legacy contract (return most-recent
    row regardless of status) still holds so cooldown checks in
    ``capabilities.sync`` keep working."""
    channel = "C_HIST"
    now = datetime.now(tz=UTC)
    older_completed = _make_sync_job_doc(
        job_id="job-older",
        channel_id=channel,
        status="completed",
        started_at=now - timedelta(hours=1),
    )
    newer_completed = _make_sync_job_doc(
        job_id="job-newer",
        channel_id=channel,
        status="completed",
        started_at=now - timedelta(minutes=2),
    )
    store, _ = _store_with_jobs([older_completed, newer_completed])

    result = await store.get_sync_status(channel)

    assert result is not None
    assert result.id == "job-newer"


async def test_get_sync_status_returns_none_when_no_rows() -> None:
    store, _ = _store_with_jobs([])
    assert await store.get_sync_status("C_EMPTY") is None


async def test_get_sync_status_fallback_ignores_worker_extraction() -> None:
    """The fallback must skip synthetic ``worker_extraction`` rows.

    The ExtractionWorker never finalizes its ``worker:<channel>:<ts>`` row to
    ``completed``; a stuck ``running`` one is newer than the real sync job. If
    the fallback returned it, ``/sync/status`` would report a perpetual sync
    and the sidebar "Syncing now…" dot would never clear.
    """
    channel = "C_WORKER"
    now = datetime.now(tz=UTC)
    real_sync = _make_sync_job_doc(
        job_id="sync-done",
        channel_id=channel,
        status="completed",
        started_at=now - timedelta(minutes=5),
        kind="sync",
    )
    stuck_worker = _make_sync_job_doc(
        job_id="worker:C_WORKER:123",
        channel_id=channel,
        status="running",  # never finalized
        started_at=now,  # newer than the real sync
        kind="worker_extraction",
    )
    store, _ = _store_with_jobs([real_sync, stuck_worker])

    result = await store.get_sync_status(channel)

    assert result is not None
    assert result.id == "sync-done"  # the real sync, not the stuck worker row
    assert result.status == "completed"


async def test_get_sync_status_none_when_only_worker_extraction() -> None:
    """A lone stuck ``worker_extraction`` row must read as idle (None), not
    as an active sync."""
    channel = "C_ONLY_WORKER"
    store, _ = _store_with_jobs(
        [
            _make_sync_job_doc(
                job_id="worker:C_ONLY_WORKER:999",
                channel_id=channel,
                status="running",
                started_at=datetime.now(tz=UTC),
                kind="worker_extraction",
            )
        ]
    )
    assert await store.get_sync_status(channel) is None


# ── Layer B tests ───────────────────────────────────────────────────────


async def test_create_sync_job_orphans_prior_running_row() -> None:
    """A new ``create_sync_job`` MUST mark any prior running row for the
    same channel/kind as ``orphaned`` and clear its ``batch_results`` so
    a stale read of ``/sync/status`` during the create window can't
    leak the previous run's done chips."""
    channel = "C_DEMO"
    prior_running = _make_sync_job_doc(
        job_id="job-prior",
        channel_id=channel,
        status="running",
        started_at=datetime.now(tz=UTC) - timedelta(minutes=10),
        batch_results=[{"batch_num": 1, "facts_count": 10}, {"batch_num": 2, "facts_count": 12}],
        batches_completed=2,
    )
    store, coll = _store_with_jobs([prior_running])

    new_job = await store.create_sync_job(
        channel_id=channel,
        sync_type="full",
        total_messages=0,
    )

    # The new row is inserted.
    assert new_job.channel_id == channel
    assert new_job.status == "running"
    # The prior row has been marked orphaned and cleared.
    prior_after = await coll.find_one({"id": "job-prior"})
    assert prior_after is not None
    assert prior_after["status"] == "orphaned"
    assert prior_after["batch_results"] == []
    assert prior_after["batches_completed"] == 0


async def test_create_sync_job_does_not_touch_completed_rows() -> None:
    """Only ``running`` rows for the same channel/kind get orphaned.
    Completed/failed rows MUST stay untouched so history is preserved."""
    channel = "C_DEMO"
    completed = _make_sync_job_doc(
        job_id="job-completed",
        channel_id=channel,
        status="completed",
        started_at=datetime.now(tz=UTC) - timedelta(hours=1),
        batch_results=[{"batch_num": 1, "facts_count": 5}],
        batches_completed=1,
    )
    store, coll = _store_with_jobs([completed])

    await store.create_sync_job(
        channel_id=channel,
        sync_type="full",
        total_messages=0,
    )

    after = await coll.find_one({"id": "job-completed"})
    assert after is not None
    assert after["status"] == "completed"  # untouched
    assert after["batch_results"] == [{"batch_num": 1, "facts_count": 5}]
    assert after["batches_completed"] == 1


async def test_create_sync_job_scopes_orphaning_by_kind() -> None:
    """A new ``kind="sync"`` insertion MUST NOT orphan a running
    ``kind="wiki_refresh"`` job — those are independent pipelines."""
    channel = "C_DEMO"
    wiki_running = _make_sync_job_doc(
        job_id="job-wiki",
        channel_id=channel,
        status="running",
        started_at=datetime.now(tz=UTC),
        kind="wiki_refresh",
    )
    store, coll = _store_with_jobs([wiki_running])

    await store.create_sync_job(
        channel_id=channel,
        sync_type="full",
        total_messages=0,
        kind="sync",
    )

    after = await coll.find_one({"id": "job-wiki"})
    assert after is not None
    assert after["status"] == "running"  # untouched — different kind
