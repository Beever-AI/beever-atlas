"""Wire all stages into the ingestion Workflow graph."""

from __future__ import annotations

import logging

from google.adk.workflow import JoinNode, Workflow

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


def create_ingestion_pipeline() -> Workflow:
    """Create the ingestion pipeline as an ADK Workflow graph.

    The classifier stage has been removed — the fact quality gate callback
    now bridges extracted_facts to classified_facts directly.
    Embedder and cross-batch validator run in parallel (independent data flows).

    The graph is::

        preprocessor -> [fact, entity] -> join_extract
            -> [embedder, validator] -> join_enrich -> persister

    The ``JoinNode``s are correctness-critical: a node with N incoming edges
    runs once PER predecessor unless a ``JoinNode`` collapses the fan-in to a
    single downstream trigger. Without ``join_enrich`` the persister would run
    twice (once per enrich-stage predecessor) and double-write to Weaviate/the
    graph. ``join_extract`` likewise gates the enrich stage on both extractors.
    The preprocessor fans out but does not fan in, so it needs no join.

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

    preprocessor = PreprocessorAgent(name="preprocessor")
    fact = create_fact_extractor()
    entity = create_entity_extractor()
    embedder = EmbedderAgent(name="embedder")
    persister = PersisterAgent(name="persister")
    # JoinNodes collapse each parallel stage to a single downstream trigger so
    # the fan-in target (enrich stage, then persister) runs exactly once.
    join_extract = JoinNode(name="join_extract")
    join_enrich = JoinNode(name="join_enrich")

    return Workflow(
        name="ingestion_pipeline",
        edges=[
            ("START", preprocessor),
            (preprocessor, fact),
            (preprocessor, entity),
            (fact, join_extract),
            (entity, join_extract),
            (join_extract, embedder),
            (join_extract, validator),
            (embedder, join_enrich),
            (validator, join_enrich),
            (join_enrich, persister),
        ],
    )
