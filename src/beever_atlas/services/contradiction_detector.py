"""Contradiction detection service for temporal fact lifecycle.

Compares newly extracted facts against existing facts to detect
contradictions and trigger fact supersession via ADK agent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from beever_atlas.infra.config import get_settings
from beever_atlas.models import AtomicFact

logger = logging.getLogger(__name__)


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
        state = await run_agent(agent, state={
            "new_fact": new_fact_text,
            "existing_facts": "\n".join(existing_lines),
        })

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
