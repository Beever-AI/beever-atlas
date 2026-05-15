"""Tests for ``AtomicFact`` Phase 3 enrichment fields.

The Phase 3 redesign adds six OPTIONAL fields to ``AtomicFact``:
``rationale``, ``alternatives_considered``, ``consequences_open``,
``numeric_values``, ``sentiment``, ``glossary_terms``. All carry safe
defaults (``None`` / empty list) so pre-Phase-3 documents persisted to
MongoDB deserialize cleanly without a migration.

These tests exercise:
  - default values when the JSON payload omits every Phase 3 field
    (the regression-critical backward-compat case)
  - happy-path round-trip when every field IS populated
  - serialize round-trip preserves the structured data
"""

from __future__ import annotations

import json

from beever_atlas.models.domain import AtomicFact


# ---------------------------------------------------------------------------
# Backward compat — pre-Phase-3 JSON deserializes cleanly
# ---------------------------------------------------------------------------


def test_phase3_fields_default_when_missing_from_payload() -> None:
    """A document persisted before Phase 3 shipped will not contain any
    of the new keys. ``AtomicFact.model_validate`` MUST populate the
    safe defaults rather than raise."""
    legacy_payload = {
        "memory_text": "Adopt CLA.",
        "fact_type": "decision",
        "importance": "critical",
        "channel_id": "C1",
        "author_name": "Alice",
        "message_ts": "2026-04-29T10:00:00Z",
    }
    fact = AtomicFact.model_validate(legacy_payload)
    assert fact.rationale is None
    assert fact.alternatives_considered == []
    assert fact.consequences_open == []
    assert fact.numeric_values == []
    assert fact.sentiment is None
    assert fact.glossary_terms == []


def test_phase3_fields_default_via_constructor() -> None:
    """Direct constructor calls (test fixtures, in-memory adapters)
    must also see the safe defaults without explicit kwargs."""
    fact = AtomicFact(memory_text="Build broke.")
    assert fact.rationale is None
    assert fact.alternatives_considered == []
    assert fact.consequences_open == []
    assert fact.numeric_values == []
    assert fact.sentiment is None
    assert fact.glossary_terms == []


def test_legacy_json_string_round_trip() -> None:
    """Defensive: a stringified pre-Phase-3 JSON blob (mimicking what
    Mongo / Weaviate serialise) must deserialise without raising."""
    legacy_json = json.dumps(
        {
            "memory_text": "Pin Python 3.12.",
            "fact_type": "decision",
            "channel_id": "C1",
            "importance": "high",
        }
    )
    fact = AtomicFact.model_validate_json(legacy_json)
    assert fact.memory_text == "Pin Python 3.12."
    assert fact.rationale is None
    assert fact.alternatives_considered == []


# ---------------------------------------------------------------------------
# Forward path — populated fields round-trip cleanly
# ---------------------------------------------------------------------------


def test_phase3_fields_populated_round_trip() -> None:
    """When the LLM provides Phase 3 enrichment, the values flow
    through model_validate → model_dump unchanged."""
    payload = {
        "memory_text": "Adopt Copyright-assignment CLA.",
        "fact_type": "decision",
        "channel_id": "C1",
        "rationale": "Provides relicensing flexibility for forks.",
        "alternatives_considered": ["DCO", "License-grant CLA"],
        "consequences_open": ["Will external contributors hesitate?"],
        "numeric_values": [
            {
                "label": "stars",
                "value": "2,396",
                "raw_value": 2396,
                "unit": "stars",
            }
        ],
        "sentiment": None,
        "glossary_terms": ["CLA", "DCO"],
    }
    fact = AtomicFact.model_validate(payload)
    assert fact.rationale == "Provides relicensing flexibility for forks."
    assert fact.alternatives_considered == ["DCO", "License-grant CLA"]
    assert fact.consequences_open == ["Will external contributors hesitate?"]
    assert len(fact.numeric_values) == 1
    assert fact.numeric_values[0]["value"] == "2,396"
    assert fact.numeric_values[0]["raw_value"] == 2396
    assert fact.glossary_terms == ["CLA", "DCO"]

    # Round-trip — model_dump → model_validate must preserve every
    # populated value.
    dumped = fact.model_dump()
    reloaded = AtomicFact.model_validate(dumped)
    assert reloaded.rationale == fact.rationale
    assert reloaded.alternatives_considered == fact.alternatives_considered
    assert reloaded.consequences_open == fact.consequences_open
    assert reloaded.numeric_values == fact.numeric_values
    assert reloaded.glossary_terms == fact.glossary_terms


def test_sentiment_accepts_known_strings() -> None:
    """Sentiment is a free-form ``str | None`` field; the prompt
    constrains the values but the schema is permissive so future
    additions don't require a migration."""
    for s in ("neutral", "concerning", "positive", "recommendation"):
        fact = AtomicFact(memory_text="Test.", sentiment=s)
        assert fact.sentiment == s


def test_numeric_values_preserves_dict_shape() -> None:
    """The structured ``numeric_values`` payload must preserve all
    four keys (``label``, ``value``, ``raw_value``, ``unit``) so
    ``stat_strip`` can read them downstream."""
    fact = AtomicFact(
        memory_text="Test.",
        numeric_values=[
            {"label": "impressions", "value": "534k", "raw_value": 534000, "unit": None},
            {"label": "paid-media", "value": "HK$130k", "raw_value": 130000, "unit": "HKD"},
        ],
    )
    assert fact.numeric_values[0]["unit"] is None
    assert fact.numeric_values[1]["unit"] == "HKD"
