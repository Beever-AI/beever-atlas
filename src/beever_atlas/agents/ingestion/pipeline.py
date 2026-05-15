"""Wire all stages into the ingestion SequentialAgent."""

from __future__ import annotations

import logging

from google.adk.agents import SequentialAgent, ParallelAgent

from beever_atlas.agents.ingestion.preprocessor import PreprocessorAgent
from beever_atlas.agents.ingestion.fact_extractor import create_fact_extractor
from beever_atlas.agents.ingestion.entity_extractor import create_entity_extractor
from beever_atlas.agents.ingestion.embedder import EmbedderAgent
from beever_atlas.agents.ingestion.cross_batch_validator import (
    DeterministicCrossBatchValidator,
)
from beever_atlas.agents.ingestion.persister import PersisterAgent
from beever_atlas.infra.config import get_settings

logger = logging.getLogger(__name__)


def create_ingestion_pipeline() -> SequentialAgent:
    """Create the 6-stage ingestion pipeline.

    The classifier stage has been removed — the fact quality gate callback
    now bridges extracted_facts to classified_facts directly.
    Embedder and cross-batch validator run in parallel (independent data flows).

    P0-3 (plan ``pipeline-cost-latency-reduction-v2.md``): the cross-batch
    validator is now a deterministic ``BaseAgent`` (name normalization +
    embedding cosine similarity). The legacy ``LlmAgent`` path has been
    removed in this PR — rollback is by git revert of the change. The
    ``cross_batch_validator_deterministic`` flag is retained for forward
    compatibility (e.g. an A/B fallback to a different validator implementation)
    and currently always selects the deterministic agent; a False value
    logs a one-shot warning and falls through to the same path.
    """
    settings = get_settings()
    if not settings.cross_batch_validator_deterministic:
        logger.warning(
            "pipeline: cross_batch_validator_deterministic=False has no "
            "alternative implementation in this build — using the "
            "deterministic validator. Roll back via git revert if a "
            "regression is observed."
        )
    validator = DeterministicCrossBatchValidator(name="cross_batch_validator_agent")

    return SequentialAgent(
        name="ingestion_pipeline",
        sub_agents=[
            PreprocessorAgent(name="preprocessor"),
            ParallelAgent(
                name="extraction_parallel",
                sub_agents=[create_fact_extractor(), create_entity_extractor()],
            ),
            ParallelAgent(
                name="enrich_parallel",
                sub_agents=[EmbedderAgent(name="embedder"), validator],
            ),
            PersisterAgent(name="persister"),
        ],
    )
