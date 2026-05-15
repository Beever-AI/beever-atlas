"""Catalog enumeration + selection-predicate sanity tests for the
adaptive page modules system. Covers the spec scenario:
``Catalog enumerated for tests``.
"""

from __future__ import annotations

from beever_atlas.wiki.modules import (
    MODULE_CATALOG,
    get_module,
    is_known_module,
    list_module_ids,
)


def test_catalog_has_all_documented_modules() -> None:
    """The 27 module IDs declared in the spec MUST all exist in the
    catalog. Adding/removing modules requires updating the spec — this
    guard catches code-only drift."""
    expected = {
        # Content
        "hero_summary",
        "narrative_article",  # multi-section narrative article (wiki-narrative-articles)
        "decision_banner",  # archetype-aware spotlight (Phase 4 prep)
        "tension_callout",  # heuristic tension detector (Phase 4)
        "key_facts",
        "decision_log",
        "timeline",
        "comparison_matrix",
        "pros_cons",
        "quote_highlights",
        "flow_chart",
        "entity_diagram",
        "open_questions",
        "subpage_cards",
        "related_threads",
        # Media
        "media_hero",
        "media_inline",
        "media_gallery",
        "link_card",
        "pdf_preview",
        "video_embed",
        # Provenance + reading aids (content-fullness pass)
        "stat_strip",
        "acronym_legend",
        "provenance_drawer",
        # Folder-archetype dashboard modules (folder redesign)
        "folder_stats",
        "top_contributors",
        "cross_cutting_decisions",
    }
    assert set(MODULE_CATALOG.keys()) == expected


def test_each_catalog_entry_has_required_metadata() -> None:
    for spec in MODULE_CATALOG.values():
        assert spec.id, f"empty id on {spec}"
        assert spec.label, f"empty label on {spec.id}"
        assert spec.description, f"empty description on {spec.id}"
        assert spec.renderer_kind in {"python", "frontend"}, (
            f"invalid renderer_kind on {spec.id}: {spec.renderer_kind}"
        )
        assert callable(spec.eligible)


def test_is_known_module_o1_lookup() -> None:
    assert is_known_module("key_facts") is True
    assert is_known_module("decision_log") is True
    assert is_known_module("totally_made_up") is False


def test_get_module_returns_spec_or_none() -> None:
    spec = get_module("decision_log")
    assert spec is not None
    assert spec.id == "decision_log"
    assert get_module("nope") is None


def test_list_module_ids_stable_order() -> None:
    """Order is the dict-insertion order from MODULE_CATALOG. Used
    by the planner prompt to render the vocabulary block — keeping
    a stable order means deterministic prompts."""
    ids = list_module_ids()
    assert ids[0] == "hero_summary"  # first in catalog (always module #1)
    assert ids[1] == "narrative_article"  # multi-section article (Top of page when present)
    assert ids[2] == "decision_banner"  # archetype-aware spotlight (module #2 on Decision pages)
    assert ids[3] == "tension_callout"  # heuristic-detected contradicting position pair
    assert ids[4] == "key_facts"  # spine content begins after archetype-spotlights
    # Folder-archetype modules sit at the END of the catalog (after
    # ``provenance_drawer``); they only fire on folder index pages.
    assert ids[-1] == "cross_cutting_decisions"
    assert "provenance_drawer" in ids
    assert "folder_stats" in ids
    assert "top_contributors" in ids
    assert "tension_callout" in ids
    assert "narrative_article" in ids
    assert len(ids) == 27


def test_eligible_predicates_handle_missing_signals() -> None:
    """Predicates MUST be total over their input contract — a missing
    signal key (planner sent a partial signals dict) returns False,
    not KeyError. This is the worst failure mode if the predicate
    isn't defensive: planner crashes mid-run."""
    empty: dict = {}
    for spec in MODULE_CATALOG.values():
        # Should not raise.
        result = spec.eligible(empty)
        assert isinstance(result, bool)


def test_human_rules_cover_every_catalog_entry() -> None:
    """Regression guard: every module in the catalog MUST have a
    matching entry in ``_HUMAN_RULES`` (which renders into the
    planner prompt's vocabulary block). If a developer adds a new
    module to the catalog but forgets to add its human-readable
    rule, the LLM gets told to "pick at planner's discretion" — a
    silent quality regression. This test catches the drift."""
    from beever_atlas.wiki.modules.planner import _HUMAN_RULES

    catalog_ids = set(MODULE_CATALOG.keys())
    rule_ids = set(_HUMAN_RULES.keys())
    missing_rules = catalog_ids - rule_ids
    extra_rules = rule_ids - catalog_ids
    assert not missing_rules, (
        f"_HUMAN_RULES is missing entries for catalog modules: {sorted(missing_rules)}"
    )
    assert not extra_rules, (
        f"_HUMAN_RULES has stale entries for removed catalog modules: {sorted(extra_rules)}"
    )


def test_eligible_predicates_satisfied_when_signals_present() -> None:
    """Spot-check a few predicates with realistic signals."""
    decision_log = get_module("decision_log")
    assert decision_log is not None
    assert decision_log.eligible({"decision_count": 1}) is True
    assert decision_log.eligible({"decision_count": 0}) is False

    timeline = get_module("timeline")
    assert timeline is not None
    assert timeline.eligible({"event_count": 5, "event_span_days": 30}) is True
    assert timeline.eligible({"event_count": 3, "event_span_days": 30}) is False  # too few events
    assert timeline.eligible({"event_count": 5, "event_span_days": 7}) is False  # too short span

    related = get_module("related_threads")
    assert related is not None
    assert related.eligible({"related_topics": [{"score": 0.5}, {"score": 0.2}]}) is True
    assert related.eligible({"related_topics": [{"score": 0.2}]}) is False
    assert related.eligible({"related_topics": []}) is False
