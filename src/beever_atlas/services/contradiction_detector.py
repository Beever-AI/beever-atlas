"""Contradiction detection service for temporal fact lifecycle.

Compares newly extracted facts against existing facts to detect
contradictions and trigger fact supersession via ADK agent.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from beever_atlas.infra.config import get_settings
from beever_atlas.models import AtomicFact

logger = logging.getLogger(__name__)


# P0-1 (pipeline-cost-latency-reduction-v2): epoch sentinel used when a
# channel_sync_state row predates the ``contradiction_watermark`` field.
# Treating missing rows as 1970-01-01 means the first post-deploy
# ``check_and_supersede_for_channel`` call processes every fact ever
# written for that channel, then advances the watermark forward.
_EPOCH_WATERMARK = datetime(1970, 1, 1, tzinfo=UTC)


async def detect_contradictions(
    new_fact: AtomicFact,
    existing_facts: list[AtomicFact],
) -> list[dict[str, Any]]:
    """Compare a new fact against existing facts for contradictions.

    Returns list of dicts with ``existing_fact_id``, ``confidence``, ``reason``.
    """
    if not existing_facts:
        return []

    # Format facts for the prompt
    new_fact_text = (
        f"Text: {new_fact.memory_text} | Topics: {new_fact.topic_tags} | "
        f"Entities: {new_fact.entity_tags}"
    )
    existing_lines: list[str] = []
    for ef in existing_facts:
        existing_lines.append(
            f"- ID: {ef.id} | Text: {ef.memory_text} | "
            f"Topics: {ef.topic_tags} | Entities: {ef.entity_tags}"
        )

    try:
        from beever_atlas.agents.ingestion.contradiction_detector import (
            create_contradiction_detector,
        )
        from beever_atlas.agents.runner import run_agent

        agent = create_contradiction_detector()
        state = await run_agent(
            agent,
            state={
                "new_fact": new_fact_text,
                "existing_facts": "\n".join(existing_lines),
            },
        )

        report = state.get("contradiction_report") or {}
        return report.get("contradictions") or [] if isinstance(report, dict) else []

    except Exception:
        logger.warning(
            "ContradictionDetector: agent call failed",
            exc_info=True,
        )
        return []


async def check_and_supersede(
    new_facts: list[AtomicFact],
    channel_id: str,
) -> None:
    """Check new facts for contradictions against existing facts and apply supersession.

    For each new fact:
    1. Query existing facts with overlapping entity/topic tags
    2. Run contradiction detection
    3. Auto-supersede at confidence >= threshold
    4. Flag potential contradictions at lower confidence
    """
    from beever_atlas.stores import get_stores
    from beever_atlas.models import MemoryFilters

    settings = get_settings()
    stores = get_stores()
    sem = asyncio.Semaphore(settings.contradiction_concurrency)

    # P0: Fast-path first-sync. If the channel has fewer than ~5 existing
    # facts in Weaviate, every per-fact query below would return ~empty
    # candidates anyway, and the ~30 facts/batch × 24 batches = ~720
    # Weaviate round-trips become pure overhead. Skip the whole loop.
    # This is the dominant cost on first-sync, identified by three
    # independent OMC agents reviewing the slow tech-vnote log.
    try:
        sample = await stores.weaviate.list_facts(
            channel_id=channel_id,
            filters=MemoryFilters(),
            limit=5,
        )
        if not sample.memories:
            logger.info(
                "ContradictionDetector: skipping (channel %s has no existing facts — first-sync fast path)",
                channel_id,
            )
            return
    except Exception:  # noqa: BLE001
        # If the probe fails, fall through to the full check loop —
        # we'd rather do extra work than skip contradiction detection
        # in error paths.
        pass

    async def _check_one(new_fact: AtomicFact) -> None:
        if not new_fact.entity_tags and not new_fact.topic_tags:
            return
        async with sem:
            try:
                existing: list[AtomicFact] = []
                if new_fact.entity_tags:
                    result = await stores.weaviate.list_facts(
                        channel_id=channel_id,
                        filters=MemoryFilters(entity=new_fact.entity_tags[0]),
                        limit=10,
                    )
                    existing.extend(result.memories)

                # Deduplicate and exclude self
                seen_ids: set[str] = set()
                candidates: list[AtomicFact] = []
                for ef in existing:
                    if ef.id != new_fact.id and ef.id not in seen_ids and ef.invalid_at is None:
                        seen_ids.add(ef.id)
                        candidates.append(ef)

                if not candidates:
                    return

                contradictions = await detect_contradictions(new_fact, candidates[:10])

                for contradiction in contradictions:
                    fact_id = contradiction.get("existing_fact_id", "")
                    confidence = float(contradiction.get("confidence", 0))

                    if confidence >= settings.contradiction_confidence_threshold:
                        await stores.weaviate.supersede_fact(
                            old_fact_id=fact_id,
                            new_fact_id=new_fact.id,
                        )
                        logger.info(
                            "ContradictionDetector: superseded fact %s with %s (confidence=%.2f)",
                            fact_id,
                            new_fact.id,
                            confidence,
                        )
                    elif confidence >= settings.contradiction_flag_threshold:
                        await stores.weaviate.flag_potential_contradiction(new_fact.id)
                        logger.info(
                            "ContradictionDetector: flagged potential contradiction on %s (confidence=%.2f)",
                            new_fact.id,
                            confidence,
                        )

            except Exception:
                logger.warning(
                    "ContradictionDetector: check failed for fact %s",
                    new_fact.id,
                    exc_info=True,
                )

    await asyncio.gather(*(_check_one(f) for f in new_facts), return_exceptions=True)


async def check_and_supersede_for_channel(
    channel_id: str,
    watermark_ts: datetime | None = None,
) -> int:
    """Bulk post-sync contradiction check for ``channel_id``.

    P0-1 (pipeline-cost-latency-reduction-v2): replaces the previous
    per-batch detached check with a single bulk pass triggered by
    ``memory_settled``. Processes all facts created after the channel's
    persisted ``contradiction_watermark`` (defaulting to the epoch when
    the row predates the field), runs the existing per-fact supersession
    logic via :func:`check_and_supersede`, and advances the watermark
    atomically.

    Atomicity: the watermark advancement uses ``find_one_and_update``
    with a ``$lte`` filter on ``pre_check_watermark`` so two concurrent
    ``memory_settled`` callbacks cannot double-advance — whichever
    invocation runs second observes ``result is None`` and returns
    without raising, the work has already been performed.

    Args:
        channel_id: Channel whose new facts should be checked.
        watermark_ts: Optional override for the start-of-window timestamp.
            When None, the persisted ``contradiction_watermark`` is read
            from the ``channel_sync_state`` MongoDB row.

    Returns:
        Number of facts checked (best-effort, count returned even if a
        subset of supersessions failed since per-fact errors are swallowed
        inside :func:`check_and_supersede`).
    """
    from beever_atlas.models import MemoryFilters
    from beever_atlas.stores import get_stores

    stores = get_stores()

    # ── 1. Resolve pre-check watermark ───────────────────────────────────
    pre_check_watermark: datetime
    if watermark_ts is not None:
        pre_check_watermark = (
            watermark_ts if watermark_ts.tzinfo is not None else watermark_ts.replace(tzinfo=UTC)
        )
    else:
        try:
            pre_check_watermark = await stores.mongodb.get_contradiction_watermark(channel_id)
        except Exception:
            logger.warning(
                "ContradictionDetector: failed to read watermark channel=%s — treating as epoch",
                channel_id,
                exc_info=True,
            )
            pre_check_watermark = _EPOCH_WATERMARK

    post_check_ts = datetime.now(UTC)

    # ── 2. Pull all facts newer than the watermark from Weaviate ─────────
    new_facts: list[AtomicFact] = []
    try:
        # ``MemoryFilters.since`` is an ISO-8601 timestamp interpreted by
        # the Weaviate store's ``list_facts`` as ``valid_at >=``. Paginate
        # explicitly — a 715-msg sync produces ~700 facts but dormant
        # channels reawakened after months may produce 2000+ in a single
        # window. The code-reviewer flagged the prior 1000-row cap as a
        # HIGH-severity latent risk: silently truncating + advancing the
        # watermark past unread facts would permanently skip contradiction
        # detection for them. Loop until we receive a partial page.
        _PAGE_SIZE = 500
        page = 1
        while True:
            result = await stores.weaviate.list_facts(
                channel_id=channel_id,
                filters=MemoryFilters(since=pre_check_watermark.isoformat()),
                page=page,
                limit=_PAGE_SIZE,
            )
            page_items = list(result.memories)
            new_facts.extend(page_items)
            if len(page_items) < _PAGE_SIZE:
                break
            page += 1
            # Defensive ceiling: 50 pages × 500 rows = 25k facts. Larger
            # channels almost certainly indicate a watermark accidentally
            # reset to epoch on a previously-synced large dataset; log
            # and stop to prevent runaway iteration.
            if page > 50:
                logger.warning(
                    "ContradictionDetector: paginated past 25k facts channel=%s — "
                    "stopping (likely watermark reset)",
                    channel_id,
                )
                break
    except Exception:
        # Backstop: if Weaviate is down, do NOT advance the watermark.
        # The next memory_settled (or admin trigger) retries from the
        # same pre_check_watermark — exactly the desired retry semantics
        # described in the v2 plan §1.4.
        logger.warning(
            "ContradictionDetector: weaviate list_facts failed channel=%s — "
            "not advancing watermark, will retry on next memory_settled",
            channel_id,
            exc_info=True,
        )
        return 0

    if not new_facts:
        # Still attempt to advance the watermark so empty drains do not
        # spin forever — but only if the persisted watermark has not
        # already moved past ``pre_check_watermark``.
        try:
            await stores.mongodb.advance_contradiction_watermark(
                channel_id=channel_id,
                pre_check=pre_check_watermark,
                post_check=post_check_ts,
            )
        except Exception:
            logger.debug(
                "ContradictionDetector: empty-window watermark advance failed channel=%s",
                channel_id,
                exc_info=True,
            )
        return 0

    # ── 3. Run per-fact supersession ──────────────────────────────────────
    try:
        await check_and_supersede(new_facts, channel_id)
    except Exception:
        # check_and_supersede is itself best-effort (per-fact errors are
        # caught inside ``_check_one``); a top-level raise indicates a
        # detector-level failure. Do NOT advance the watermark so the
        # next event re-attempts.
        logger.warning(
            "ContradictionDetector: bulk check_and_supersede raised channel=%s — "
            "leaving watermark unchanged",
            channel_id,
            exc_info=True,
        )
        return 0

    # ── 4. Atomic watermark advance with concurrent-guard ────────────────
    advanced = False
    try:
        advanced = await stores.mongodb.advance_contradiction_watermark(
            channel_id=channel_id,
            pre_check=pre_check_watermark,
            post_check=post_check_ts,
        )
    except Exception:
        logger.warning(
            "ContradictionDetector: watermark advance raised channel=%s — "
            "next memory_settled will retry",
            channel_id,
            exc_info=True,
        )
        return len(new_facts)

    if not advanced:
        logger.info(
            "ContradictionDetector: watermark already advanced by concurrent "
            "check channel=%s pre=%s — skipping",
            channel_id,
            pre_check_watermark.isoformat(),
        )
    else:
        logger.info(
            "ContradictionDetector: post-sync check channel=%s facts_checked=%d "
            "watermark_advanced=%s",
            channel_id,
            len(new_facts),
            post_check_ts.isoformat(),
        )

    return len(new_facts)
