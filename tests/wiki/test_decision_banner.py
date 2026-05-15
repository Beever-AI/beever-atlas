"""Tests for ``decision_banner`` module — Phase 4 archetype-aware
spotlight that surfaces a single decision on Decision-archetype pages.

Today the builder uses ONLY existing fields on AtomicFact (Phase 3
extraction enrichment hasn't shipped). The placeholder fields
(``rationale``, ``alternatives_rejected``, ``consequences_open``)
emit ``null`` / ``[]`` so the schema is forward-compatible.
"""

from __future__ import annotations

from beever_atlas.wiki.modules.decision_banner import (
    _iso_date,
    _split_sentence,
    build_decision_banner_data,
)
from beever_atlas.wiki.modules.planner import _derive_archetype, compute_signals


def _decision_fact(
    *,
    fact_id: str = "f_1",
    text: str = "Adopt a Copyright-assignment CLA. It provides relicensing flexibility.",
    importance: str = "critical",
    author: str = "Jacky Chan",
    ts: str = "2026-04-29T10:32:00Z",
) -> dict:
    return {
        "fact_id": fact_id,
        "memory_text": text,
        "fact_type": "decision",
        "importance": importance,
        "author_name": author,
        "message_ts": ts,
    }


# ---------------------------------------------------------------------------
# build_decision_banner_data — happy paths
# ---------------------------------------------------------------------------


def test_returns_full_payload_for_two_sentence_decision() -> None:
    """A decision fact with two sentences splits into headline + body."""
    out = build_decision_banner_data([_decision_fact()])
    assert out["renderer_kind"] == "frontend"
    assert out["label"] == "Decision"
    assert out["decision"] == "Adopt a Copyright-assignment CLA."
    assert out["body"] == "It provides relicensing flexibility."
    assert out["decided_by"] == {"name": "Jacky Chan", "fact_id": "f_1"}
    assert out["decided_at"] == "2026-04-29"
    assert out["fact_id"] == "f_1"


def test_capitalizes_first_letter_of_decision() -> None:
    """The headline MUST start capitalised even if the source text is
    lowercase — chat messages frequently start mid-sentence."""
    fact = _decision_fact(text="adopt the new schema. Migration starts Friday.")
    out = build_decision_banner_data([fact])
    assert out["decision"][0].isupper()
    assert out["decision"] == "Adopt the new schema."


def test_single_sentence_decision_has_empty_body() -> None:
    """A single-sentence decision has no body paragraph — the frontend
    hides the body row entirely when ``body`` is empty."""
    fact = _decision_fact(text="Adopt the Copyright-assignment CLA.")
    out = build_decision_banner_data([fact])
    assert out["decision"] == "Adopt the Copyright-assignment CLA."
    assert out["body"] == ""


def test_no_terminator_falls_back_to_whole_text_as_headline() -> None:
    """Defensive: messages without sentence terminators (rare but seen
    in chat) treat the whole text as the headline with no body."""
    fact = _decision_fact(text="adopt the cla")
    out = build_decision_banner_data([fact])
    assert out["decision"] == "Adopt the cla"
    assert out["body"] == ""


def test_phase_3_placeholders_default_to_null_and_empty() -> None:
    """``rationale`` is ``None``; ``alternatives_rejected`` and
    ``consequences_open`` are empty lists when the fact carries no
    Phase 3 enrichment — backward compatibility for pre-Phase-3
    documents."""
    out = build_decision_banner_data([_decision_fact()])
    assert out["rationale"] is None
    assert out["alternatives_rejected"] == []
    assert out["consequences_open"] == []


# ---------------------------------------------------------------------------
# Phase 3 — populated rationale / alternatives / consequences
# ---------------------------------------------------------------------------


def test_phase_3_populates_rationale_from_structured_field() -> None:
    """When the extractor populates ``rationale``, the banner surfaces
    it on the ``rationale`` payload key (frontend renders the
    'Because:' row)."""
    fact = _decision_fact()
    fact["rationale"] = "Provides relicensing flexibility for commercial forks."
    out = build_decision_banner_data([fact])
    assert out["rationale"] == "Provides relicensing flexibility for commercial forks."


