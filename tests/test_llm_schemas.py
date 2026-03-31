from __future__ import annotations

import json

from beever_atlas.agents.schemas.classification import ClassificationResult
from beever_atlas.agents.schemas.extraction import EntityExtractionResult, FactExtractionResult
from beever_atlas.agents.schemas.validation import ValidationResult


def test_gemini_output_schemas_do_not_use_additional_properties_true() -> None:
    for schema_cls in (
        FactExtractionResult,
        EntityExtractionResult,
        ClassificationResult,
        ValidationResult,
    ):
        schema_json = json.dumps(schema_cls.model_json_schema())
        assert '"additionalProperties": true' not in schema_json
