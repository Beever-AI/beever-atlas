"""Tests for the marker substitution pipeline that turns
``<<MODULE:id>>`` markers into rendered content (``render.py``).

The substitution is invoked by the orchestrator's single-call flow —
its correctness is independently testable here without any LLM.
"""

from __future__ import annotations

import pytest

from beever_atlas.wiki.render import (
    MODULE_MARKER_RE,
    ModuleSubstitutionError,
    substitute_module_markers,
)


# ---------------------------------------------------------------------------
# Marker regex
# ---------------------------------------------------------------------------


def test_module_marker_regex_matches_simple_marker() -> None:
    matches = list(MODULE_MARKER_RE.finditer("Body <<MODULE:key_facts>> tail"))
    assert len(matches) == 1
    assert matches[0].group(1) == "key_facts"
    assert matches[0].group(2) is None


def test_module_marker_regex_matches_marker_with_ref() -> None:
    matches = list(MODULE_MARKER_RE.finditer("<<MODULE:media_inline:m_42>>"))
    assert len(matches) == 1
    assert matches[0].group(1) == "media_inline"
    assert matches[0].group(2) == "m_42"


def test_module_marker_regex_ignores_non_module_double_angle() -> None:
    """Don't match ordinary << or other token patterns the wiki uses
    (e.g., the existing ``<<CHILDREN_TOC>>`` marker)."""
    matches = list(MODULE_MARKER_RE.finditer("<<CHILDREN_TOC>> and <<KEY_FACTS_TABLE>>"))
    assert matches == []


# ---------------------------------------------------------------------------
# substitute_module_markers
# ---------------------------------------------------------------------------


def test_substitute_replaces_simple_marker() -> None:
    body = "Intro\n\n<<MODULE:key_facts>>\n\nOutro"
    rendered = {"key_facts": "| Fact | Source |\n|------|--------|\n| X | A |"}
    out = substitute_module_markers(body, rendered)
    assert "<<MODULE:" not in out
    assert "| Fact |" in out
    assert "Intro" in out
    assert "Outro" in out


def test_substitute_replaces_marker_with_ref() -> None:
    body = "<<MODULE:media_inline:m_42>>"
    rendered = {"media_inline:m_42": "[image-block]"}
    out = substitute_module_markers(body, rendered)
    assert out == "[image-block]"


def test_substitute_falls_back_from_ref_to_bare() -> None:
    """When the composite key isn't supplied, falls back to the bare
    module ID — useful when a module renders the same content for
    every ref (rare, but supported)."""
    body = "<<MODULE:media_inline:m_42>>"
    rendered = {"media_inline": "<generic-inline>"}
    out = substitute_module_markers(body, rendered)
    assert out == "<generic-inline>"


def test_substitute_strips_known_module_with_no_rendered_entry() -> None:
    """Planner picked the module but rendered content is empty —
    silently drop the marker rather than emitting it raw. The
    orchestrator logs a structured event so soak telemetry can spot
    systematic data-contract gaps."""
    body = "Before <<MODULE:decision_log>> after"
    out = substitute_module_markers(body, {})  # decision_log is known but empty
    assert "<<MODULE:" not in out
    assert "Before" in out
    assert "after" in out


def test_substitute_raises_on_unknown_module_id() -> None:
    """Unknown module IDs are left in place by the substitutor and
    surface as a ModuleSubstitutionError. Caller falls back to the
    legacy renderer rather than shipping a half-rendered page."""
    body = "<<MODULE:totally_made_up>>"
    with pytest.raises(ModuleSubstitutionError):
        substitute_module_markers(body, {})


def test_substitute_handles_multiple_markers_in_order() -> None:
    body = "Intro.\n\n<<MODULE:key_facts>>\n\nMiddle prose.\n\n<<MODULE:decision_log>>\n\nOutro."
    rendered = {
        "key_facts": "FACTS_TABLE",
        "decision_log": "DECISIONS_TABLE",
    }
    out = substitute_module_markers(body, rendered)
    assert out.index("FACTS_TABLE") < out.index("Middle prose")
    assert out.index("Middle prose") < out.index("DECISIONS_TABLE")
    assert "<<MODULE:" not in out
