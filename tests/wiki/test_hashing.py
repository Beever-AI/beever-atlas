"""Tests for the canonical kind_schema hashing helper."""

from __future__ import annotations

from beever_atlas.wiki.hashing import (
    compute_kind_schema_hash,
    compute_prompt_version,
)


def test_hash_stable_for_identical_payload() -> None:
    payload = {"name": "Alpha", "count": 3, "tags": ["a", "b"]}
    assert compute_kind_schema_hash("topic", payload) == compute_kind_schema_hash("topic", payload)


def test_hash_changes_when_payload_changes() -> None:
    h1 = compute_kind_schema_hash("topic", {"name": "Alpha"})
    h2 = compute_kind_schema_hash("topic", {"name": "Beta"})
    assert h1 != h2


def test_hash_invariant_to_dict_key_order() -> None:
    h1 = compute_kind_schema_hash("topic", {"a": 1, "b": 2})
    h2 = compute_kind_schema_hash("topic", {"b": 2, "a": 1})
    assert h1 == h2


def test_hash_invariant_to_unordered_list_order() -> None:
    """``entity_tags`` is declared unordered for the topic kind."""
    p1 = {"entity_tags": ["alpha", "beta"]}
    p2 = {"entity_tags": ["beta", "alpha"]}
    assert compute_kind_schema_hash("topic", p1) == compute_kind_schema_hash("topic", p2)


def test_hash_sensitive_to_ordered_list_order() -> None:
    """``epochs`` for the timeline kind is order-significant."""
    p1 = {"epochs": ["jan", "feb"]}
    p2 = {"epochs": ["feb", "jan"]}
    assert compute_kind_schema_hash("timeline", p1) != compute_kind_schema_hash("timeline", p2)


def test_hash_strips_whitespace_in_strings() -> None:
    h1 = compute_kind_schema_hash("topic", {"name": "Alpha"})
    h2 = compute_kind_schema_hash("topic", {"name": "  Alpha  "})
    assert h1 == h2


def test_hash_excludes_derived_fields() -> None:
    p1 = {"name": "Alpha", "fact_count": 3, "generated_at": "2026-01-01"}
    p2 = {"name": "Alpha", "fact_count": 99, "generated_at": "2030-12-31"}
    assert compute_kind_schema_hash("topic", p1) == compute_kind_schema_hash("topic", p2)


def test_hash_busts_on_prompt_version_change() -> None:
    payload = {"name": "Alpha"}
    h1 = compute_kind_schema_hash("topic", payload, prompt_version="v1")
    h2 = compute_kind_schema_hash("topic", payload, prompt_version="v2")
    assert h1 != h2


def test_hash_empty_for_none_payload() -> None:
    assert compute_kind_schema_hash("topic", None) == ""


def test_hash_kind_separation() -> None:
    """Same payload under different kinds produces different hashes."""
    p = {"name": "Alpha"}
    assert compute_kind_schema_hash("topic", p) != compute_kind_schema_hash("people", p)


def test_compute_prompt_version_short_and_stable() -> None:
    v1 = compute_prompt_version("hello world")
    v2 = compute_prompt_version("hello world")
    v3 = compute_prompt_version("hello world!")
    assert v1 == v2
    assert v1 != v3
    assert len(v1) == 16