def test_phase_3_populates_alternatives_from_structured_field() -> None:
    """``alternatives_considered`` (extractor field) flows to
    ``alternatives_rejected`` (banner payload key, frontend-facing
    name) preserving order and content."""
    fact = _decision_fact()
    fact["alternatives_considered"] = ["DCO", "License-grant CLA"]
    out = build_decision_banner_data([fact])
    assert out["alternatives_rejected"] == ["DCO", "License-grant CLA"]


def test_phase_3_populates_consequences_open_from_structured_field() -> None:
    """``consequences_open`` flows through unchanged."""
    fact = _decision_fact()
    fact["consequences_open"] = [
        "Will external contributors hesitate to sign?",
        "Need CLA bot before public PRs",
    ]
    out = build_decision_banner_data([fact])
    assert out["consequences_open"] == [
        "Will external contributors hesitate to sign?",
        "Need CLA bot before public PRs",
    ]


def test_phase_3_filters_empty_string_items_from_lists() -> None:
    """Defensive: malformed extraction may emit empty / whitespace
    items. The builder strips and skips them rather than rendering a
    blank chip."""
    fact = _decision_fact()
    fact["alternatives_considered"] = ["DCO", "  ", "", "License-grant CLA"]
    fact["consequences_open"] = ["", "Will it work?", "   "]
    out = build_decision_banner_data([fact])
    assert out["alternatives_rejected"] == ["DCO", "License-grant CLA"]
    assert out["consequences_open"] == ["Will it work?"]


def test_phase_3_handles_non_list_alternatives_defensively() -> None:
    """If the extractor accidentally emits a string instead of a list
    for ``alternatives_considered``, the builder returns ``[]``
    rather than crashing — same defensive pattern used elsewhere."""
    fact = _decision_fact()
    fact["alternatives_considered"] = "DCO, License-grant CLA"  # wrong type
    fact["consequences_open"] = None
    out = build_decision_banner_data([fact])
    assert out["alternatives_rejected"] == []
    assert out["consequences_open"] == []


def test_phase_3_rationale_empty_string_becomes_null() -> None:
    """An explicit empty / whitespace ``rationale`` collapses to
    ``None`` so the frontend hides the row consistently."""
    fact = _decision_fact()
    fact["rationale"] = "   "
    out = build_decision_banner_data([fact])
    assert out["rationale"] is None


def test_phase_3_full_payload_renders_all_three_rows() -> None:
    """End-to-end: a decision fact with every Phase 3 field populated
    surfaces all three rows on the banner payload."""
    fact = _decision_fact(text="Adopt CLA. Use Copyright-assignment.")
    fact["rationale"] = "Provides relicensing flexibility."
    fact["alternatives_considered"] = ["DCO", "License-grant CLA"]
    fact["consequences_open"] = ["Will contributors hesitate?"]
    out = build_decision_banner_data([fact])
    assert out["decision"] == "Adopt CLA."
    assert out["rationale"] == "Provides relicensing flexibility."
    assert out["alternatives_rejected"] == ["DCO", "License-grant CLA"]
    assert out["consequences_open"] == ["Will contributors hesitate?"]


# ---------------------------------------------------------------------------
# build_decision_banner_data — selection logic
# ---------------------------------------------------------------------------


def test_picks_highest_importance_decision_when_multiple_exist() -> None:
    """When several decisions are present, the banner spotlights the
    HIGHEST-importance one. Ties broken by longer text."""
    facts = [
        _decision_fact(fact_id="f_low", text="Use semicolons.", importance="low"),
        _decision_fact(
            fact_id="f_crit",
            text="Adopt a Copyright-assignment CLA.",
            importance="critical",
        ),
        _decision_fact(
            fact_id="f_med",
            text="Pin Python 3.12.",
            importance="medium",
        ),
    ]
    out = build_decision_banner_data(facts)
    assert out["fact_id"] == "f_crit"


