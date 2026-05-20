from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from beever_atlas.services import sync_runner as sync_runner_module
from beever_atlas.services.batch_processor import BatchResult


@pytest.fixture(autouse=True)
def _reset_batch_processor_module_locks():
    """Reset event-loop-bound module-level primitives between tests.

    PR-C removed ``_consecutive_503_count`` / ``_consecutive_503_lock`` —
    the breaker is now an injected :class:`CircuitBreaker`. The remaining
    module-globals (``_limiter_lock`` rate-limiter lock and
    ``_provider_limiters`` AsyncLimiter cache) still bind to whichever
    event loop first imported the module, so this fixture re-creates
    them per test to prevent ``Event loop is closed`` cascades when the
    sync_runner tests run after a TestClient-based test torn down its
    loop. Also resets the breaker singleton so each test starts closed.
    """
    import beever_atlas.services.batch_processor as bp_mod
    from beever_atlas.services.circuit_breaker import reset_circuit_breaker_for_tests

    bp_mod._limiter_lock = asyncio.Lock()
    bp_mod._provider_limiters = {}
    reset_circuit_breaker_for_tests()
    yield


@dataclass
class _Msg:
    timestamp: datetime


class _InclusiveSinceAdapter:
    def __init__(self, messages: list[_Msg]) -> None:
        self.messages = messages
        self.calls = 0

    async def fetch_history(
        self,
        channel_id: str,
        since: datetime | None,
        limit: int,
        order: str = "desc",
    ) -> list[_Msg]:
        self.calls += 1
        if since is None:
            return self.messages[:2]
        return [m for m in self.messages if m.timestamp >= since][:2]


class _Status:
    def __init__(
        self,
        *,
        id: str,
        status: str,
        started_at: datetime,
        processed_messages: int = 0,
        total_messages: int = 0,
        current_batch: int = 0,
    ) -> None:
        self.id = id
        self.status = status
        self.started_at = started_at
        self.processed_messages = processed_messages
        self.total_messages = total_messages
        self.current_batch = current_batch


@pytest.mark.asyncio
async def test_fetch_all_messages_filters_inclusive_cursor_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 3, 1, 11, 0, tzinfo=UTC)
    t3 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    adapter = _InclusiveSinceAdapter([_Msg(t1), _Msg(t2), _Msg(t3)])

    monkeypatch.setattr(sync_runner_module, "get_adapter", lambda: adapter)
    monkeypatch.setattr(
        sync_runner_module,
        "get_settings",
        lambda: SimpleNamespace(sync_max_messages=100),
    )

    runner = sync_runner_module.SyncRunner()
    result = await runner._fetch_all_messages("C123", adapter=adapter)

    assert [m.timestamp for m in result] == [t1, t2, t3]
    assert adapter.calls == 3


