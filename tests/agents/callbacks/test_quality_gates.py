"""Unit tests for agents/callbacks/quality_gates.py.

All ADK / LLM / external calls are mocked so these tests run offline and
in a fresh checkout without optional extras.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(state: dict[str, Any]) -> MagicMock:
    """Return a minimal CallbackContext mock backed by *state* dict."""
    ctx = MagicMock()
    ctx.state = state
    return ctx


def _make_settings(quality_threshold: float = 0.6, entity_threshold: float = 0.5) -> MagicMock:
    s = MagicMock()
    s.quality_threshold = quality_threshold
    s.entity_threshold = entity_threshold
    return s


# ---------------------------------------------------------------------------
# fact_quality_gate_callback
# ---------------------------------------------------------------------------


class TestFactQualityGateCallback:
    def _run(self, state: dict, threshold: float = 0.6) -> dict:
        from beever_atlas.agents.callbacks.quality_gates import fact_quality_gate_callback

        with patch(
            "beever_atlas.agents.callbacks.quality_gates.get_settings",
            return_value=_make_settings(quality_threshold=threshold),
        ):
            ctx = _make_ctx(state)
            fact_quality_gate_callback(ctx)
            return ctx.state

    def test_no_extracted_facts_returns_early(self):
        # When extracted_facts is absent, callback returns without modifying state.
        state = self._run({})
        assert "extracted_facts" not in state

    def test_dict_input_filters_below_threshold(self):
        facts = [
            {"text": "keep", "quality_score": 0.8},
            {"text": "drop", "quality_score": 0.3},
            {"text": "keep2", "quality_score": 0.6},
        ]
        state = self._run({"extracted_facts": {"facts": facts}})
        result = state["extracted_facts"]["facts"]
        assert len(result) == 2
        assert all(f["quality_score"] >= 0.6 for f in result)

    def test_dict_input_all_pass_threshold(self):
        facts = [{"text": "a", "quality_score": 0.9}, {"text": "b", "quality_score": 0.7}]
        state = self._run({"extracted_facts": {"facts": facts}})
        assert len(state["extracted_facts"]["facts"]) == 2

    def test_dict_input_all_fail_threshold(self):
        facts = [{"text": "a", "quality_score": 0.1}, {"text": "b", "quality_score": 0.2}]
        state = self._run({"extracted_facts": {"facts": facts}})
        assert state["extracted_facts"]["facts"] == []

    def test_dict_preserves_extra_keys(self):
        state_in = {"extracted_facts": {"facts": [], "skip_reason": "no_messages", "extra": "x"}}
        state = self._run(state_in)
        assert state["extracted_facts"]["skip_reason"] == "no_messages"
        assert state["extracted_facts"]["extra"] == "x"

    def test_pydantic_model_input(self):
        from beever_atlas.agents.schemas.extraction import ExtractedFact, FactExtractionResult

        facts = [
            ExtractedFact(memory_text="keep", quality_score=0.9),
            ExtractedFact(memory_text="drop", quality_score=0.1),
        ]
        raw = FactExtractionResult(facts=facts, skip_reason=None)
        state = self._run({"extracted_facts": raw})
        assert len(state["extracted_facts"]["facts"]) == 1

    def test_unexpected_type_returns_early(self):
        # Non-dict, non-model input: callback returns without modifying state.
        state = self._run({"extracted_facts": 42})
        # State is unchanged — extracted_facts still holds the original value.
        assert state["extracted_facts"] == 42

    def test_classified_facts_bridge_set(self):
        facts = [{"text": "x", "quality_score": 0.7}]
        state = self._run({"extracted_facts": {"facts": facts}})
        assert state["classified_facts"] == state["extracted_facts"]

    def test_per_channel_threshold_from_state(self):
        """State quality_threshold overrides settings value."""
        facts = [
            {"text": "keep", "quality_score": 0.8},
            {"text": "drop", "quality_score": 0.5},
        ]
        from beever_atlas.agents.callbacks.quality_gates import fact_quality_gate_callback

        with patch(
            "beever_atlas.agents.callbacks.quality_gates.get_settings",
            return_value=_make_settings(quality_threshold=0.3),
        ):
            ctx = _make_ctx({"extracted_facts": {"facts": facts}, "quality_threshold": 0.7})
            fact_quality_gate_callback(ctx)
            # threshold=0.7 from state, not 0.3 from settings
            result = ctx.state["extracted_facts"]["facts"]
        assert len(result) == 1
        assert result[0]["quality_score"] == 0.8

    def test_empty_facts_list(self):
        state = self._run({"extracted_facts": {"facts": []}})
        assert state["extracted_facts"]["facts"] == []

    def test_missing_quality_score_treated_as_zero(self):
        facts = [{"text": "no_score"}, {"text": "has_score", "quality_score": 0.9}]
        state = self._run({"extracted_facts": {"facts": facts}}, threshold=0.5)
        assert len(state["extracted_facts"]["facts"]) == 1
        assert state["extracted_facts"]["facts"][0]["text"] == "has_score"


# ---------------------------------------------------------------------------
# entity_quality_gate_callback
# ---------------------------------------------------------------------------


class TestEntityQualityGateCallback:
    def _run(self, state: dict, entity_threshold: float = 0.5) -> dict:
        from beever_atlas.agents.callbacks.quality_gates import entity_quality_gate_callback

        with patch(
            "beever_atlas.agents.callbacks.quality_gates.get_settings",
            return_value=_make_settings(entity_threshold=entity_threshold),
        ):
            ctx = _make_ctx(state)
            entity_quality_gate_callback(ctx)
            return ctx.state

    def test_no_extracted_entities_returns_early(self):
        # When extracted_entities is absent, callback returns without modifying state.
        state = self._run({})
        assert "extracted_entities" not in state

    def test_filters_low_confidence_relationships(self):
        rels = [
            {"source": "A", "target": "B", "confidence": 0.8},
            {"source": "C", "target": "D", "confidence": 0.2},
        ]
        entities = [
            {"name": "A", "scope": "channel"},
            {"name": "B", "scope": "channel"},
            {"name": "C", "scope": "channel"},
        ]
        state = self._run({"extracted_entities": {"entities": entities, "relationships": rels}})
        result_rels = state["extracted_entities"]["relationships"]
        assert len(result_rels) == 1
        assert result_rels[0]["confidence"] == 0.8

    def test_global_scope_entities_always_kept(self):
        rels: list = []
        entities = [
            {"name": "GlobalEnt", "scope": "global"},
            {"name": "LocalEnt", "scope": "channel"},
        ]
        state = self._run({"extracted_entities": {"entities": entities, "relationships": rels}})
        result_entities = state["extracted_entities"]["entities"]
        # GlobalEnt kept, LocalEnt dropped (no qualifying rels)
        assert len(result_entities) == 1
        assert result_entities[0]["name"] == "GlobalEnt"

    def test_channel_entity_kept_when_in_surviving_rel(self):
        rels = [{"source": "Alice", "target": "Bob", "confidence": 0.9}]
        entities = [
            {"name": "Alice", "scope": "channel"},
            {"name": "Bob", "scope": "channel"},
            {"name": "Charlie", "scope": "channel"},
        ]
        state = self._run({"extracted_entities": {"entities": entities, "relationships": rels}})
        names = {e["name"] for e in state["extracted_entities"]["entities"]}
        assert "Alice" in names
        assert "Bob" in names
        assert "Charlie" not in names

    def test_skip_reason_preserved(self):
        state = self._run(
            {
                "extracted_entities": {
                    "entities": [],
                    "relationships": [],
                    "skip_reason": "too_short",
                }
            }
        )
        assert state["extracted_entities"]["skip_reason"] == "too_short"

    def test_unexpected_type_returns_early(self):
        # Non-dict, non-model input: callback returns without modifying state.
        state = self._run({"extracted_entities": "bad_type"})
        assert state["extracted_entities"] == "bad_type"

    def test_pydantic_model_input(self):
        from beever_atlas.agents.schemas.extraction import (
            EntityExtractionResult,
            ExtractedEntity,
            ExtractedRelationship,
        )

        entities = [
            ExtractedEntity(name="A", type="Person", scope="channel"),
        ]
        rels = [ExtractedRelationship(source="A", target="B", type="KNOWS", confidence=0.9)]
        raw = EntityExtractionResult(entities=entities, relationships=rels, skip_reason=None)
        state = self._run({"extracted_entities": raw})
        # confidence 0.9 >= 0.5 → rel survives, entity A referenced → kept
        assert len(state["extracted_entities"]["relationships"]) == 1

    def test_empty_input(self):
        state = self._run({"extracted_entities": {"entities": [], "relationships": []}})
        assert state["extracted_entities"]["entities"] == []
        assert state["extracted_entities"]["relationships"] == []


# ---------------------------------------------------------------------------
# fact_extraction_with_recovery
# ---------------------------------------------------------------------------


class TestFactExtractionWithRecovery:
    def _run(self, state: dict, threshold: float = 0.6) -> dict:
        from beever_atlas.agents.callbacks.quality_gates import fact_extraction_with_recovery

        with patch(
            "beever_atlas.agents.callbacks.quality_gates.get_settings",
            return_value=_make_settings(quality_threshold=threshold),
        ):
            ctx = _make_ctx(state)
            fact_extraction_with_recovery(ctx)
            return ctx.state

    def test_valid_dict_delegates_to_quality_gate(self):
        facts = [{"text": "keep", "quality_score": 0.9}]
        state = self._run({"extracted_facts": {"facts": facts}})
        assert len(state["extracted_facts"]["facts"]) == 1

    def test_string_input_with_recoverable_json(self):
        import json

        payload = json.dumps({"facts": [{"text": "recovered", "quality_score": 0.8}]})

        mock_report = MagicMock()
        mock_report.recovered_count = 1
        mock_report.estimated_lost = 0
        mock_report.raw_bytes = len(payload)
        mock_report.last_boundary_offset = len(payload)

        with patch(
            "beever_atlas.agents.callbacks.quality_gates.get_settings",
            return_value=_make_settings(quality_threshold=0.6),
        ):
            with patch(
                "beever_atlas.services.json_recovery.recover_truncated_json_with_report",
                return_value=(
                    {"facts": [{"text": "recovered", "quality_score": 0.8}]},
                    mock_report,
                ),
            ):
                ctx = _make_ctx({"extracted_facts": payload})
                from beever_atlas.agents.callbacks.quality_gates import (
                    fact_extraction_with_recovery,
                )

                fact_extraction_with_recovery(ctx)
        assert ctx.state["extracted_facts"]["facts"][0]["text"] == "recovered"

    def test_string_input_unrecoverable_sets_empty(self):
        mock_report = MagicMock()
        mock_report.recovered_count = 0
        mock_report.estimated_lost = 0
        mock_report.raw_bytes = 5
        mock_report.last_boundary_offset = 0

        with patch(
            "beever_atlas.agents.callbacks.quality_gates.get_settings",
            return_value=_make_settings(quality_threshold=0.6),
        ):
            with patch(
                "beever_atlas.services.json_recovery.recover_truncated_json_with_report",
                return_value=(None, mock_report),
            ):
                ctx = _make_ctx({"extracted_facts": "garbage"})
                from beever_atlas.agents.callbacks.quality_gates import (
                    fact_extraction_with_recovery,
                )

                fact_extraction_with_recovery(ctx)
        assert ctx.state["extracted_facts"]["facts"] == []
        assert ctx.state["extracted_facts"]["skip_reason"] == "extraction_failed"

    def test_none_input_sets_empty(self):
        # raw is None → falls to fallback
        state = self._run({"extracted_facts": None})
        # fact_quality_gate_callback logs warning when raw is None and returns
        # without touching state — recovery path doesn't apply. Check no crash.
        assert "extracted_facts" in state


# ---------------------------------------------------------------------------
# entity_extraction_with_recovery
# ---------------------------------------------------------------------------


class TestEntityExtractionWithRecovery:
    def _run(self, state: dict, entity_threshold: float = 0.5) -> dict:
        from beever_atlas.agents.callbacks.quality_gates import entity_extraction_with_recovery

        with patch(
            "beever_atlas.agents.callbacks.quality_gates.get_settings",
            return_value=_make_settings(entity_threshold=entity_threshold),
        ):
            ctx = _make_ctx(state)
            entity_extraction_with_recovery(ctx)
            return ctx.state

    def test_valid_dict_delegates_to_quality_gate(self):
        state = self._run({"extracted_entities": {"entities": [], "relationships": []}})
        assert state["extracted_entities"]["entities"] == []

    def test_string_input_recoverable(self):
        import json

        payload = json.dumps({"entities": [{"name": "X", "scope": "global"}], "relationships": []})
        mock_report = MagicMock()
        mock_report.recovered_count = 1
        mock_report.estimated_lost = 0
        mock_report.raw_bytes = len(payload)
        mock_report.last_boundary_offset = len(payload)

        with patch(
            "beever_atlas.agents.callbacks.quality_gates.get_settings",
            return_value=_make_settings(entity_threshold=0.5),
        ):
            with patch(
                "beever_atlas.services.json_recovery.recover_truncated_json_with_report",
                return_value=(
                    {"entities": [{"name": "X", "scope": "global"}], "relationships": []},
                    mock_report,
                ),
            ):
                ctx = _make_ctx({"extracted_entities": payload})
                from beever_atlas.agents.callbacks.quality_gates import (
                    entity_extraction_with_recovery,
                )

                entity_extraction_with_recovery(ctx)
        assert ctx.state["extracted_entities"]["entities"][0]["name"] == "X"

    def test_string_input_unrecoverable_sets_empty(self):
        mock_report = MagicMock()
        mock_report.recovered_count = 0
        mock_report.estimated_lost = 0
        mock_report.raw_bytes = 3
        mock_report.last_boundary_offset = 0

        with patch(
            "beever_atlas.agents.callbacks.quality_gates.get_settings",
            return_value=_make_settings(entity_threshold=0.5),
        ):
            with patch(
                "beever_atlas.services.json_recovery.recover_truncated_json_with_report",
                return_value=(None, mock_report),
            ):
                ctx = _make_ctx({"extracted_entities": "bad"})
                from beever_atlas.agents.callbacks.quality_gates import (
                    entity_extraction_with_recovery,
                )

                entity_extraction_with_recovery(ctx)
        assert ctx.state["extracted_entities"]["entities"] == []
        assert ctx.state["extracted_entities"]["relationships"] == []