def test_ignores_non_decision_facts() -> None:
    """The picker filters by ``fact_type == "decision"`` — observations,
    questions, and action_items are skipped."""
    facts = [
        {
            "fact_id": "f_obs",
            "memory_text": "Observation only.",
            "fact_type": "observation",
            "importance": "high",
        },
        _decision_fact(fact_id="f_dec", text="Adopt CLA."),
    ]
    out = build_decision_banner_data(facts)
    assert out["fact_id"] == "f_dec"


def test_returns_empty_payload_when_no_decision_present() -> None:
    """Defensive — if the planner picked the module but the cluster
    has no decision-typed fact, the builder emits the empty shape and
    the frontend renders nothing."""
    facts = [
        {
            "fact_id": "f_obs",
            "memory_text": "Observation only.",
            "fact_type": "observation",
        },
    ]
    out = build_decision_banner_data(facts)
    assert out["decision"] == ""
    assert out["fact_id"] == ""
    assert out["decided_at"] == ""


def test_handles_empty_input_gracefully() -> None:
    """No facts at all — empty payload, no exception."""
    assert build_decision_banner_data([])["decision"] == ""
    assert build_decision_banner_data(None)["decision"] == ""


def test_member_facts_used_as_fallback_when_facts_empty() -> None:
    """When ``facts`` is empty but ``member_facts`` carries the
    decision (common in some cluster shapes), the fallback pool kicks
    in."""
    out = build_decision_banner_data(
        facts=[],
        member_facts=[_decision_fact(fact_id="f_member")],
    )
    assert out["fact_id"] == "f_member"


# ---------------------------------------------------------------------------
# Helper functions — tested separately so failures pinpoint the issue
# ---------------------------------------------------------------------------


def test_split_sentence_on_terminators() -> None:
    assert _split_sentence("Hello world. More text.") == ("Hello world.", "More text.")
    assert _split_sentence("Question? Yes.") == ("Question?", "Yes.")
    assert _split_sentence("Single.") == ("Single.", "")
    assert _split_sentence("No terminator") == ("No terminator", "")
    assert _split_sentence("") == ("", "")
    assert _split_sentence("   ") == ("", "")


def test_iso_date_extraction() -> None:
    assert _iso_date("2026-04-29T10:32:00Z") == "2026-04-29"
    assert _iso_date("2026-04-29") == "2026-04-29"
    assert _iso_date("") == ""
    assert _iso_date("not a date") == ""
    assert _iso_date("April 29, 2026") == ""


# ---------------------------------------------------------------------------
# Archetype-derivation — the predicate that controls when the banner fires
# ---------------------------------------------------------------------------


def test_derive_archetype_picks_decision_when_density_high_and_few_facts() -> None:
    """Decision archetype when the cluster has ≤16 facts AND
    decision_density ≥ 0.25 — the page IS the decision."""
    assert (
        _derive_archetype(
            fact_count=2,
            decision_count=1,
            tension_count=0,
            person_fact_count=0,
            media_count=0,
            child_count=0,
        )
        == "decision"
    )


def test_derive_archetype_falls_back_to_topic_when_decision_diluted() -> None:
    """A page with 1 decision among 20 observations stays Topic — the
    decision belongs in decision_log, not the spotlight banner. The
    fact_count cap (>16) is what blocks this scenario; the decision
    density (1/20 = 0.05) would also fail the ≥0.25 floor on its own."""
    assert (
        _derive_archetype(
            fact_count=20,
            decision_count=1,
            tension_count=0,
            person_fact_count=0,
            media_count=0,
            child_count=0,
        )
        == "topic"
    )


def test_derive_archetype_decision_for_9_fact_cla_style_page() -> None:
    """CLA Adoption-style page: 9 facts where 3 are decisions (and the
    rest are supporting context — alternatives, votes, rationale).
    This is the canonical case the loosened threshold catches —
    previously fact_count=9 hit the old ``fc <= 8`` cap and fell back
    to Topic, burying the decision in ``decision_log``."""
    assert (
        _derive_archetype(
            fact_count=9,
            decision_count=3,
            tension_count=0,
            person_fact_count=0,
            media_count=0,
            child_count=0,
        )
        == "decision"
    )


