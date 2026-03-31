"""Wire all 7 stages into the ingestion SequentialAgent."""
from __future__ import annotations

from google.adk.agents import SequentialAgent, ParallelAgent

from beever_atlas.agents.ingestion.preprocessor import PreprocessorAgent
from beever_atlas.agents.ingestion.fact_extractor import create_fact_extractor
from beever_atlas.agents.ingestion.entity_extractor import create_entity_extractor
from beever_atlas.agents.ingestion.classifier import create_classifier
from beever_atlas.agents.ingestion.embedder import EmbedderAgent
from beever_atlas.agents.ingestion.cross_batch_validator import create_cross_batch_validator
from beever_atlas.agents.ingestion.persister import PersisterAgent


def create_ingestion_pipeline() -> SequentialAgent:
    """Create the 7-stage ingestion pipeline."""
    return SequentialAgent(
        name="ingestion_pipeline",
        sub_agents=[
            PreprocessorAgent(name="preprocessor"),
            ParallelAgent(
                name="extraction_parallel",
                sub_agents=[create_fact_extractor(), create_entity_extractor()],
            ),
            create_classifier(),
            EmbedderAgent(name="embedder"),
            create_cross_batch_validator(),
            PersisterAgent(name="persister"),
        ],
    )
