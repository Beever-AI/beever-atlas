"""Tests for the shared channel-deletion fan-out service (Wave 2).

delete-channel-v2 Wave 2. Exercises ``services.channel_deletion.purge_channel``
(the full hard-purge) and ``_ordered_store_fanout(mode="reset")`` (the reset
subset) against cross-store fakes — no live Mongo / Weaviate / Neo4j.

Coverage:
  * happy-path purge — every store delete method called, counts aggregated,
    unlinked from all connections, sync + consolidation cancelled, audit
    written, lock released;
  * AC#12 concurrent double-purge — two ``purge_channel`` via ``asyncio.gather``
    → exactly one runs the fan-out via the CAS, one audit record, final lock
    released (relies on ``claim_purge``, NOT a plain upsert);
  * AC#9 re-entrancy — a pre-existing stale lock → purge proceeds, completes,
    lock released;
  * partial failure — one store raises → status="partial", lock RETAINED,
    audit still written;
  * reset regression — ``mode="reset"`` flips messages (not deleted), leaves
    wiki pages + graph :WikiPage untouched.

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import beever_atlas.services.channel_deletion as cd

_CHANNEL_ID = "C-purge-1"


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────


class _FakePurgeLocks:
    """In-memory CAS gate mirroring ``MongoDBStore.claim_purge`` semantics.

    ``claim`` performs an atomic check-and-set with NO internal ``await`` — so
    under ``asyncio.gather`` exactly one of two interleaved coroutines wins the
    lock, exactly as the Mongo ``find_one_and_update`` CAS does. A plain
    upsert (no CAS) would let BOTH win; this fake encodes the CAS contract so
    the AC#12 test actually exercises ``claim_purge``.
    """

    def __init__(self) -> None:
        # channel_id -> True when held by a fresh (non-stale) lock.
        self._held: set[str] = set()
        self.claim_calls = 0
        self.release_calls = 0

    def seed_stale_lock(self, channel_id: str) -> None:
        """A stale lock is treated as not-held: the next claim reclaims it."""
        # Stale == reclaimable, so for the fake we simply do NOT mark it held;
        # the claim path returns True (reclaim) which is the observable contract.
        self._held.discard(channel_id)

    async def claim_purge(
        self,
        channel_id: str,
        *,
        stale_after_s: float = 900.0,
        owner_principal_id: str | None = None,
    ) -> bool:
        self.claim_calls += 1
        # Atomic check-and-set — no await between the read and the write, so
        # under ``asyncio.gather`` exactly one of two interleaved coroutines
        # observes "not held" and wins (mirrors the Mongo find_one_and_update
        # CAS). The winner then yields ONCE while holding the lock so a racing
        # loser is scheduled and observes the held lock — this reproduces the
        # real contention window where the loser claims while the winner is
        # still mid-fan-out (in production every store await is such a window).
        if channel_id in self._held:
            return False
        self._held.add(channel_id)
        await asyncio.sleep(0)
        return True

    async def release_purge(self, channel_id: str) -> None:
        self.release_calls += 1
        self._held.discard(channel_id)

    def is_held(self, channel_id: str) -> bool:
        return channel_id in self._held


def _make_stores(locks: _FakePurgeLocks | None = None) -> SimpleNamespace:
    """Build a SimpleNamespace stores stub wired with cross-store fakes."""
    locks = locks or _FakePurgeLocks()

    mongodb = SimpleNamespace()
    mongodb.claim_purge = locks.claim_purge
    mongodb.release_purge = locks.release_purge
    mongodb.delete_channel_policy = AsyncMock(return_value=True)
    mongodb.purge_channel = AsyncMock(
        return_value={
            "channel_messages": 9,
            "imported_messages": 0,
            "activity_events": 3,
            "write_intents": 1,
            "pipeline_checkpoints": 2,
            "channel_sync_state": 1,
            "sync_jobs": 4,
        }
    )
    mongodb.log_channel_purge_audit = AsyncMock(return_value=None)
    # Reset-only surface (db["channel_messages"].update_many).
    channel_messages_coll = SimpleNamespace(
        update_many=AsyncMock(return_value=SimpleNamespace(modified_count=7)),
    )
    mongodb.db = {"channel_messages": channel_messages_coll}
    mongodb.clear_channel_sync_state = AsyncMock(return_value=None)
    # Stash the lock helper for assertions.
    mongodb._locks = locks  # type: ignore[attr-defined]

    graph = SimpleNamespace(
        delete_channel_data=AsyncMock(
            return_value={
                "events_deleted": 12,
                "media_deleted": 5,
                "entities_deleted": 7,
            }
        ),
        delete_channel_wiki_graph=AsyncMock(return_value=3),
    )
    weaviate = SimpleNamespace(delete_by_channel=AsyncMock(return_value=42))
    qa_history = SimpleNamespace(delete_by_channel=AsyncMock(return_value=6))
    chat_history = SimpleNamespace(delete_by_channel=AsyncMock(return_value=8))

    # Two connections — one linking the channel, one not — so unlink touches
    # only the linker.
    conn_a = SimpleNamespace(id="conn-a", selected_channels=[_CHANNEL_ID, "C-other"])
    conn_b = SimpleNamespace(id="conn-b", selected_channels=["C-unrelated"])
    platform = SimpleNamespace(
        list_connections=AsyncMock(return_value=[conn_a, conn_b]),
        update_connection=AsyncMock(return_value=None),
    )

    return SimpleNamespace(
        mongodb=mongodb,
        graph=graph,
        weaviate=weaviate,
        qa_history=qa_history,
        chat_history=chat_history,
        platform=platform,
    )


class _FakePageStore:
    """Callable WikiPageStore fake — ``WikiPageStore(db=...)`` returns self."""

    def __init__(self, returns: int = 4) -> None:
        self._returns = returns
        self.all_langs_calls: list[str] = []

    def __call__(self, db: Any = None) -> "_FakePageStore":
        return self

    async def delete_all_for_channel_all_langs(self, channel_id: str) -> int:
        self.all_langs_calls.append(channel_id)
        return self._returns


class _FakeSyncRunner:
    def __init__(self) -> None:
        self.cancel_calls: list[str] = []

    async def cancel_sync(self, channel_id: str) -> bool:
        self.cancel_calls.append(channel_id)
        return True


@pytest.fixture
def patched(monkeypatch):
    """Patch ``get_stores`` + every lazy import the purge fan-out reaches.

    Returns the ``stores`` stub plus the fakes the tests assert against.
    """
    locks = _FakePurgeLocks()
    stores = _make_stores(locks)
    monkeypatch.setattr(cd, "get_stores", lambda: stores)

    # Lazy import: ``from beever_atlas.api.sync import get_sync_runner``.
    runner = _FakeSyncRunner()
    import beever_atlas.api.sync as sync_mod

    monkeypatch.setattr(sync_mod, "get_sync_runner", lambda: runner)

    # Lazy import: ``from beever_atlas.services.pipeline_orchestrator import
    # cancel_consolidation``.
    cancel_consolidation = AsyncMock(return_value=True)
    import beever_atlas.services.pipeline_orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "cancel_consolidation", cancel_consolidation)

    # Lazy import: ``from beever_atlas.services.scheduler import get_scheduler``.
    scheduler = SimpleNamespace(deregister_channel_jobs=AsyncMock(return_value=None))
    import beever_atlas.services.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "get_scheduler", lambda: scheduler)

    # Lazy import: ``from beever_atlas.wiki.page_store import WikiPageStore``.
    page_store = _FakePageStore(returns=4)
    import beever_atlas.wiki.page_store as page_store_mod

    monkeypatch.setattr(page_store_mod, "WikiPageStore", page_store)

    return SimpleNamespace(
        stores=stores,
        locks=locks,
        runner=runner,
        cancel_consolidation=cancel_consolidation,
        scheduler=scheduler,
        page_store=page_store,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Happy-path purge
# ─────────────────────────────────────────────────────────────────────────────


async def test_purge_happy_path_fans_out_to_every_store(patched) -> None:
    result = await cd.purge_channel(_CHANNEL_ID, principal_id="user:alice")

    assert result["status"] == "completed"
    assert result["errors"] == {}
    assert result["channel_id"] == _CHANNEL_ID
    assert result["sync_cancelled"] is True
    assert isinstance(result["purge_run_id"], str) and result["purge_run_id"]

    s = patched.stores
    # Cancellation (best-effort, process-local).
    assert patched.runner.cancel_calls == [_CHANNEL_ID]
    patched.cancel_consolidation.assert_awaited_once_with(_CHANNEL_ID)

    # Unlinked only from the connection that linked the channel; conn-a's
    # remaining channels keep C-other.
    assert result["unlinked_from"] == ["conn-a"]
    s.platform.update_connection.assert_awaited_once_with("conn-a", selected_channels=["C-other"])

    # Policy delete + scheduler de-reg (order: policy first).
    s.mongodb.delete_channel_policy.assert_awaited_once_with(_CHANNEL_ID)
    patched.scheduler.deregister_channel_jobs.assert_awaited_once_with(_CHANNEL_ID)

    # Graph (data + wiki nodes), Weaviate facts + QA, wiki pages, chat history.
    s.graph.delete_channel_data.assert_awaited_once_with(_CHANNEL_ID)
    s.graph.delete_channel_wiki_graph.assert_awaited_once_with(_CHANNEL_ID)
    s.weaviate.delete_by_channel.assert_awaited_once_with(_CHANNEL_ID)
    s.qa_history.delete_by_channel.assert_awaited_once_with(_CHANNEL_ID)
    assert patched.page_store.all_langs_calls == [_CHANNEL_ID]
    s.chat_history.delete_by_channel.assert_awaited_once_with(_CHANNEL_ID)
    s.mongodb.purge_channel.assert_awaited_once_with(_CHANNEL_ID)

    # Counts aggregated across stores (graph + weaviate + qa + wiki + chat +
    # mongo aggregator).
    counts = result["counts"]
    assert counts["events_deleted"] == 12
    assert counts["media_deleted"] == 5
    assert counts["entities_deleted"] == 7
    assert counts["wiki_graph_deleted"] == 3
    assert counts["weaviate_deleted"] == 42
    assert counts["qa_history_deleted"] == 6
    assert counts["wiki_pages_deleted"] == 4
    assert counts["chat_history_deleted"] == 8
    assert counts["channel_policy_deleted"] == 1
    # Merged-in mongo aggregator counts.
    assert counts["channel_messages"] == 9
    assert counts["activity_events"] == 3
    assert counts["sync_jobs"] == 4

    # Audit written with the full payload, BEFORE release.
    s.mongodb.log_channel_purge_audit.assert_awaited_once()
    audit_kwargs = s.mongodb.log_channel_purge_audit.call_args.kwargs
    assert audit_kwargs["channel_id"] == _CHANNEL_ID
    assert audit_kwargs["principal_id"] == "user:alice"
    assert audit_kwargs["counts"] == counts
    assert audit_kwargs["errors"] == {}
    assert audit_kwargs["unlinked_from"] == ["conn-a"]
    assert audit_kwargs["purge_run_id"] == result["purge_run_id"]

    # Clean run → lock released, no longer held.
    assert patched.locks.release_calls == 1
    assert not patched.locks.is_held(_CHANNEL_ID)


# ─────────────────────────────────────────────────────────────────────────────
# AC#12 — concurrent double-purge (CAS)
# ─────────────────────────────────────────────────────────────────────────────


async def test_concurrent_double_purge_one_runs_fanout(patched) -> None:
    """Two ``purge_channel`` for the same channel via ``asyncio.gather``:
    exactly one runs the fan-out (CAS winner), the other returns
    ``already_in_progress`` with NO store calls; one audit record; lock
    released at the end. Relies on ``claim_purge`` — a plain upsert would let
    both win.
    """
    r1, r2 = await asyncio.gather(
        cd.purge_channel(_CHANNEL_ID, principal_id="user:alice"),
        cd.purge_channel(_CHANNEL_ID, principal_id="reaper"),
    )

    statuses = sorted([r1["status"], r2["status"]])
    assert statuses == ["already_in_progress", "completed"]

    # Exactly one audit record (only the winner audits).
    assert patched.stores.mongodb.log_channel_purge_audit.await_count == 1

    # The loser never touched a store.
    s = patched.stores
    s.graph.delete_channel_data.assert_awaited_once_with(_CHANNEL_ID)
    s.mongodb.purge_channel.assert_awaited_once_with(_CHANNEL_ID)

    # Final state: lock released (NOT stuck purging).
    assert not patched.locks.is_held(_CHANNEL_ID)
    assert patched.locks.release_calls == 1


# ─────────────────────────────────────────────────────────────────────────────
# AC#9 — re-entrancy after a stale lock
# ─────────────────────────────────────────────────────────────────────────────


async def test_purge_reenters_after_stale_lock(patched) -> None:
    """A pre-existing STALE lock is reclaimable: the purge proceeds, completes,
    and releases the lock (the reaper path)."""
    patched.locks.seed_stale_lock(_CHANNEL_ID)

    result = await cd.purge_channel(_CHANNEL_ID, principal_id="reaper")

    assert result["status"] == "completed"
    assert result["errors"] == {}
    # Fan-out ran (stale lock did not block re-entry).
    patched.stores.mongodb.purge_channel.assert_awaited_once_with(_CHANNEL_ID)
    patched.stores.mongodb.log_channel_purge_audit.assert_awaited_once()
    # Lock released on the clean re-run.
    assert not patched.locks.is_held(_CHANNEL_ID)


# ─────────────────────────────────────────────────────────────────────────────
# Partial failure — one store raises
# ─────────────────────────────────────────────────────────────────────────────


async def test_purge_partial_failure_retains_lock_and_audits(patched) -> None:
    """One store raising → status='partial', remaining stages still run, audit
    STILL written, lock RETAINED (the reaper re-runs)."""
    patched.stores.weaviate.delete_by_channel = AsyncMock(side_effect=RuntimeError("weaviate down"))

    result = await cd.purge_channel(_CHANNEL_ID, principal_id="user:alice")

    assert result["status"] == "partial"
    assert "weaviate_delete_by_channel" in result["errors"]
    assert "weaviate down" in result["errors"]["weaviate_delete_by_channel"]
    # No weaviate count, but other stages succeeded.
    assert "weaviate_deleted" not in result["counts"]
    assert result["counts"]["entities_deleted"] == 7
    assert result["counts"]["chat_history_deleted"] == 8

    # Audit still written despite the failure.
    patched.stores.mongodb.log_channel_purge_audit.assert_awaited_once()
    audit_kwargs = patched.stores.mongodb.log_channel_purge_audit.call_args.kwargs
    assert "weaviate_delete_by_channel" in audit_kwargs["errors"]

    # Lock RETAINED (not released) so the reaper re-runs.
    assert patched.locks.release_calls == 0
    assert patched.locks.is_held(_CHANNEL_ID)


async def test_imported_messages_error_sentinel_makes_partial_and_retains_lock(
    patched,
) -> None:
    """The Mongo aggregator swallows a legacy ``imported_messages`` delete
    failure but flags it via the ``imported_messages_error`` sentinel. The
    fan-out must promote that sentinel into ``errors`` so the run is reported
    'partial' and the lock is RETAINED — otherwise a lone legacy-delete failure
    would silently release the lock and the reaper would never retry."""
    patched.stores.mongodb.purge_channel = AsyncMock(
        return_value={
            "channel_messages": 9,
            "imported_messages": 0,
            "imported_messages_error": 1,  # legacy delete failed (swallowed)
            "activity_events": 3,
            "channel_sync_state": 1,
            "sync_jobs": 4,
        }
    )

    result = await cd.purge_channel(_CHANNEL_ID, principal_id="user:alice")

    assert result["status"] == "partial"
    assert result["errors"].get("imported_messages") == "legacy delete failed"

    # Audit still written, carrying the promoted error.
    patched.stores.mongodb.log_channel_purge_audit.assert_awaited_once()
    audit_kwargs = patched.stores.mongodb.log_channel_purge_audit.call_args.kwargs
    assert "imported_messages" in audit_kwargs["errors"]

    # Lock RETAINED (not released) so the reaper re-runs to clean up the
    # surviving legacy rows.
    assert patched.locks.release_calls == 0
    assert patched.locks.is_held(_CHANNEL_ID)


# ─────────────────────────────────────────────────────────────────────────────
# AC#11 — reset subset (regression)
# ─────────────────────────────────────────────────────────────────────────────


async def test_reset_mode_flips_messages_and_skips_wiki(patched) -> None:
    """``mode='reset'`` flips messages to pending (NOT deleted), drops graph
    data + Weaviate facts + sync state, and SKIPS wiki pages, graph :WikiPage,
    QA history, chat history, connection unlink, scheduler de-reg, and the
    purge lock entirely."""
    out = await cd._ordered_store_fanout(_CHANNEL_ID, mode="reset", principal_id="admin:reset")

    # Reset return shape: results dict + errors LIST (admin's contract).
    assert isinstance(out["results"], dict)
    assert isinstance(out["errors"], list)
    assert out["errors"] == []
    assert out["sync_cancelled"] is False

    results = out["results"]
    assert results["events_deleted"] == 12
    assert results["weaviate_deleted"] == 42
    assert results["sync_state_cleared"] == 1
    assert results["messages_marked_pending"] == 7

    s = patched.stores
    # Messages FLIPPED, not deleted — update_many with the pending flip.
    update_call = s.mongodb.db["channel_messages"].update_many.call_args
    assert update_call.args[0] == {"channel_id": _CHANNEL_ID}
    set_doc = update_call.args[1]["$set"]
    assert set_doc["extraction_status"] == "pending"
    assert set_doc["attempt_count"] == 0
    assert "next_attempt_at" in set_doc
    assert update_call.args[1]["$unset"] == {"extraction_error": ""}
    # purge_channel (the delete aggregator) is NEVER called in reset mode.
    s.mongodb.purge_channel.assert_not_called()

    # Wiki preserved: WikiPageStore + graph :WikiPage NOT touched.
    assert patched.page_store.all_langs_calls == []
    s.graph.delete_channel_wiki_graph.assert_not_called()
    # Chat history + QA history NOT touched in reset.
    s.chat_history.delete_by_channel.assert_not_called()
    s.qa_history.delete_by_channel.assert_not_called()
    # No unlink / de-reg / lock in reset.
    s.platform.update_connection.assert_not_called()
    assert patched.locks.claim_calls == 0
    assert patched.locks.release_calls == 0
