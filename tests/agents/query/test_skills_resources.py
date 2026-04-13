"""Resource-loader tests for the QA skill pack."""

from __future__ import annotations

import pytest

pytest.importorskip("google.adk.skills")
pytest.importorskip("google.adk.skills.models")

from beever_atlas.agents.query.skills import load_resource


EXPECTED_RESOURCES = [
    "timeline_template.md",
    "profile_template.md",
    "comparison_table_template.md",
    "mermaid_cheatsheet.md",
    "gallery_template.md",
    "digest_template.md",
    "braid_pattern.md",
    "followup_templates_by_type.md",
]


@pytest.mark.parametrize("name", EXPECTED_RESOURCES)
def test_all_resources_load(name: str) -> None:
    content = load_resource(name)
    assert isinstance(content, str)
    assert content.strip(), f"{name} is empty"
