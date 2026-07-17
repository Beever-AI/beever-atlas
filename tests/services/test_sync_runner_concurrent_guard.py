"""Regression tests for RES-948 — a burst of sync triggers must not pile up dozens
of concurrent "running" sync_jobs on one channel.

``start_sync``'s concurrency guard used ``_is_task_active`` alone (an IN-PROCESS
task registry). A "running" row created moments ago by a concurrent trigger — or
by another worker process — is invisible to that registry, so each trigger treated
the others' fresh rows as crashed leftovers, "recovered" them, and started yet
another sync. ``_running_row_is_active`` now also treats a recently-started running
row as active, so concurrent triggers are rejected instead of piling up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from beever_atlas.services.sync_runner import SyncRunner


def _runner() -> SyncRunner:
    r = object.__new__(SyncRunner)  # bypass __init__ (BatchProcessor)
    r._active_tasks = {}
    return r


def _job(started_at):
    return SimpleNamespace(id="job-1", status="running", started_at=started_at)


def test_recently_started_running_row_blocks_without_local_task() -> None:
    """A running row started seconds ago blocks even though this process has no
    task for it — it is a concurrent / other-process run."""
    r = _runner()
    recent = datetime.now(tz=UTC) - timedelta(seconds=5)
    assert r._running_row_is_active(_job(recent), "C") is True


def test_old_running_row_without_task_is_recoverable() -> None:
    """A running row older than the grace window with no local task is a genuine
    stale leftover — recoverable (not blocking)."""
    r = _runner()
    old = datetime.now(tz=UTC) - timedelta(hours=2)
    assert r._running_row_is_active(_job(old), "C") is False


def test_task_active_always_blocks() -> None:
    """If this process is running the sync task, block regardless of age."""
    r = _runner()
    r._active_tasks["C"] = SimpleNamespace(done=lambda: False)  # type: ignore[assignment]
    old = datetime.now(tz=UTC) - timedelta(hours=5)
    assert r._running_row_is_active(_job(old), "C") is True


def test_missing_started_at_is_not_active() -> None:
    r = _runner()
    assert r._running_row_is_active(_job(None), "C") is False


def test_naive_started_at_treated_as_utc() -> None:
    """Naive datetimes (as Mongo may return) are coerced to UTC, not mis-dated."""
    r = _runner()
    naive_recent = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(seconds=5)
    assert r._running_row_is_active(_job(naive_recent), "C") is True
