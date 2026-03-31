from __future__ import annotations

from pydantic import BaseModel

from beever_atlas.agents.schemas.extraction import ExtractedFact


class ClassificationResult(BaseModel):
    """Output schema for the ClassifierAgent."""

    facts: list[ExtractedFact]