def test_derive_archetype_decision_for_15_fact_saas_pricing_style_page() -> None:
    """SaaS Pricing-style page: 12 facts with 4 decisions. Right at
    the inclusive ≤16 cap; density 4/12 ≈ 0.33 ≥ 0.25."""
    assert (
        _derive_archetype(
            fact_count=12,
            decision_count=4,
            tension_count=0,
            person_fact_count=0,
            media_count=0,
            child_count=0,
        )
        == "decision"
    )


def test_derive_archetype_topic_when_above_fact_cap() -> None:
    """A 17-fact page with even 5 decisions is Topic — wider than
    "centered on a decision". The cap is the safety net that prevents
    sprawling pages from claiming Decision archetype."""
    assert (
        _derive_archetype(
            fact_count=17,
            decision_count=5,
            tension_count=0,
            person_fact_count=0,
            media_count=0,
            child_count=0,
        )
        == "topic"
    )


def test_derive_archetype_topic_when_below_density_floor() -> None:
    """A 12-fact page with 2 decisions has density 2/12 ≈ 0.17, below
    the 0.25 floor — Topic, not Decision."""
    assert (
        _derive_archetype(
            fact_count=12,
            decision_count=2,
            tension_count=0,
            person_fact_count=0,
            media_count=0,
            child_count=0,
        )
        == "topic"
    )


def test_derive_archetype_resource_when_media_heavy() -> None:
    """Resource archetype when media items dominate the fact count."""
    assert (
        _derive_archetype(
            fact_count=4,
            decision_count=0,
            tension_count=0,
            person_fact_count=0,
            media_count=8,
            child_count=0,
        )
        == "resource"
    )


def test_derive_archetype_folder_when_children_present() -> None:
    """Folder archetype when child_count ≥ 2 (and no other archetype
    fires)."""
    assert (
        _derive_archetype(
            fact_count=2,
            decision_count=0,
            tension_count=0,
            person_fact_count=0,
            media_count=0,
            child_count=3,
        )
        == "folder"
    )


def test_derive_archetype_defaults_to_topic() -> None:
    """When no archetype rule matches, the default is Topic — every
    page has a fallback."""
    assert (
        _derive_archetype(
            fact_count=10,
            decision_count=0,
            tension_count=0,
            person_fact_count=0,
            media_count=1,
            child_count=0,
        )
        == "topic"
    )


def test_derive_archetype_returns_topic_on_malformed_input() -> None:
    """Defensive: malformed signal types fall back to Topic instead of
    raising. Planner runs untrusted LLM output through this — never
    crash."""
    # type: ignore[arg-type] — intentionally passing wrong types
    assert _derive_archetype("a", "b", "c", "d", "e", "f") == "topic"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compute_signals — exposes the archetype string the catalog predicate reads
# ---------------------------------------------------------------------------


def test_compute_signals_exposes_archetype_string() -> None:
    """``compute_signals`` MUST surface the derived archetype as a
    top-level signal so the catalog predicate can gate on it."""
    cluster = {
        "title": "CLA Adoption",
        "member_facts": [_decision_fact()],
        "child_count": 0,
    }
    signals = compute_signals(cluster=cluster, decisions=[{"text": "adopt CLA"}])
    assert "archetype" in signals
    assert signals["archetype"] in {
        "topic",
        "decision",
        "resource",
        "person",
        "tension",
        "folder",
    }


def test_compute_signals_includes_placeholder_signals() -> None:
    """Phase 3 will populate ``tension_count`` and ``person_fact_count``;
    Phase 4 prep ships them as 0 so consumers don't break when the real
    values arrive later."""
    signals = compute_signals(cluster={"member_facts": []})
    assert signals["tension_count"] == 0
    assert signals["person_fact_count"] == 0