@pytest.mark.asyncio
async def test_fetch_all_messages_parses_iso_since_string(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_since: datetime | None = None

    class _Adapter:
        async def fetch_history(
            self,
            channel_id: str,
            since: datetime | None,
            limit: int,
            order: str = "desc",
        ) -> list[_Msg]:
            nonlocal seen_since
            seen_since = since
            return []

    _adapter_instance = _Adapter()
    monkeypatch.setattr(sync_runner_module, "get_adapter", lambda: _adapter_instance)
    monkeypatch.setattr(
        sync_runner_module,
        "get_settings",
        lambda: SimpleNamespace(sync_max_messages=100),
    )

    runner = sync_runner_module.SyncRunner()
    await runner._fetch_all_messages(
        "C123", adapter=_adapter_instance, since="2026-03-15T00:00:00Z"
    )

    assert isinstance(seen_since, datetime)
    assert seen_since == datetime(2026, 3, 15, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_run_sync_marks_completed_with_errors_when_batches_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-0: extraction failures alone produce ``completed_with_errors`` (not
    ``failed``) and populate ``failed_batches`` so an operator can recover.

    Spec: sync-cursor-resilience > Three terminal sync statuses.
    """
    calls: dict[str, object] = {}

    class _Mongo:
        async def upsert_channel_messages(self, rows) -> dict[str, int]:
            return {"inserted": len(rows), "modified": 0, "matched": 0, "upserted_ids": len(rows)}

        async def complete_sync_job(
            self,
            job_id: str,
            status: str,
            errors: list[str] | None = None,
            failed_stage: str | None = None,
            failed_batches: list[dict[str, object]] | None = None,
        ) -> None:
            calls["complete"] = {
                "job_id": job_id,
                "status": status,
                "errors": errors,
                "failed_batches": failed_batches,
            }

        async def log_activity(
            self, event_type: str, channel_id: str, details: dict[str, object]
        ) -> None:
            calls["activity"] = {
                "event_type": event_type,
                "channel_id": channel_id,
                "details": details,
            }

        async def update_channel_sync_state(
            self, channel_id: str, last_sync_ts: str, increment: int = 0, **kwargs
        ) -> None:
            calls["sync_state"] = {
                "channel_id": channel_id,
                "last_sync_ts": last_sync_ts,
                "increment": increment,
            }

    stores = SimpleNamespace(mongodb=_Mongo())
    monkeypatch.setattr(sync_runner_module, "get_stores", lambda: stores)

    async def _fake_resolve_policy(channel_id):
        from types import SimpleNamespace as NS

        return NS(ingestion=NS(), sync=NS(max_messages=100))

    monkeypatch.setattr(
        "beever_atlas.services.policy_resolver.resolve_effective_policy",
        _fake_resolve_policy,
        raising=False,
    )

    # Force inline extraction path regardless of .env value.
    monkeypatch.setattr(
        sync_runner_module,
        "get_settings",
        lambda: SimpleNamespace(decouple_extraction=False),
    )

    async def _process_messages(**kwargs) -> BatchResult:
        return BatchResult(
            total_facts=0,
            total_entities=0,
            errors=[
                {
                    "batch_num": 0,
                    "error": "503 UNAVAILABLE",
                    "error_class": "ServerError",
                    "message_count": 50,
                    "timestamp_range_start": "2026-04-29T10:00:00Z",
                    "timestamp_range_end": "2026-04-29T10:05:00Z",
                }
            ],
        )

    runner = sync_runner_module.SyncRunner()
    runner._batch_processor = SimpleNamespace(process_messages=_process_messages)

    await runner._run_sync(
        job_id="job-1",
        channel_id="C123",
        channel_name="general",
        messages=[],
    )

    complete = calls.get("complete")
    assert isinstance(complete, dict)
    # PR-0: extraction failure ≠ sync failure. Status reflects partial success.
    assert complete["status"] == "completed_with_errors"
    failed_batches = complete["failed_batches"]
    assert isinstance(failed_batches, list) and len(failed_batches) == 1
    entry = failed_batches[0]
    assert entry["batch_index"] == 0
    assert entry["error_class"] == "ServerError"
    assert entry["message_count"] == 50
    assert entry["error_summary"].startswith("503")
    # Activity log event_type stays binary (matches existing UI contract;
    # frontend dedupe lands in PR-B).
    activity = calls.get("activity")
    assert isinstance(activity, dict)
    assert activity["event_type"] == "sync_failed"


def test_normalized_to_channel_messages_maps_required_fields() -> None:
    """PR-A.3: NormalizedMessage-shaped objects convert to ChannelMessage rows
    with source_id derived from ``platform``.

    Spec: message-store > Per-message extraction state machine > New message
    inserted on sync (status defaults to ``pending`` via ChannelMessage model).
    """
    from beever_atlas.services.sync_runner import _normalized_to_channel_messages

    msgs = [
        SimpleNamespace(
            platform="slack",
            channel_id="C123",
            message_id="m1",
            timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
            author="alice",
            author_name="Alice",
            author_image="",
            content="hello",
            thread_id=None,
            attachments=[],
            reactions=[],
            reply_count=0,
            raw_metadata={"is_bot": False},
        )
    ]
    rows = _normalized_to_channel_messages(msgs)
    assert len(rows) == 1
    row = rows[0]
    assert row.source_id == "slack"
    assert row.channel_id == "C123"
    assert row.message_id == "m1"
    assert row.content == "hello"
    assert row.extraction_status == "pending"
    assert row.attempt_count == 0


def test_normalized_to_channel_messages_skips_messages_without_identity() -> None:
    """Defensive: messages missing message_id are skipped (no key to dedup on)."""
    from beever_atlas.services.sync_runner import _normalized_to_channel_messages

    msgs = [
        SimpleNamespace(
            platform="slack",
            channel_id="C123",
            message_id="",  # empty → skip
            timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
            author="bob",
            content="ghost",
        )
    ]
    rows = _normalized_to_channel_messages(msgs)
    assert rows == []


def test_normalized_to_channel_messages_accepts_dict_messages() -> None:
    """File importer hands dicts; converter must accept both shapes."""
    from beever_atlas.services.sync_runner import _normalized_to_channel_messages

    msgs = [
        {
            "platform": "file",
            "channel_id": "csv-channel",
            "message_id": "row-3",
            "timestamp": datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
            "author": "csv:alice",
            "content": "imported text",
        }
    ]
    rows = _normalized_to_channel_messages(msgs)
    assert len(rows) == 1
    assert rows[0].source_id == "file"
    assert rows[0].channel_id == "csv-channel"


@pytest.mark.asyncio
async def test_run_sync_upserts_messages_to_message_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-A.3: ``_run_sync`` calls ``upsert_channel_messages`` on the store
    BEFORE invoking BatchProcessor — the durable store is populated even if
    extraction subsequently fails."""
    upsert_called: dict[str, object] = {}

    class _Mongo:
        async def upsert_channel_messages(self, rows) -> dict[str, int]:
            upsert_called["rows"] = rows
            upsert_called["count"] = len(rows)
            return {"inserted": len(rows), "modified": 0, "matched": 0, "upserted_ids": len(rows)}

        async def complete_sync_job(self, **kwargs) -> None:
            pass

        async def log_activity(self, **kwargs) -> None:
            pass

        async def update_channel_sync_state(self, **kwargs) -> None:
            pass

    stores = SimpleNamespace(mongodb=_Mongo())
    monkeypatch.setattr(sync_runner_module, "get_stores", lambda: stores)

    async def _fake_resolve_policy(channel_id):
        from types import SimpleNamespace as NS

        return NS(ingestion=NS(), sync=NS(max_messages=100))

    monkeypatch.setattr(
        "beever_atlas.services.policy_resolver.resolve_effective_policy",
        _fake_resolve_policy,
        raising=False,
    )

    async def _process_messages(**kwargs) -> BatchResult:
        return BatchResult(total_facts=1, total_entities=1, errors=[])

    runner = sync_runner_module.SyncRunner()
    runner._batch_processor = SimpleNamespace(process_messages=_process_messages)

    msg = SimpleNamespace(
        platform="slack",
        channel_id="C789",
        message_id="m_only",
        timestamp=datetime(2026, 4, 29, 11, 0, tzinfo=UTC),
        author="carol",
        thread_id=None,
        content="hi",
    )
    await runner._run_sync(
        job_id="job-3",
        channel_id="C789",
        channel_name="general",
        messages=[msg],
        parent_count=1,
    )

    assert upsert_called.get("count") == 1, (
        "upsert_channel_messages was not called with the fetched message"
    )
    rows = upsert_called["rows"]
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0].source_id == "slack"
    assert rows[0].channel_id == "C789"
    assert rows[0].message_id == "m_only"


@pytest.mark.asyncio
async def test_run_sync_continues_when_message_store_upsert_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-A.3 best-effort contract: a Mongo error during the upsert MUST NOT
    fail the sync — extraction proceeds inline (existing path) until the
    READ_FROM_MESSAGE_STORE flag flips in PR-A.4."""

    class _Mongo:
        async def upsert_channel_messages(self, rows) -> dict[str, int]:
            raise RuntimeError("mongo down")

        async def complete_sync_job(self, **kwargs) -> None:
            pass

        async def log_activity(self, **kwargs) -> None:
            pass

        async def update_channel_sync_state(self, **kwargs) -> None:
            pass

    stores = SimpleNamespace(mongodb=_Mongo())
    monkeypatch.setattr(sync_runner_module, "get_stores", lambda: stores)

    async def _fake_resolve_policy(channel_id):
        from types import SimpleNamespace as NS

        return NS(ingestion=NS(), sync=NS(max_messages=100))

    monkeypatch.setattr(
        "beever_atlas.services.policy_resolver.resolve_effective_policy",
        _fake_resolve_policy,
        raising=False,
    )

    # Force inline extraction path regardless of .env value.
    monkeypatch.setattr(
        sync_runner_module,
        "get_settings",
        lambda: SimpleNamespace(decouple_extraction=False),
    )

    process_called: dict[str, bool] = {"ran": False}

    async def _process_messages(**kwargs) -> BatchResult:
        process_called["ran"] = True
        return BatchResult(total_facts=0, total_entities=0, errors=[])

    runner = sync_runner_module.SyncRunner()
    runner._batch_processor = SimpleNamespace(process_messages=_process_messages)

    msg = SimpleNamespace(
        platform="slack",
        channel_id="C_FAIL",
        message_id="m1",
        timestamp=datetime(2026, 4, 29, 11, 0, tzinfo=UTC),
        author="dan",
        thread_id=None,
        content="hi",
    )
    # Should NOT raise.
    await runner._run_sync(
        job_id="job-4",
        channel_id="C_FAIL",
        channel_name="general",
        messages=[msg],
        parent_count=1,
    )
    assert process_called["ran"] is True


@pytest.mark.asyncio
async def test_run_sync_increments_total_by_inserted_count_not_parent_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-A.6.1 (review C1): on incremental sync, ``update_channel_sync_state``
    SHALL increment ``total_synced_messages`` by the upsert's NEW-rows count —
    NOT by ``parent_count`` — so a manual re-sync doesn't inflate the total.
    """
    calls: dict[str, object] = {}

    class _Mongo:
        async def upsert_channel_messages(self, rows) -> dict[str, int]:
            # Simulate "5 messages re-fetched, only 2 are new" — the common
            # case after a manual re-sync where most rows already exist.
            return {"inserted": 2, "modified": 0, "matched": 3, "upserted_ids": 2}

        async def complete_sync_job(self, **kwargs) -> None:
            pass

        async def log_activity(self, **kwargs) -> None:
            pass

        async def update_channel_sync_state(self, **kwargs) -> None:
            calls["sync_state"] = kwargs

    stores = SimpleNamespace(mongodb=_Mongo())
    monkeypatch.setattr(sync_runner_module, "get_stores", lambda: stores)

    async def _fake_resolve_policy(channel_id):
        from types import SimpleNamespace as NS

        return NS(ingestion=NS(), sync=NS(max_messages=100))

    monkeypatch.setattr(
        "beever_atlas.services.policy_resolver.resolve_effective_policy",
        _fake_resolve_policy,
        raising=False,
    )

    async def _process_messages(**kwargs) -> BatchResult:
        return BatchResult(total_facts=0, total_entities=0, errors=[])

    runner = sync_runner_module.SyncRunner()
    runner._batch_processor = SimpleNamespace(process_messages=_process_messages)

    msgs = [
        SimpleNamespace(
            platform="slack",
            channel_id="C_INCR",
            message_id=f"m{i}",
            timestamp=datetime(2026, 4, 30, 10, i, tzinfo=UTC),
            author="alice",
            thread_id=None,
            content=f"msg {i}",
        )
        for i in range(5)
    ]

    await runner._run_sync(
        job_id="job-incr",
        channel_id="C_INCR",
        channel_name="general",
        messages=msgs,
        parent_count=5,
        sync_type="incremental",
    )

    sync_state = calls.get("sync_state")
    assert isinstance(sync_state, dict), "update_channel_sync_state was not called"
    # Pre-PR-A.6.1: increment would be 5 (parent_count) → over-counts.
    # Post-PR-A.6.1: increment is 2 (inserted count from upsert).
    assert sync_state["increment"] == 2, (
        f"expected increment=2 (inserted count), got {sync_state.get('increment')} — "
        "PR-A.6.1 C1 regression: total_synced_messages would inflate on re-sync"
    )


@pytest.mark.asyncio
async def test_run_sync_increment_falls_back_to_parent_count_on_upsert_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-A.6.1 (review C1) fallback contract: when the Mongo upsert fails
    or is skipped, the cursor-advance increment falls back to ``parent_count``
    so we preserve the pre-PR-A.6.1 behaviour for that branch.
    """
    calls: dict[str, object] = {}

    class _Mongo:
        async def upsert_channel_messages(self, rows) -> dict[str, int]:
            raise RuntimeError("mongo down")

        async def complete_sync_job(self, **kwargs) -> None:
            pass

        async def log_activity(self, **kwargs) -> None:
            pass

        async def update_channel_sync_state(self, **kwargs) -> None:
            calls["sync_state"] = kwargs

    stores = SimpleNamespace(mongodb=_Mongo())
    monkeypatch.setattr(sync_runner_module, "get_stores", lambda: stores)

    async def _fake_resolve_policy(channel_id):
        from types import SimpleNamespace as NS

        return NS(ingestion=NS(), sync=NS(max_messages=100))

    monkeypatch.setattr(
        "beever_atlas.services.policy_resolver.resolve_effective_policy",
        _fake_resolve_policy,
        raising=False,
    )

    async def _process_messages(**kwargs) -> BatchResult:
        return BatchResult(total_facts=0, total_entities=0, errors=[])

    runner = sync_runner_module.SyncRunner()
    runner._batch_processor = SimpleNamespace(process_messages=_process_messages)

    msg = SimpleNamespace(
        platform="slack",
        channel_id="C_FB",
        message_id="m1",
        timestamp=datetime(2026, 4, 30, 10, 0, tzinfo=UTC),
        author="alice",
        thread_id=None,
        content="hi",
    )
    await runner._run_sync(
        job_id="job-fb",
        channel_id="C_FB",
        channel_name="general",
        messages=[msg],
        parent_count=1,
        sync_type="incremental",
    )

    sync_state = calls.get("sync_state")
    assert isinstance(sync_state, dict)
    # Upsert raised → inserted_count is None → fallback to parent_count=1.
    assert sync_state["increment"] == 1


@pytest.mark.asyncio
async def test_run_sync_advances_cursor_even_when_batches_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR-0: cursor advances on successful fetch regardless of extraction errors.

    Spec: sync-cursor-resilience > Cursor advances on successful fetch
    independent of extraction outcome > Scenario: Fetch succeeds, some
    extraction batches fail.
    """
    calls: dict[str, object] = {}

    class _Mongo:
        async def upsert_channel_messages(self, rows) -> dict[str, int]:
            return {"inserted": len(rows), "modified": 0, "matched": 0, "upserted_ids": len(rows)}

        async def complete_sync_job(self, **kwargs) -> None:
            calls["complete"] = kwargs

        async def log_activity(self, **kwargs) -> None:
            calls.setdefault("activity_calls", []).append(kwargs)  # type: ignore[union-attr]

        async def update_channel_sync_state(self, **kwargs) -> None:
            calls["sync_state"] = kwargs

    stores = SimpleNamespace(mongodb=_Mongo())
    monkeypatch.setattr(sync_runner_module, "get_stores", lambda: stores)

    async def _fake_resolve_policy(channel_id):
        from types import SimpleNamespace as NS

        return NS(ingestion=NS(), sync=NS(max_messages=100))

    monkeypatch.setattr(
        "beever_atlas.services.policy_resolver.resolve_effective_policy",
        _fake_resolve_policy,
        raising=False,
    )

    # Force inline extraction path regardless of .env value.
    monkeypatch.setattr(
        sync_runner_module,
        "get_settings",
        lambda: SimpleNamespace(decouple_extraction=False),
    )

    async def _process_messages(**kwargs) -> BatchResult:
        # 3 batches reported; 1 failed.
        return BatchResult(
            total_facts=10,
            total_entities=5,
            errors=[{"batch_num": 1, "error": "503", "message_count": 50}],
        )

    runner = sync_runner_module.SyncRunner()
    runner._batch_processor = SimpleNamespace(process_messages=_process_messages)

    # Provide messages with timestamps so last_ts is computable.
    latest = datetime(2026, 4, 29, 11, 0, tzinfo=UTC)
    earlier = datetime(2026, 4, 29, 10, 0, tzinfo=UTC)
    msg_old = SimpleNamespace(message_id="m1", thread_id=None, timestamp=earlier)
    msg_new = SimpleNamespace(message_id="m2", thread_id=None, timestamp=latest)

    await runner._run_sync(
        job_id="job-2",
        channel_id="C456",
        channel_name="general",
        messages=[msg_old, msg_new],
        parent_count=2,
    )

    # Cursor MUST advance even though one batch failed.
    sync_state = calls.get("sync_state")
    assert isinstance(sync_state, dict), "cursor not advanced — PR-0 regression"
    assert sync_state["channel_id"] == "C456"
    assert sync_state["last_sync_ts"] == latest.isoformat()

    complete = calls.get("complete")
    assert isinstance(complete, dict)
    assert complete["status"] == "completed_with_errors"


@pytest.mark.asyncio
async def test_start_sync_recovers_stale_running_job(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}
    now = datetime(2026, 3, 30, 13, 30, tzinfo=UTC)
    stale = _Status(
        id="job-stale",
        status="running",
        started_at=now,
        processed_messages=0,
        total_messages=9,
        current_batch=0,
    )

    class _Mongo:
        async def is_purging(self, channel_id: str, **kwargs) -> bool:
            # delete-channel-v2 Wave 0 — start_sync now consults the purge
            # guard at function entry; this channel is not purging.
            return False

        async def get_sync_status(self, channel_id: str):
            calls["get_sync_status"] = channel_id
            return stale

        async def get_channel_sync_state(self, channel_id: str):
            return None

        async def complete_sync_job(
            self, job_id: str, status: str, errors: list[str] | None = None
        ) -> None:
            calls["complete_sync_job"] = {"job_id": job_id, "status": status, "errors": errors}

        async def create_sync_job(
            self,
            channel_id: str,
            sync_type: str,
            total_messages: int,
            batch_size: int,
            parent_messages: int = 0,
            **kwargs,
        ):
            calls["create_sync_job"] = {
                "channel_id": channel_id,
                "sync_type": sync_type,
                "total_messages": total_messages,
                "parent_messages": parent_messages,
                "batch_size": batch_size,
                **kwargs,
            }
            return SimpleNamespace(id="job-new")

    class _Adapter:
        async def fetch_history(self, channel_id: str, since, limit: int, order: str = "desc"):
            return []

        async def get_channel_info(self, channel_id: str):
            return SimpleNamespace(name="all-testing")

    class _FakeCollection:
        def find(self, *args, **kwargs):
            return self

        def to_list(self, length=None):
            async def _empty():
                return []

            return _empty()

    class _FakeDb:
        def __getitem__(self, name):
            return _FakeCollection()

    mongo = _Mongo()
    mongo.db = _FakeDb()
    stores = SimpleNamespace(mongodb=mongo)
    monkeypatch.setattr(sync_runner_module, "get_stores", lambda: stores)
    monkeypatch.setattr(
        sync_runner_module,
        "get_settings",
        lambda: SimpleNamespace(
            sync_max_messages=100, sync_batch_size=50, stale_job_threshold_hours=2
        ),
    )
    monkeypatch.setattr(sync_runner_module, "get_adapter", lambda: _Adapter())

    import beever_atlas.services.policy_resolver as _policy_mod

    async def _fake_policy(channel_id):
        return SimpleNamespace(sync=SimpleNamespace(max_messages=100), ingestion=SimpleNamespace())

    monkeypatch.setattr(_policy_mod, "resolve_effective_policy", _fake_policy)

    runner = sync_runner_module.SyncRunner()

    async def _fake_resolve_conn(channel_id, connection_id):
        return None

    runner._resolve_connection_id = _fake_resolve_conn

    job_id = await runner.start_sync("C0AMY9QSPB2")

    assert job_id == "job-new"
    completed = calls.get("complete_sync_job")
    assert isinstance(completed, dict)
    assert completed["job_id"] == "job-stale"
    assert completed["status"] == "failed"


def test_has_active_sync_returns_false_for_done_task() -> None:
    runner = sync_runner_module.SyncRunner()

    async def _noop() -> None:
        return None

    async def _run() -> None:
        task = asyncio.create_task(_noop())
        runner._active_tasks["C123"] = task
        await task
        assert runner.has_active_sync("C123") is False

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# PR-B.3 — DECOUPLE_EXTRACTION flag wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_sync_skips_inline_extraction_when_decouple_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``DECOUPLE_EXTRACTION=true``, ``_run_sync`` MUST skip
    ``BatchProcessor.process_messages`` entirely.

    Messages still land in ``channel_messages`` via the PR-A.3 upsert
    (with ``extraction_status="pending"``); the background ExtractionWorker
    is responsible for actually running the LLM pipeline. This is the
    primary lever that makes a Gemini 503 storm survivable — sync
    completes in seconds even when the LLM is unreachable.
    """

    class _Mongo:
        async def upsert_channel_messages(self, rows) -> dict[str, int]:
            return {
                "inserted": len(rows),
                "modified": 0,
                "matched": 0,
                "upserted_ids": len(rows),
            }

        async def complete_sync_job(self, **kwargs) -> None:
            pass

        async def log_activity(self, **kwargs) -> None:
            pass

        async def update_channel_sync_state(self, **kwargs) -> None:
            pass

    stores = SimpleNamespace(mongodb=_Mongo())
    monkeypatch.setattr(sync_runner_module, "get_stores", lambda: stores)

    async def _fake_resolve_policy(channel_id):
        from types import SimpleNamespace as NS

        return NS(ingestion=NS(), sync=NS(max_messages=100))

    monkeypatch.setattr(
        "beever_atlas.services.policy_resolver.resolve_effective_policy",
        _fake_resolve_policy,
        raising=False,
    )

    # Flip the flag ON for this test only — get_settings() inside _run_sync.
    monkeypatch.setattr(
        sync_runner_module,
        "get_settings",
        lambda: SimpleNamespace(decouple_extraction=True),
    )

    process_called: dict[str, bool] = {"ran": False}

    async def _process_messages(**kwargs) -> BatchResult:
        process_called["ran"] = True
        return BatchResult(total_facts=0, total_entities=0, errors=[])

    runner = sync_runner_module.SyncRunner()
    runner._batch_processor = SimpleNamespace(process_messages=_process_messages)

    msg = SimpleNamespace(
        platform="slack",
        channel_id="C_DECOUPLE",
        message_id="m1",
        timestamp=datetime(2026, 4, 30, 11, 0, tzinfo=UTC),
        author="dave",
        thread_id=None,
        content="hi",
    )
    await runner._run_sync(
        job_id="job-decouple",
        channel_id="C_DECOUPLE",
        channel_name="general",
        messages=[msg],
        parent_count=1,
    )

    # The flag's whole point: BatchProcessor was NOT invoked inline.
    assert process_called["ran"] is False, (
        "DECOUPLE_EXTRACTION=true must skip BatchProcessor.process_messages — "
        "the worker is responsible for extraction."
    )


@pytest.mark.asyncio
async def test_run_sync_runs_inline_extraction_when_decouple_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default behaviour (flag OFF) MUST still invoke inline extraction.

    Guards against accidental default-flip during code edits — the rollout
    is staging-soak first, production second; both default OFF.
    """

    class _Mongo:
        async def upsert_channel_messages(self, rows) -> dict[str, int]:
            return {
                "inserted": len(rows),
                "modified": 0,
                "matched": 0,
                "upserted_ids": len(rows),
            }

        async def complete_sync_job(self, **kwargs) -> None:
            pass

        async def log_activity(self, **kwargs) -> None:
            pass

        async def update_channel_sync_state(self, **kwargs) -> None:
            pass

    stores = SimpleNamespace(mongodb=_Mongo())
    monkeypatch.setattr(sync_runner_module, "get_stores", lambda: stores)

    async def _fake_resolve_policy(channel_id):
        from types import SimpleNamespace as NS

        return NS(ingestion=NS(), sync=NS(max_messages=100))

    monkeypatch.setattr(
        "beever_atlas.services.policy_resolver.resolve_effective_policy",
        _fake_resolve_policy,
        raising=False,
    )

    monkeypatch.setattr(
        sync_runner_module,
        "get_settings",
        lambda: SimpleNamespace(decouple_extraction=False),
    )

    process_called: dict[str, bool] = {"ran": False}

    async def _process_messages(**kwargs) -> BatchResult:
        process_called["ran"] = True
        return BatchResult(total_facts=0, total_entities=0, errors=[])

    runner = sync_runner_module.SyncRunner()
    runner._batch_processor = SimpleNamespace(process_messages=_process_messages)

    msg = SimpleNamespace(
        platform="slack",
        channel_id="C_INLINE",
        message_id="m1",
        timestamp=datetime(2026, 4, 30, 11, 0, tzinfo=UTC),
        author="eve",
        thread_id=None,
        content="hi",
    )
    await runner._run_sync(
        job_id="job-inline",
        channel_id="C_INLINE",
        channel_name="general",
        messages=[msg],
        parent_count=1,
    )

    assert process_called["ran"] is True, (
        "DECOUPLE_EXTRACTION=false must keep inline BatchProcessor invocation."
    )
