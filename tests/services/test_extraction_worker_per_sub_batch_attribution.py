"""Per-sub-batch failure attribution (Phase 1.1 / B1).

These tests lock in the contract that ``ExtractionWorker._process_channel_batch``
finalizes only the failing sub-batch's keys as ``failed`` and the rest as
``done`` — even when sibling sub-batches in the same tick error. Live
evidence (30-min test run on a 711-message channel) showed the legacy
all-or-nothing path reprocessing 511/711 rows; this is the regression
guard for that bug.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.services.batch_processor import BatchBreakdown, BatchResult
from beever_atlas.services.extraction_worker import ExtractionWorker


def _make_doc(*, message_id: str, attempt_count: int = 0) -> dict[str, Any]:
    """Minimal claimed-doc stub matching the worker's reverse mapping."""
    return {
        "source_id": "src",
        "channel_id": "ch",
        "channel_name": "general",
        "message_id": message_id,
        "timestamp": "2026-05-01T00:00:00Z",
        "author": "U1",
        "author_name": "Alice",
        "content": f"hello {message_id}",
        "thread_id": None,
        "attachments": [],
        "reactions": [],
        "reply_count": 0,
        "raw_metadata": {},
        "extraction_status": "extracting",
        "attempt_count": attempt_count,
    }


