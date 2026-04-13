"""Tests for the QA_TOOL_DESCRIPTORS registry."""

from __future__ import annotations

from beever_atlas.agents.tools import QA_TOOLS, QA_TOOL_DESCRIPTORS


def _tool_name(tool) -> str:
    return (
        getattr(tool, "__name__", None)
        or getattr(tool, "name", None)
        or getattr(getattr(tool, "func", None), "__name__", "")
    )


def test_descriptor_count_matches_registry():
    assert len(QA_TOOL_DESCRIPTORS) == len(QA_TOOLS) == 10


def test_every_descriptor_name_in_registry():
    tool_names = {_tool_name(t) for t in QA_TOOLS}
    for descriptor in QA_TOOL_DESCRIPTORS:
        assert descriptor["name"] in tool_names, (
            f"Descriptor {descriptor['name']!r} not found in QA_TOOLS"
        )


def test_all_four_categories_represented():
    categories = {d["category"] for d in QA_TOOL_DESCRIPTORS}
    assert categories == {"wiki", "memory", "graph", "external"}
