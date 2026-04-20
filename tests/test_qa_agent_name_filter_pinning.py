"""Pinning tests for name-based filters in `beever_atlas.agents.query.qa_agent`.

Task 1.0 of openspec change `atlas-mcp-server`. These are **regression**
tests, not correctness tests: the expected values are derived by reading
the current implementation, and are asserted as hard-coded constants so
that the capabilities extraction in tasks 1.1-1.7 cannot silently drift
the observable behaviour of:

- `_filter_tools_for_untrusted(tools)` — substring denylist over tool
  `__name__` / `name` attributes.
- `_WIKI_TOOLS_NAMES` / `_SUMMARIZE_TOOLS_NAMES` — mode-specific subsets
  of `QA_TOOLS`.
- `_maybe_wrap_with_skills(tools_list)` — `allowed_tools` → enabled-tool
  overlap filter applied to the QA skill pack.

If a refactor makes any of these fail, **fix the refactor, not the test**.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from beever_atlas.agents.query.qa_agent import (
    _filter_tools_for_untrusted,
    _maybe_wrap_with_skills,
    _SUMMARIZE_TOOLS_NAMES,
    _WIKI_TOOLS_NAMES,
)
from beever_atlas.agents.tools import QA_TOOLS


# --- Pinned expectations (derived from CURRENT source on main) --------------

# All ten QA_TOOLS function names (order-insensitive for set comparison).
_ALL_QA_TOOL_NAMES = {
    "get_wiki_page",
    "get_topic_overview",
    "search_qa_history",
    "search_channel_facts",
    "search_media_references",
    "get_recent_activity",
    "search_relationships",
    "trace_decision_history",
    "find_experts",
    "search_external_knowledge",
}

# None of the current QA_TOOLS names contain any of the denylist fragments
# (`tavily`, `web_search`, `write`, `create`, `update`, `delete`, `send`,
# `post`), so the filter is a no-op over `QA_TOOLS` today.
_EXPECTED_FILTERED_QA_TOOL_NAMES = set(_ALL_QA_TOOL_NAMES)

_EXPECTED_WIKI_TOOLS_NAMES = {"get_wiki_page", "get_topic_overview"}
_EXPECTED_SUMMARIZE_TOOLS_NAMES = {
    "get_wiki_page",
    "get_topic_overview",
    "search_channel_facts",
    "search_qa_history",
}


def _tool_name(tool) -> str:
    """Match the lookup path used inside qa_agent (name / __name__ / func)."""
    return (
        getattr(tool, "__name__", None)
        or getattr(tool, "name", None)
        or getattr(getattr(tool, "func", None), "__name__", "")
    )


# --- Test 1: filter-for-untrusted -------------------------------------------


def test_filter_tools_for_untrusted_pins_expected_set():
    """`_filter_tools_for_untrusted` over the three mode tool-sets must
    return exactly the pinned name-sets derived from current `qa_agent.py`.

    Current state: no QA tool name contains any denylist fragment, so
    the filter is a no-op on each subset. This test also proves the
    filter *would* drop a synthetic write/egress tool, locking in the
    substring-match contract.
    """
    # Deep mode (all QA_TOOLS)
    deep_filtered = _filter_tools_for_untrusted(list(QA_TOOLS))
    assert {_tool_name(t) for t in deep_filtered} == _EXPECTED_FILTERED_QA_TOOL_NAMES

    # Quick mode subset (wiki-only)
    quick_subset = [t for t in QA_TOOLS if _tool_name(t) in _WIKI_TOOLS_NAMES]
    quick_filtered = _filter_tools_for_untrusted(quick_subset)
    assert {_tool_name(t) for t in quick_filtered} == _EXPECTED_WIKI_TOOLS_NAMES

    # Summarize mode subset
    summarize_subset = [t for t in QA_TOOLS if _tool_name(t) in _SUMMARIZE_TOOLS_NAMES]
    summarize_filtered = _filter_tools_for_untrusted(summarize_subset)
    assert {_tool_name(t) for t in summarize_filtered} == _EXPECTED_SUMMARIZE_TOOLS_NAMES

    # Sanity probe: the filter must drop a synthetic write/egress tool.
    def upsert_write_thing() -> None:
        """Synthetic; name contains 'write'."""

    def tavily_search_external() -> None:
        """Synthetic; name contains 'tavily'."""

    probe = [*QA_TOOLS, upsert_write_thing, tavily_search_external]
    probe_filtered = _filter_tools_for_untrusted(probe)
    assert {_tool_name(t) for t in probe_filtered} == _EXPECTED_FILTERED_QA_TOOL_NAMES


# --- Test 2: wiki / summarize name sets -------------------------------------


def test_wiki_tools_names_pins_expected_set():
    """`_WIKI_TOOLS_NAMES` and `_SUMMARIZE_TOOLS_NAMES` pin mode-filter
    inputs. Each name must also map to a real function in `QA_TOOLS` —
    a stale entry would silently produce an empty tool list in that mode.
    """
    assert _WIKI_TOOLS_NAMES == _EXPECTED_WIKI_TOOLS_NAMES
    assert _SUMMARIZE_TOOLS_NAMES == _EXPECTED_SUMMARIZE_TOOLS_NAMES

    qa_tool_names = {_tool_name(t) for t in QA_TOOLS}

    # Non-stale: every pinned name is backed by a tool in QA_TOOLS.
    assert _WIKI_TOOLS_NAMES.issubset(qa_tool_names)
    assert _SUMMARIZE_TOOLS_NAMES.issubset(qa_tool_names)

    # Monotone containment: summarize is a proper superset of wiki today.
    assert _WIKI_TOOLS_NAMES.issubset(_SUMMARIZE_TOOLS_NAMES)


# --- Test 3: skills allowed_tools overlap -----------------------------------


def test_skills_allowed_tools_pins_overlap():
    """`_maybe_wrap_with_skills` must keep only skills whose `allowed_tools`
    are a subset of the enabled tool names (plus skills with no
    `allowed_tools` constraint), and prepend the resulting `SkillToolset`
    to the original tool list without reordering the tools themselves.

    This pins the exact name-intersect semantics so the Phase 1 extraction
    cannot silently change which skills survive.
    """
    pytest.importorskip("google.adk.skills")
    pytest.importorskip("google.adk.skills.models")
    from google.adk.skills.models import Frontmatter, Resources, Skill  # noqa: E402

    # Two skills with `allowed_tools`: one is fully inside QA_TOOLS, the
    # other requires a name that does NOT exist in QA_TOOLS.
    inside_skill = Skill(
        frontmatter=Frontmatter(
            name="inside-skill",
            description="all allowed tools exist in QA_TOOLS",
            allowed_tools="get_wiki_page get_topic_overview",
        ),
        instructions="noop",
        resources=Resources(),
    )
    outside_skill = Skill(
        frontmatter=Frontmatter(
            name="outside-skill",
            description="requires a tool not in QA_TOOLS",
            allowed_tools="get_wiki_page nonexistent_tool_xyz",
        ),
        instructions="noop",
        resources=Resources(),
    )
    # Pure-formatting skill (allowed_tools=None) — always passes.
    formatting_skill = Skill(
        frontmatter=Frontmatter(
            name="formatting-skill",
            description="no allowed_tools constraint",
            allowed_tools=None,
        ),
        instructions="noop",
        resources=Resources(),
    )
    synthetic_pack = [inside_skill, outside_skill, formatting_skill]

    fake_settings = type(
        "FakeSettings",
        (),
        {"qa_skills_enabled": True, "qa_new_prompt": True},
    )()

    # Recorder stands in for `SkillToolset` — ADK's real class stores
    # the list under a private attribute that would couple this pin to
    # an implementation detail. The constructor kwargs ARE the contract.
    class _RecorderToolset:
        def __init__(self, skills):
            self.skills = list(skills)

    # Patch the settings accessor, the skill-pack builder, AND
    # SkillToolset at the import-time resolution sites inside
    # `_maybe_wrap_with_skills`.
    with (
        patch(
            "beever_atlas.infra.config.get_settings",
            return_value=fake_settings,
        ),
        patch(
            "beever_atlas.agents.query.skills.build_qa_skill_pack",
            return_value=synthetic_pack,
        ),
        patch(
            "google.adk.tools.skill_toolset.SkillToolset",
            _RecorderToolset,
        ),
    ):
        result = _maybe_wrap_with_skills(list(QA_TOOLS))

    # The wrapped output is `[SkillToolset, *tools_list]`.
    assert len(result) == len(QA_TOOLS) + 1
    toolset, *rest = result
    assert isinstance(toolset, _RecorderToolset)
    # The tool order and identity after the toolset is untouched.
    assert rest == list(QA_TOOLS)

    # Overlap resolution: inside_skill + formatting_skill survive;
    # outside_skill is dropped because `nonexistent_tool_xyz` is not
    # in QA_TOOLS' name set.
    surviving_names = {s.frontmatter.name for s in toolset.skills}
    assert surviving_names == {"inside-skill", "formatting-skill"}


# --- Bonus sanity: the flag-off path is a pure pass-through -----------------


def test_maybe_wrap_with_skills_flag_off_returns_input_unchanged():
    """When `qa_skills_enabled=False`, `_maybe_wrap_with_skills` must
    return the exact input list (identity / order preserved). Pinned to
    catch accidental wrapping regressions.
    """
    fake_settings = type(
        "FakeSettings",
        (),
        {"qa_skills_enabled": False, "qa_new_prompt": False},
    )()
    with patch(
        "beever_atlas.infra.config.get_settings",
        return_value=fake_settings,
    ):
        result = _maybe_wrap_with_skills(list(QA_TOOLS))

    assert result == list(QA_TOOLS)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
