"""Shared ordered cross-store fan-out for channel hard-purge + reset.

delete-channel-v2 Wave 2. This module is the single ordered fan-out that both
the full hard-purge (``mode="purge"``) and the per-channel reset
(``mode="reset"``) drive, so the two destructive paths can never drift apart in
ordering. The atomically-claimed purge lock (Wave 0) gates the purge path; the
reaper (Wave 0) re-invokes :func:`purge_channel` for stale locks.

Two public surfaces:

* :func:`purge_channel` — the full hard-purge. Claims the durable purge lock
  via CAS (re-entrant + concurrency-safe), runs the whole fan-out, writes a
  retained audit record, and releases the lock only on a clean run.
* :func:`_ordered_store_fanout` — the shared ordering primitive. ``mode="reset"``
  runs the reset *subset* (flip messages to pending instead of deleting; skip
  wiki + chat-history; no lock) and returns the exact ``results``/``errors``
  shape ``api.admin.reset_channel_data`` already exposes, so reset's observable
  behaviour does not regress. ``mode="purge"`` runs the full destructive
  sequence.

Lazy imports: anything in the ``api`` layer (``get_sync_runner`` from
``api.sync``) is imported inside the function to avoid a ``services → api``
circular import — the established precedent (``admin.py:742``,
``reconciler.py:59``). Stores are reached via the standard ``get_stores()``
accessor.

Ordering (purge), each stage isolated in its own try/except so one store
failure never aborts the rest of the purge:

    0. claim_purge (CAS gate)        — abort with already_in_progress if lost
    1. cancel_sync + cancel_consolidation (best-effort, process-local)
    2. unlink from every connection's selected_channels
    3. delete policy, THEN deregister scheduler jobs (order: policy first)
    4. graph.delete_channel_data
    5. graph.delete_channel_wiki_graph
    6. weaviate.delete_by_channel
    7. qa_history.delete_by_channel
    8. WikiPageStore.delete_all_for_channel_all_langs
    9. chat_history.delete_by_channel
   10. mongodb.purge_channel (messages/checkpoints/activity/intents + sync state)
   11. media_blob_store.delete_by_channel (durable channel media blobs + refs)
   12. audit (retained log)
   13. release_purge on a clean run; retain the lock for the reaper on errors
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from beever_atlas.stores import get_stores
from beever_atlas.stores.mongodb_store import PURGE_LOCK_STALE_AFTER_S

logger = logging.getLogger(__name__)


async def purge_channel(channel_id: str, *, principal_id: str) -> dict:
    """Hard-purge every store's data for ``channel_id`` behind the purge lock.

    Re-entrant and concurrency-safe: the CAS :meth:`MongoDBStore.claim_purge`
    ensures exactly one caller runs the destructive fan-out even when a user
    re-click and the reaper (or two EE workers) race. The loser returns
    ``{"status": "already_in_progress"}`` and does NOT touch any store.

    Returns a dict::

        {
            "channel_id": str,
            "counts": {<stage>: int, ...},   # aggregated per-store counts
            "errors": {<stage>: str, ...},   # empty iff every stage succeeded
            "unlinked_from": [connection_id, ...],
            "sync_cancelled": bool,
            "purge_run_id": str,             # UUID — distinguishes reaper re-runs
            "status": "completed" | "partial" | "already_in_progress",
        }

    ``status == "completed"`` iff ``errors == {}`` (the lock is then released).
    ``status == "partial"`` means at least one stage failed; the lock is
    RETAINED so the reaper re-runs the purge to convergence. The audit record
    is written either way (before the release) so a crash after audit / before
    release simply leaves the lock for the reaper. Wave 3 maps "completed" →
    HTTP 200 and "partial" → HTTP 207.
    """
    stores = get_stores()
    purge_run_id = str(uuid4())

    # 0. Atomically claim the purge lock. If another (fresh) purge already
    #    holds it, abort without any fan-out — this is the CAS that prevents a
    #    concurrent double-purge from bricking the channel (AC#12) and makes
    #    the reaper re-invocation safe (AC#9 re-entrancy via stale reclaim).
    claimed = await stores.mongodb.claim_purge(
        channel_id,
        stale_after_s=PURGE_LOCK_STALE_AFTER_S,
        owner_principal_id=principal_id,
    )
    if not claimed:
        logger.info(
            "purge_channel: lock already held for channel=%s — aborting (no fan-out)",
            channel_id,
        )
        return {"channel_id": channel_id, "status": "already_in_progress"}

    fanout = await _ordered_store_fanout(channel_id, mode="purge", principal_id=principal_id)
    counts: dict[str, int] = fanout["counts"]
    errors: dict[str, str] = fanout["errors"]
    unlinked_from: list[str] = fanout["unlinked_from"]
    sync_cancelled: bool = fanout["sync_cancelled"]

    # 12. Audit — retained record of the run, BEFORE the lock release so a
    #     crash in between leaves the lock for the reaper. Audit failure itself
    #     is recorded into ``errors`` (and therefore retains the lock) but never
    #     raises out of the purge.
    try:
        await stores.mongodb.log_channel_purge_audit(
            channel_id=channel_id,
            principal_id=principal_id,
            counts=counts,
            errors=errors,
            unlinked_from=unlinked_from,
            purge_run_id=purge_run_id,
            ts=datetime.now(tz=UTC),
        )
    except Exception as exc:  # noqa: BLE001 — audit failure must not crash purge
        errors["audit"] = str(exc)
        logger.warning(
            "purge_channel: audit write failed channel=%s run=%s: %s",
            channel_id,
            purge_run_id,
            exc,
        )

    # 13. Release the lock ONLY on a fully clean run. Any error → retain the
    #     lock so the reaper re-runs the purge to convergence.
    status = "completed" if not errors else "partial"
    if not errors:
        try:
            await stores.mongodb.release_purge(channel_id)
        except Exception as exc:  # noqa: BLE001
            # Could not release a clean lock — treat as partial so the reaper
            # eventually clears it; the data is already gone (idempotent re-run).
            errors["release_purge"] = str(exc)
            status = "partial"
            logger.warning(
                "purge_channel: release_purge failed channel=%s: %s",
                channel_id,
                exc,
            )

    logger.warning(
        "CHANNEL PURGE channel=%s principal=%s run=%s status=%s counts=%s "
        "errors=%s unlinked_from=%s",
        channel_id,
        principal_id,
        purge_run_id,
        status,
        counts,
        errors,
        unlinked_from,
    )

    return {
        "channel_id": channel_id,
        "counts": counts,
        "errors": errors,
        "unlinked_from": unlinked_from,
        "sync_cancelled": sync_cancelled,
        "purge_run_id": purge_run_id,
        "status": status,
    }


async def _ordered_store_fanout(
    channel_id: str,
    *,
    mode: Literal["purge", "reset"],
    principal_id: str,
) -> dict[str, Any]:
    """Run the shared ordered cross-store fan-out for ``channel_id``.

    The single source of truth for stage ordering, shared by the hard-purge
    and the per-channel reset so they can never drift. ``mode`` selects the
    subset and the return shape:

    ``mode="purge"`` — the full destructive sequence (steps 1-10 above; the
    lock + audit + release are owned by :func:`purge_channel`). Returns::

        {"counts": dict[str, int], "errors": dict[str, str],
         "unlinked_from": list[str], "sync_cancelled": bool}

    ``mode="reset"`` — the reset SUBSET, which reproduces the exact observable
    behaviour of ``api.admin.reset_channel_data``'s store fan-out:
        (i)   FLIP ``channel_messages`` to ``extraction_status="pending"``,
              ``next_attempt_at=now``, ``attempt_count=0``, unset
              ``extraction_error`` — NOT delete (messages are the source of
              truth);
        (ii)  SKIP wiki deletion (graph wiki nodes + WikiPageStore pages);
        (iii) SKIP chat-history delete;
        (iv)  does NOT touch the purge lock (reset is not destructive of the
              channel's identity — the caller keeps its 409-on-running-sync
              gate);
        (v)   does NOT unlink connections or deregister scheduler jobs.
    It runs graph.delete_channel_data + weaviate.delete_by_channel +
    clear_channel_sync_state + the message-flip, and returns the SAME keyed
    ``results``/``errors`` shape admin's reset already exposes::

        {"results": dict[str, int], "errors": list[str],
         "sync_cancelled": bool}

    The divergent return shapes are intentional: reset's error strings + result
    keys are asserted by ``tests/api/test_admin_channel_reset.py`` and surfaced
    to operators, so the shared primitive emits each mode's existing contract
    verbatim rather than forcing one shape and regressing the other.
    """
    stores = get_stores()

    if mode == "reset":
        return await _reset_fanout(channel_id, stores)
    return await _purge_fanout(channel_id, stores, principal_id=principal_id)


async def _reset_fanout(channel_id: str, stores: Any) -> dict[str, Any]:
    """Reset subset of the fan-out — see :func:`_ordered_store_fanout`.

    Mirrors ``api.admin.reset_channel_data`` stages 1-4 verbatim (same order,
    same ``results`` keys, same ``errors`` strings) so delegating reset to this
    primitive is behaviour-preserving.
    """
    results: dict[str, int] = {}
    errors: list[str] = []

    # 1. Drop derived graph data (Events, Media, channel-scoped Entities, plus
    #    orphan-global Entity cleanup). Wiki :WikiPage nodes are deliberately
    #    NOT touched here (reset preserves the versioned wiki).
    try:
        graph_counts = await stores.graph.delete_channel_data(channel_id)
        results["events_deleted"] = int(graph_counts.get("events_deleted", 0) or 0)
        results["media_deleted"] = int(graph_counts.get("media_deleted", 0) or 0)
        results["entities_deleted"] = int(graph_counts.get("entities_deleted", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        errors.append("delete_channel_data failed")
        logger.warning(
            "reset fan-out: delete_channel_data failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 2. Drop Weaviate facts + clusters + summaries for the channel.
    try:
        weaviate_n = await stores.weaviate.delete_by_channel(channel_id)
        results["weaviate_deleted"] = int(weaviate_n or 0)
    except Exception as exc:  # noqa: BLE001
        errors.append("weaviate.delete_by_channel failed")
        logger.warning(
            "reset fan-out: weaviate.delete_by_channel failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 3. Drop sync state (MongoDB). Run before the message-state flip so any
    #    in-flight worker that observes the half-dropped state has nothing to
    #    re-anchor to — the next sync auto-resolves to ``full``.
    try:
        await stores.mongodb.clear_channel_sync_state(channel_id)
        results["sync_state_cleared"] = 1
    except Exception as exc:  # noqa: BLE001
        errors.append("clear_channel_sync_state failed")
        logger.warning(
            "reset fan-out: clear_channel_sync_state failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 4. Flip every preserved ``channel_messages`` row back to
    #    ``extraction_status='pending'`` so the next sync re-extracts.
    #    ``next_attempt_at`` MUST be set (not unset) — the worker's claim filter
    #    is ``{"next_attempt_at": {"$lte": now}}`` and a missing field never
    #    satisfies ``$lte``. Messages themselves are preserved.
    try:
        now_utc = datetime.now(tz=UTC)
        result = await stores.mongodb.db["channel_messages"].update_many(
            {"channel_id": channel_id},
            {
                "$set": {
                    "extraction_status": "pending",
                    "next_attempt_at": now_utc,
                    "attempt_count": 0,
                },
                "$unset": {"extraction_error": ""},
            },
        )
        results["messages_marked_pending"] = int(result.modified_count)
    except Exception as exc:  # noqa: BLE001
        errors.append("reset_extraction_status failed")
        logger.warning(
            "reset fan-out: extraction_status reset failed channel=%s: %s",
            channel_id,
            exc,
        )

    return {"results": results, "errors": errors, "sync_cancelled": False}


async def _purge_fanout(channel_id: str, stores: Any, *, principal_id: str) -> dict[str, Any]:
    """Full destructive subset of the fan-out — see :func:`_ordered_store_fanout`.

    Steps 1-10. Each stage is isolated in its own try/except: an error is
    recorded in ``errors[stage]`` and the remaining stages still run, so a
    single store hiccup never aborts the purge (the reaper re-runs to
    convergence on any retained error).
    """
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    unlinked_from: list[str] = []

    # 1. Best-effort process-local cancellation of any in-flight sync +
    #    consolidation. The durable lock + writer guards are the cross-process
    #    guarantee; this just stops same-process tasks sooner.
    sync_cancelled = False
    try:
        from beever_atlas.api.sync import get_sync_runner  # lazy: avoid svc→api cycle

        sync_runner = get_sync_runner()
        sync_cancelled = await sync_runner.cancel_sync(channel_id)
    except Exception as exc:  # noqa: BLE001
        errors["cancel_sync"] = str(exc)
        logger.warning("purge fan-out: cancel_sync failed channel=%s: %s", channel_id, exc)
    try:
        from beever_atlas.services.pipeline_orchestrator import cancel_consolidation

        await cancel_consolidation(channel_id)
    except Exception as exc:  # noqa: BLE001
        errors["cancel_consolidation"] = str(exc)
        logger.warning(
            "purge fan-out: cancel_consolidation failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 2. Unlink from every connection's ``selected_channels`` so a partial
    #    purge cannot self-heal into a live channel via a scheduler timer.
    try:
        connections = await stores.platform.list_connections()
        to_unlink = [c for c in connections if channel_id in (c.selected_channels or [])]
        # Concurrent updates: on large (EE) deployments the matching set can be
        # more than one, and sequential awaits would add latency toward the
        # stale-lock threshold. gather keeps it bounded; any one failure raises
        # and is caught below (the reaper re-runs the purge).
        await asyncio.gather(
            *(
                stores.platform.update_connection(
                    conn.id,
                    selected_channels=[
                        c for c in (conn.selected_channels or []) if c != channel_id
                    ],
                )
                for conn in to_unlink
            )
        )
        unlinked_from.extend(conn.id for conn in to_unlink)
    except Exception as exc:  # noqa: BLE001
        errors["unlink_connections"] = str(exc)
        logger.warning(
            "purge fan-out: unlink connections failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 3. Delete the channel policy, THEN deregister the scheduler timers. Order
    #    matters: deregistering first then deleting could leave a window where a
    #    policy still exists to re-register a job. Policy delete first closes it.
    try:
        await stores.mongodb.delete_channel_policy(channel_id)
        counts["channel_policy_deleted"] = 1
    except Exception as exc:  # noqa: BLE001
        errors["delete_channel_policy"] = str(exc)
        logger.warning(
            "purge fan-out: delete_channel_policy failed channel=%s: %s",
            channel_id,
            exc,
        )
    try:
        from beever_atlas.services.scheduler import get_scheduler

        scheduler = get_scheduler()
        if scheduler is not None:
            await scheduler.deregister_channel_jobs(channel_id)
    except Exception as exc:  # noqa: BLE001
        errors["deregister_channel_jobs"] = str(exc)
        logger.warning(
            "purge fan-out: deregister_channel_jobs failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 4. Graph: channel data (Events, Media, channel-scoped + orphan Entities).
    try:
        graph_counts = await stores.graph.delete_channel_data(channel_id)
        counts["events_deleted"] = int(graph_counts.get("events_deleted", 0) or 0)
        counts["media_deleted"] = int(graph_counts.get("media_deleted", 0) or 0)
        counts["entities_deleted"] = int(graph_counts.get("entities_deleted", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        errors["delete_channel_data"] = str(exc)
        logger.warning(
            "purge fan-out: delete_channel_data failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 5. Graph: :WikiPage nodes (purge-only — reset preserves the wiki).
    try:
        wiki_graph_n = await stores.graph.delete_channel_wiki_graph(channel_id)
        counts["wiki_graph_deleted"] = int(wiki_graph_n or 0)
    except Exception as exc:  # noqa: BLE001
        errors["delete_channel_wiki_graph"] = str(exc)
        logger.warning(
            "purge fan-out: delete_channel_wiki_graph failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 6. Weaviate facts + clusters + summaries.
    try:
        weaviate_n = await stores.weaviate.delete_by_channel(channel_id)
        counts["weaviate_deleted"] = int(weaviate_n or 0)
    except Exception as exc:  # noqa: BLE001
        errors["weaviate_delete_by_channel"] = str(exc)
        logger.warning(
            "purge fan-out: weaviate.delete_by_channel failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 7. Weaviate QA history.
    try:
        qa_n = await stores.qa_history.delete_by_channel(channel_id)
        counts["qa_history_deleted"] = int(qa_n or 0)
    except Exception as exc:  # noqa: BLE001
        errors["qa_history_delete_by_channel"] = str(exc)
        logger.warning(
            "purge fan-out: qa_history.delete_by_channel failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 8. Wiki pages (all languages) — purge-only. Instantiate WikiPageStore
    #    directly (there is no ``stores.wiki``), matching admin.py:452.
    try:
        from beever_atlas.wiki.page_store import WikiPageStore

        page_store = WikiPageStore(db=stores.mongodb.db)
        pages_n = await page_store.delete_all_for_channel_all_langs(channel_id)
        counts["wiki_pages_deleted"] = int(pages_n or 0)
    except Exception as exc:  # noqa: BLE001
        errors["delete_all_for_channel_all_langs"] = str(exc)
        logger.warning(
            "purge fan-out: WikiPageStore.delete_all_for_channel_all_langs failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 8b. Wiki render cache + generation status (all languages) — purge-only.
    #     GET /api/channels/{id}/wiki serves the RENDERED blob from the
    #     ``wiki_cache`` collection (keyed ``{channel_id}:{lang}``), not from
    #     ``wiki_pages`` — so without this the wiki kept being served (HTTP 200)
    #     after a hard delete even though every other store was emptied.
    #     Instantiate WikiCache directly (there is no ``stores.wiki``).
    try:
        from beever_atlas.infra.config import get_settings
        from beever_atlas.wiki.cache import WikiCache

        wiki_cache = WikiCache(get_settings().mongodb_uri)
        cache_n = await wiki_cache.delete_all_for_channel(channel_id)
        counts["wiki_cache_deleted"] = int(cache_n or 0)
    except Exception as exc:  # noqa: BLE001
        errors["wiki_cache_delete_all_for_channel"] = str(exc)
        logger.warning(
            "purge fan-out: WikiCache.delete_all_for_channel failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 9. Chat history (purge-only).
    try:
        chat_n = await stores.chat_history.delete_by_channel(channel_id)
        counts["chat_history_deleted"] = int(chat_n or 0)
    except Exception as exc:  # noqa: BLE001
        errors["chat_history_delete_by_channel"] = str(exc)
        logger.warning(
            "purge fan-out: chat_history.delete_by_channel failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 10. MongoDB aggregator — messages, checkpoints, activity, wiki-ops,
    #     write-intents + clear sync state. Merge its per-collection counts in.
    try:
        mongo_counts = await stores.mongodb.purge_channel(channel_id)
        for key, value in mongo_counts.items():
            counts[key] = int(value or 0)
        # The Mongo aggregator swallows a legacy ``imported_messages`` delete
        # failure (best-effort, so one missing collection can't abort the run)
        # but flags it with a sentinel. Promote that sentinel into ``errors`` so
        # the run is reported "partial" and the purge lock is RETAINED — the
        # reaper then re-runs the purge to clean up the surviving legacy rows.
        if counts.get("imported_messages_error"):
            errors["imported_messages"] = "legacy delete failed"
    except Exception as exc:  # noqa: BLE001
        errors["mongodb_purge_channel"] = str(exc)
        logger.warning(
            "purge fan-out: mongodb.purge_channel failed channel=%s: %s",
            channel_id,
            exc,
        )

    # 11. Durable channel media — drop the GridFS blobs + refs the read-through
    #     proxy serves (purge-only). delete_by_channel NEVER raises (it returns
    #     partial counts), but the call is still isolated so a missing store /
    #     accessor never aborts the run. A None store (feature staged off /
    #     pre-migration) is skipped without counting as an error.
    media_store = getattr(stores, "media_blob_store", None)
    if media_store is None:
        logger.debug(
            "purge fan-out: media_blob_store absent — skipping media purge channel=%s",
            channel_id,
        )
    else:
        try:
            media_counts = await media_store.delete_by_channel(channel_id)
            counts["channel_media_blobs"] = int(media_counts.get("blobs_deleted", 0) or 0)
            counts["channel_media_refs"] = int(media_counts.get("refs_deleted", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            errors["media_blob_store_delete_by_channel"] = str(exc)
            logger.warning(
                "purge fan-out: media_blob_store.delete_by_channel failed channel=%s: %s",
                channel_id,
                exc,
            )

    return {
        "counts": counts,
        "errors": errors,
        "unlinked_from": unlinked_from,
        "sync_cancelled": sync_cancelled,
    }
