"""Unit tests for the citation-envelope persistence helpers."""

from __future__ import annotations

from beever_atlas.agents.citations.persistence import (
    as_legacy_items,
    upgrade_envelope,
)


def test_none_becomes_empty_envelope():
    assert upgrade_envelope(None) == {"items": [], "sources": [], "refs": []}


def test_legacy_list_wrapped():
    legacy = [{"author": "a", "channel": "c"}]
    env = upgrade_envelope(legacy)
    assert env == {"items": legacy, "sources": [], "refs": []}


def test_existing_envelope_passthrough():
    env_in = {
        "items": [{"a": 1}],
        "sources": [{"id": "src_x"}],
        "refs": [{"marker": 1, "source_id": "src_x"}],
    }
    out = upgrade_envelope(env_in)
    assert out == env_in
    # Defensive copy: mutating the output must not touch input.
    out["items"].append({"new": 1})
    assert len(env_in["items"]) == 1


def test_dict_missing_fields_filled():
    env = upgrade_envelope({"items": [{"x": 1}]})
    assert env["sources"] == []
    assert env["refs"] == []


def test_unexpected_type_returns_empty():
    assert upgrade_envelope(42) == {"items": [], "sources": [], "refs": []}
    assert upgrade_envelope("string") == {"items": [], "sources": [], "refs": []}


def test_as_legacy_items_from_envelope():
    env = {"items": [{"a": 1}], "sources": [], "refs": []}
    assert as_legacy_items(env) == [{"a": 1}]


def test_as_legacy_items_from_list():
    assert as_legacy_items([{"x": 1}]) == [{"x": 1}]


def test_as_legacy_items_from_none():
    assert as_legacy_items(None) == []
