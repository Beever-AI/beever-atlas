"""Invariant test for per-sub-batch failure attribution (B1 / Task 2.1.7).

Asserts the safety property the change exists to provide:
    succeeded_keys ∪ failed_keys == set(valid_keys)
    succeeded_keys ∩ failed_keys == ∅

Parametrised over the full success/failure split — guards against rows
leaking into limbo (claimed but never finalized) on any combination of
sub-batch outcomes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.services.batch_processor import BatchBreakdown, BatchResult
from beever_atlas.services.extraction_worker import ExtractionWorker


def _make_doc(*, message_id: str, attempt_count: int = 0) -> dict[str, Any]:
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
    class _Mongo:
        async def finalize_extraction_status_bulk(self, **kwargs):
            if kwargs.get("new_status") == "done":
                finalize_done_calls.append(list(kwargs.get("keys") or []))
            else:
                finalize_failed_calls.append(list(kwargs.get("keys") or []))
            return len(kwargs.get("keys") or [])

    class FakeStores:
        mongodb = _Mongo()

    fake = FakeStores()
    import beever_atlas.stores as stores_module

    monkeypatch.setattr(stores_module, "get_stores", lambda: fake)
    return fake


@pytest.mark.parametrize(
    "n_succeed_batches,n_fail_batches",
    [(8, 0), (7, 1), (4, 4), (1, 7), (0, 8)],
)
@pytest.mark.asyncio
async def test_partition_invariant(
    n_succeed_batches: int, n_fail_batches: int, monkeypatch
) -> None:
    """``succeeded_keys ∪ failed_keys == valid_keys`` for every split."""
    rows_per_batch = 3
    breakdowns: list[BatchBreakdown] = []
    docs: list[dict] = []
    valid_keys: set[tuple[str, str, str]] = set()
    msg_idx = 1

    # Build the succeeding sub-batches first.
    for batch_i in range(n_succeed_batches):
        keys: list[tuple[str, str, str]] = []
        for _ in range(rows_per_batch):
            mid = f"m{msg_idx}"
            msg_idx += 1
            keys.append(("src", "ch", mid))
            valid_keys.add(("src", "ch", mid))
            docs.append(_make_doc(message_id=mid))
        breakdowns.append(BatchBreakdown(batch_num=batch_i + 1, error=None, keys=keys))

    # Then the failing ones.
    for batch_i in range(n_fail_batches):
        keys = []
        for _ in range(rows_per_batch):
            mid = f"m{msg_idx}"
            msg_idx += 1
            keys.append(("src", "ch", mid))
            valid_keys.add(("src", "ch", mid))
            docs.append(_make_doc(message_id=mid))
        breakdowns.append(
            BatchBreakdown(
                batch_num=n_succeed_batches + batch_i + 1,
                error="forced 429",
                keys=keys,
            )
        )

    errors = [
        {"batch_index": n_succeed_batches + i + 1, "error": "forced 429"}
        for i in range(n_fail_batches)
    ]
    mock_result = BatchResult(errors=errors, batch_breakdowns=breakdowns)
    mock_bp = MagicMock()
    mock_bp.process_messages = AsyncMock(return_value=mock_result)

    finalize_done_calls: list[list] = []
    finalize_failed_calls: list[list] = []
    _patch_stores(monkeypatch, finalize_done_calls, finalize_failed_calls)

    worker = ExtractionWorker(batch_processor=mock_bp)
    succeeded, failed = await worker._process_channel_batch("ch", docs)

    expected_total = (n_succeed_batches + n_fail_batches) * rows_per_batch
    assert succeeded + failed == expected_total

    done_keys: set = set()
    for call in finalize_done_calls:
        done_keys.update(call)
    failed_keys: set = set()
    for call in finalize_failed_calls:
        failed_keys.update(call)

    # When there are NO errors, the worker takes the legacy success branch
    # which calls finalize_done on ``valid_keys`` directly (one bulk call).
    # When there ARE errors, the new partition path produces the split.
    if n_fail_batches == 0:
        assert done_keys == valid_keys
        assert failed_keys == set()
    else:
        # Invariant: union covers every claimed row, intersection empty.
        assert done_keys | failed_keys == valid_keys, (
            f"succeeded ∪ failed must cover all valid_keys "
            f"(missing: {valid_keys - (done_keys | failed_keys)})"
        )
        assert done_keys & failed_keys == set(), (
            f"succeeded ∩ failed must be empty (overlap: {done_keys & failed_keys})"
        )
