"""Cross-batch validator agent — Stage 5 of the ingestion pipeline.

Deterministic ``BaseAgent`` subclass that replaces the previous
``LlmAgent``-based validator. Performs name-normalization + embedding
cosine similarity dedup via
:func:`beever_atlas.services.entity_dedup.dedupe_entities`. The output
schema (``validated_entities`` → ``ValidationResult``) is unchanged so
``PersisterAgent`` works without modification.

See plan ``.omc/plans/pipeline-cost-latency-reduction-v2.md`` section
P0-3 for the rationale, calibration plan, and rollback strategy.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from beever_atlas.infra.config import get_settings
from beever_atlas.services.entity_dedup import dedupe_entities

logger = logging.getLogger(__name__)


class DeterministicCrossBatchValidator(BaseAgent):
    """Deterministic dedup stage — no LLM calls on the common path.

    Reads  : ``session.state["extracted_entities"]``,
             ``session.state["known_entities"]``
    Writes : ``session.state["validated_entities"]``
             (``ValidationResult.model_dump()``)
    """

    model_config = {"arbitrary_types_allowed": True}

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        """Run the three-tier dedup pass and yield a single completion event."""
        from beever_atlas.agents.callbacks.checkpoint_skip import should_skip_stage

        if should_skip_stage(ctx.session.state, "validated_entities", self.name):
            yield Event(author=self.name, invocation_id=ctx.invocation_id)
            return

        sync_job_id = ctx.session.state.get("sync_job_id", "unknown")
        channel_id = ctx.session.state.get("channel_id", "unknown")
        batch_num = ctx.session.state.get("batch_num", "?")

        raw_extracted = ctx.session.state.get("extracted_entities")
        if not isinstance(raw_extracted, dict):
            if raw_extracted is not None:
                logger.warning(
                    "DeterministicCrossBatchValidator: extracted_entities is "
                    "%s, not dict; treating as empty",
                    type(raw_extracted).__name__,
                )
            empty_payload: dict[str, Any] = {
                "entities": [],
                "relationships": [],
                "merges": [],
            }
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                actions=EventActions(state_delta={"validated_entities": empty_payload}),
            )
            return

        entities = raw_extracted.get("entities") or []
        relationships = raw_extracted.get("relationships") or []
        if not entities and not relationships:
            logger.info(
                "DeterministicCrossBatchValidator: skipping — 0 entities, "
                "0 relationships job_id=%s channel=%s batch=%s",
                sync_job_id,
                channel_id,
                batch_num,
            )
            empty_payload = {"entities": [], "relationships": [], "merges": []}
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                actions=EventActions(state_delta={"validated_entities": empty_payload}),
            )
            return

        known_entities = ctx.session.state.get("known_entities") or []
        settings = get_settings()
        llm_fallback_enabled = settings.cross_batch_validator_llm_fallback

        # Resolve the entity registry lazily — the agent runs inside the
        # batch processor where the global ``stores`` singleton has
        # already been initialised. In CI / unit-test contexts this can
        # raise; we treat that as "no embedding tier" rather than crash
        # so the deterministic Tier 1 still runs.
        entity_registry = None
        try:
            from beever_atlas.stores import get_stores

            entity_registry = get_stores().entity_registry
        except Exception:
            logger.warning(
                "DeterministicCrossBatchValidator: entity_registry unavailable "
                "— running in exact-match-only mode (job_id=%s batch=%s)",
                sync_job_id,
                batch_num,
            )

        result, llm_fallback_count = await dedupe_entities(
            entities=entities,
            relationships=relationships,
            prior_entities=known_entities if isinstance(known_entities, list) else [],
            llm_fallback_enabled=llm_fallback_enabled,
            entity_registry=entity_registry,
        )

        # Per-batch fallback metric. Logged as a structured key so the
        # calibration script can grep it cheaply during the soak window.
        logger.info(
            "DeterministicCrossBatchValidator: done job_id=%s channel=%s "
            "batch=%s entities_in=%d entities_out=%d merges=%d "
            "cross_batch_validator_llm_fallback_count=%d",
            sync_job_id,
            channel_id,
            batch_num,
            len(entities),
            len(result.entities),
            len(result.merges),
            llm_fallback_count,
        )
        # Accumulate into per-sync counter for sync_summary: metric line.
        if llm_fallback_count and channel_id and sync_job_id:
            from beever_atlas.services.batch_processor import increment_sync_metric

            increment_sync_metric(
                channel_id,
                sync_job_id,
                "cross_batch_validator_llm_fallback_total",
                llm_fallback_count,
            )

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(
                state_delta={"validated_entities": result.model_dump()},
            ),
        )


def create_cross_batch_validator(model: Any = None) -> BaseAgent:
    """Factory preserved for backwards compatibility with the pipeline wiring.

    The ``model`` argument is accepted (and ignored) so call sites that
    previously passed an explicit Gemini model spec continue to work.
    A future PR may drop the kwarg once all callers have been updated.
    """
    del model  # deterministic agent — no LLM model needed
    return DeterministicCrossBatchValidator(name="cross_batch_validator_agent")
