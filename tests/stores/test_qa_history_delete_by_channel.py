"""Tests for ``QAHistoryStore.delete_by_channel`` (delete-channel-v2 Wave 1).

HARD delete of every QAHistory entry for a channel — ignores ``is_deleted``
(both live and soft-deleted rows go). Modelled on the WeaviateStore batch
delete: one ``collection.data.delete_many(where=<channel filter>)`` call,
returns ``result.successful``.

No live Weaviate — a stubbed v4 collection records the call so we can assert
the filter shape and that ``fetch_objects`` (the unbounded-bug path) is never
used.

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from beever_atlas.stores.qa_history_store import QAHistoryStore


def _store_with_stub(successful: int = 0, failed: int = 0, matches: int | None = None):
    store = QAHistoryStore("http://localhost:8080")
    fake_collection = MagicMock(name="QAHistoryCollection")
    fake_collection.data.delete_many = MagicMock(
        return_value=SimpleNamespace(
            successful=successful,
            failed=failed,
            matches=matches if matches is not None else successful,
        )
    )
    store._collection = lambda: fake_collection  # type: ignore[method-assign]
    return store, fake_collection


async def test_delete_by_channel_uses_batch_delete_many() -> None:
    store, fake = _store_with_stub(successful=42)

    deleted = await store.delete_by_channel("C1")

    assert deleted == 42  # returns result.successful
    fake.data.delete_many.assert_called_once()
    _, kwargs = fake.data.delete_many.call_args
    assert "where" in kwargs and kwargs["where"] is not None
    # Must NOT fall back to the fetch+loop (the unbounded-delete bug path).
    fake.query.fetch_objects.assert_not_called()


async def test_delete_by_channel_ignores_is_deleted() -> None:
    """The hard purge filters ONLY on channel_id — no is_deleted clause — so
    soft-deleted rows are removed too. We assert no is_deleted constraint is
    added by checking only one filter (the channel) is passed."""
    store, fake = _store_with_stub(successful=3)

    await store.delete_by_channel("C1")

    _, kwargs = fake.data.delete_many.call_args
    where = kwargs["where"]
    # A single by_property("channel_id").equal(...) filter has operator
    # EQUAL; a channel & not_deleted combo (the search path) would be an
    # AND/OR composite. Compare on the operator's string name so the assert
    # is robust to the private ``_Operator`` enum.
    op_name = str(getattr(getattr(where, "operator", None), "name", "")).upper()
    assert op_name not in ("AND", "OR"), (
        "delete_by_channel must filter on channel_id ALONE (ignore is_deleted)"
    )
    assert op_name == "EQUAL"


async def test_delete_by_channel_returns_successful_count_on_partial_failure() -> None:
    store, fake = _store_with_stub(successful=7, failed=2, matches=9)

    deleted = await store.delete_by_channel("C1")

    # Returns the SUCCESSFUL count even when some objects failed.
    assert deleted == 7
