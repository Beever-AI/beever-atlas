"""Unit tests for the QA agent skill pack (Stream 1)."""

from __future__ import annotations

import re

import pytest

# Skip this entire module if the upstream `google.adk.skills` module is not
# available (e.g. older google-adk versions or environments without the
# private skills API surface). The skill pack cannot be constructed without it.
pytest.importorskip("google.adk.skills")
pytest.importorskip("google.adk.skills.models")

from beever_atlas.agents.query.skills import (
    QA_SKILL_NAMES,
    build_qa_skill_pack,
)
from beever_atlas.agents.tools import QA_TOOLS


_KEBAB_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _tool_registry_names() -> set[str]:
    names: set[str] = set()
    for tool in QA_TOOLS:
        n = (
            getattr(tool, "__name__", None)
            or getattr(tool, "name", None)
            or getattr(getattr(tool, "func", None), "__name__", None)
        )
        if n:
            names.add(n)
    return names


def test_skill_pack_count() -> None:
    skills = build_qa_skill_pack()
    assert len(skills) == 8
    assert len(QA_SKILL_NAMES) == 8
    assert {s.frontmatter.name for s in skills} == set(QA_SKILL_NAMES)


def test_skill_names_are_kebab() -> None:
    for s in build_qa_skill_pack():
        name = s.frontmatter.name
        assert _KEBAB_RE.match(name), f"{name!r} is not kebab-case"
        assert len(name) <= 64, f"{name!r} exceeds 64 chars"


def test_description_length() -> None:
    for s in build_qa_skill_pack():
        desc = s.frontmatter.description
        assert desc, f"skill {s.frontmatter.name} has empty description"
        assert len(desc) <= 1024, f"{s.frontmatter.name} description is {len(desc)} chars (>1024)"


def test_allowed_tools_exist_in_registry() -> None:
    registry = _tool_registry_names()
    # Also allow suggest_follow_ups, which is registered dynamically when the
    # citation registry flag is on. Include it in the allowed set.
    from beever_atlas.agents.query.follow_ups_tool import suggest_follow_ups

    registry.add(
        getattr(suggest_follow_ups, "__name__", None)
        or getattr(suggest_follow_ups, "name", "suggest_follow_ups")
    )

    for s in build_qa_skill_pack():
        allowed = s.frontmatter.allowed_tools
        if not allowed:
            continue
        for tool_name in allowed.split():
            assert tool_name in registry, (
                f"skill {s.frontmatter.name} references unknown tool {tool_name!r}"
            )


def test_media_gallery_description_keywords() -> None:
    skills = {s.frontmatter.name: s for s in build_qa_skill_pack()}
    desc = skills["media-gallery"].frontmatter.description
    assert "architecture diagram" in desc or "flowchart" in desc


def test_source_braid_description_keywords() -> None:
    skills = {s.frontmatter.name: s for s in build_qa_skill_pack()}
    desc = skills["source-braid"].frontmatter.description
    assert "industry" in desc
    assert "best practices" in desc


def test_l1_token_budget() -> None:
    tiktoken = pytest.importorskip("tiktoken")
    enc = tiktoken.get_encoding("o200k_base")

    total = 0
    for s in build_qa_skill_pack():
        total += len(enc.encode(s.frontmatter.name))
        total += len(enc.encode(s.frontmatter.description))
    assert total <= 1000, f"L1 token budget exceeded: {total} > 1000"
