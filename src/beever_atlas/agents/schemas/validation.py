from __future__ import annotations

from pydantic import BaseModel

from beever_atlas.agents.schemas.extraction import ExtractedEntity, ExtractedRelationship


class MergeRecord(BaseModel):
    canonical: str
    merged_from: list[str]


class ValidationResult(BaseModel):
    """Output schema for the CrossBatchValidatorAgent."""

    entities: list[ExtractedEntity]
    relationships: list[ExtractedRelationship]
    merges: list[MergeRecord]