def _patch_stores(monkeypatch, finalize_done_calls, finalize_failed_calls):
    """Patch ``get_stores()`` so the worker writes to lists instead of mongo.

    Returns the FakeStores instance for inspection.
    """

    class _Mongo:
        async def finalize_extraction_status_bulk(self, **kwargs):
            if kwargs.get("new_status") == "done":
                finalize_done_calls.append(list(kwargs.get("keys") or []))
            else:
                finalize_failed_calls.append(
                    {
                        "keys": list(kwargs.get("keys") or []),
                        "last_error": kwargs.get("last_error"),
                        "next_attempt_at": kwargs.get("next_attempt_at"),
                    }
                )
            return len(kwargs.get("keys") or [])

    class FakeStores:
        mongodb = _Mongo()

    fake = FakeStores()
    import beever_atlas.stores as stores_module

    monkeypatch.setattr(stores_module, "get_stores", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_partial_failure_attribution(monkeypatch) -> None:
    """Sub-batch 2 fails on 429; sub-batches 1 and 3 succeed.

    The worker MUST mark only sub-batch 2's keys as failed and the rest as done.
    """
    keys_succ_1 = [("src", "ch", "m1"), ("src", "ch", "m2")]
    keys_fail = [("src", "ch", "m3")]
    keys_succ_2 = [("src", "ch", "m4"), ("src", "ch", "m5")]

    mock_result = BatchResult(
        total_facts=10,
        errors=[{"batch_index": 2, "error": "RateLimitError 429"}],
        batch_breakdowns=[
            BatchBreakdown(batch_num=1, error=None, keys=keys_succ_1),
            BatchBreakdown(batch_num=2, error="RateLimitError 429", keys=keys_fail),
            BatchBreakdown(batch_num=3, error=None, keys=keys_succ_2),
        ],
    )
    mock_bp = MagicMock()
    mock_bp.process_messages = AsyncMock(return_value=mock_result)

    finalize_done_calls: list[list] = []
    finalize_failed_calls: list[dict] = []
    _patch_stores(monkeypatch, finalize_done_calls, finalize_failed_calls)

    worker = ExtractionWorker(batch_processor=mock_bp)

    docs = [
        _make_doc(message_id="m1"),
        _make_doc(message_id="m2"),
        _make_doc(message_id="m3"),
        _make_doc(message_id="m4"),
        _make_doc(message_id="m5"),
    ]

    succeeded, failed = await worker._process_channel_batch("ch", docs)

    assert succeeded == 4, f"Expected 4 succeeded, got {succeeded}"
    assert failed == 1, f"Expected 1 failed, got {failed}"

    # Done-set must contain m1, m2, m4, m5 (NOT m3)
    assert len(finalize_done_calls) == 1
    done_keys = set(finalize_done_calls[0])
    assert done_keys == set(keys_succ_1 + keys_succ_2)

    # Failed-set must contain only m3
    assert len(finalize_failed_calls) == 1
    failed_call = finalize_failed_calls[0]
    failed_keys = set(failed_call["keys"])
    assert failed_keys == {("src", "ch", "m3")}
    err = failed_call["last_error"] or ""
    assert "RateLimit" in err or "429" in err


@pytest.mark.asyncio
async def test_all_succeed_path_unchanged(monkeypatch) -> None:
    """No errors → all keys go to ``done`` (unchanged from pre-B1)."""
    keys_a = [("src", "ch", "m1"), ("src", "ch", "m2")]
    keys_b = [("src", "ch", "m3"), ("src", "ch", "m4"), ("src", "ch", "m5")]

    mock_result = BatchResult(
        total_facts=15,
        errors=[],
        batch_breakdowns=[
            BatchBreakdown(batch_num=1, error=None, keys=keys_a),
            BatchBreakdown(batch_num=2, error=None, keys=keys_b),
        ],
        fact_ids=["f1", "f2", "f3"],
    )
    mock_bp = MagicMock()
    mock_bp.process_messages = AsyncMock(return_value=mock_result)

    finalize_done_calls: list[list] = []
    finalize_failed_calls: list[dict] = []
    _patch_stores(monkeypatch, finalize_done_calls, finalize_failed_calls)

    worker = ExtractionWorker(batch_processor=mock_bp)

    docs = [_make_doc(message_id=f"m{i}") for i in range(1, 6)]
    succeeded, failed = await worker._process_channel_batch("ch", docs)

    # No errors → goes through legacy success branch, finalizes 5 done.
    assert succeeded == 5
    assert failed == 0
    assert len(finalize_done_calls) == 1
    assert len(finalize_done_calls[0]) == 5
    assert finalize_failed_calls == []


@pytest.mark.asyncio
async def test_all_fail_path_attributes_via_breakdowns(monkeypatch) -> None:
    """When every sub-batch errors, all keys still go to ``failed``."""
    keys_a = [("src", "ch", "m1"), ("src", "ch", "m2")]
    keys_b = [("src", "ch", "m3"), ("src", "ch", "m4"), ("src", "ch", "m5")]

    mock_result = BatchResult(
        total_facts=0,
        errors=[
            {"batch_index": 1, "error": "Gemini 503"},
            {"batch_index": 2, "error": "Gemini 503"},
        ],
        batch_breakdowns=[
            BatchBreakdown(batch_num=1, error="Gemini 503", keys=keys_a),
            BatchBreakdown(batch_num=2, error="Gemini 503", keys=keys_b),
        ],
    )
    mock_bp = MagicMock()
    mock_bp.process_messages = AsyncMock(return_value=mock_result)

    finalize_done_calls: list[list] = []
    finalize_failed_calls: list[dict] = []
    _patch_stores(monkeypatch, finalize_done_calls, finalize_failed_calls)

    worker = ExtractionWorker(batch_processor=mock_bp)

    docs = [_make_doc(message_id=f"m{i}") for i in range(1, 6)]
    succeeded, failed = await worker._process_channel_batch("ch", docs)

    assert succeeded == 0
    assert failed == 5
    # All five keys should land in the failed set; finalize_done must not fire.
    assert finalize_done_calls == []
    assert len(finalize_failed_calls) >= 1
    all_failed_keys = set()
    for call in finalize_failed_calls:
        all_failed_keys.update(call["keys"])
    assert all_failed_keys == set(keys_a + keys_b)


@pytest.mark.asyncio
async def test_legacy_path_when_keys_empty(monkeypatch) -> None:
    """If breakdowns carry no keys, fall back to the all-or-nothing path.

    Older callers / deep error paths may emit a stub BatchBreakdown with
    ``keys=[]``. The worker must not silently drop those rows into limbo —
    it must fall through to ``_finalize_failed`` on the full claimed set
    so the stale-recovery sweep is at most a backstop, not the only
    safety net.
    """
    mock_result = BatchResult(
        total_facts=0,
        errors=[{"batch_index": 1, "error": "boom"}],
        batch_breakdowns=[BatchBreakdown(batch_num=1, error="boom")],  # keys empty
    )
    mock_bp = MagicMock()
    mock_bp.process_messages = AsyncMock(return_value=mock_result)

    finalize_done_calls: list[list] = []
    finalize_failed_calls: list[dict] = []
    _patch_stores(monkeypatch, finalize_done_calls, finalize_failed_calls)

    worker = ExtractionWorker(batch_processor=mock_bp)

    docs = [_make_doc(message_id=f"m{i}") for i in range(1, 4)]
    succeeded, failed = await worker._process_channel_batch("ch", docs)

    assert succeeded == 0
    assert failed == 3
    assert finalize_done_calls == []
    # Legacy path: every claimed row in finalize_failed.
    all_failed_keys: set = set()
    for call in finalize_failed_calls:
        all_failed_keys.update(call["keys"])
    assert all_failed_keys == {
        ("src", "ch", "m1"),
        ("src", "ch", "m2"),
        ("src", "ch", "m3"),
    }
