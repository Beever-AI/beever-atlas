"""Tests for the ``stat_strip`` module.

Covers:
  - catalog entry shape + min-3-numeric-facts predicate
  - regex matching for plain integers ≥ 100, comma-grouped, k/M-suffixed,
    multi-currency
  - label extraction (1–3 words after the number, skip stopwords)
  - dedup by ``(value, label)`` so the same metric mentioned twice
    doesn't render two cards
  - cap at 5 stats per page
  - period (date range) computed from contributing facts
  - graceful empty inputs

Pure unit tests — no LLM, network, or DB.
"""

from __future__ import annotations

from beever_atlas.wiki.modules import MODULE_CATALOG
from beever_atlas.wiki.modules.stat_strip import (
    build_stat_strip_data,
    count_numeric_facts,
)


# ---------------------------------------------------------------------------
# Catalog entry
# ---------------------------------------------------------------------------


def test_stat_strip_in_catalog() -> None:
    assert "stat_strip" in MODULE_CATALOG
    spec = MODULE_CATALOG["stat_strip"]
    assert spec.id == "stat_strip"
    assert spec.label == "Stats"
    assert spec.renderer_kind == "frontend"


def test_stat_strip_predicate_requires_min_3_numeric_facts() -> None:
    spec = MODULE_CATALOG["stat_strip"]
    assert spec.eligible({"numeric_fact_count": 3}) is True
    assert spec.eligible({"numeric_fact_count": 10}) is True
    assert spec.eligible({"numeric_fact_count": 2}) is False
    assert spec.eligible({"numeric_fact_count": 0}) is False
    assert spec.eligible({}) is False


# ---------------------------------------------------------------------------
# count_numeric_facts — signal computation
# ---------------------------------------------------------------------------


def test_count_numeric_facts_matches_comma_grouped_integer() -> None:
    facts = [{"memory_text": "Generated 2,396 actions across the campaign."}]
    assert count_numeric_facts(facts) == 1


def test_count_numeric_facts_matches_k_suffix() -> None:
    facts = [{"memory_text": "Total 534k impressions on the launch."}]
    assert count_numeric_facts(facts) == 1


def test_count_numeric_facts_matches_currency_prefix() -> None:
    facts = [
        {"memory_text": "Equivalent to HK$130k in paid-media value."},
        {"memory_text": "Closed at $1,200 per seat."},
        {"memory_text": "Saved €42k annually."},
    ]
    assert count_numeric_facts(facts) == 3


def test_count_numeric_facts_matches_plain_int_at_or_above_100() -> None:
    facts = [
        {"memory_text": "We onboarded 250 new accounts."},  # 250 ≥ 100 → stat
        {"memory_text": "Only 5 people attended."},  # 5 < 100 → not a stat
        {"memory_text": "Three months of work."},  # no numeric → not a stat
    ]
    assert count_numeric_facts(facts) == 1


def test_count_numeric_facts_does_not_double_count_one_fact() -> None:
    """A fact with multiple numerics still counts as ONE numeric fact
    (the signal feeds an eligibility predicate, not a per-stat
    counter)."""
    facts = [{"memory_text": "534k impressions and HK$130k in paid-media."}]
    assert count_numeric_facts(facts) == 1


def test_count_numeric_facts_handles_empty_input() -> None:
    assert count_numeric_facts([]) == 0
    assert count_numeric_facts(None) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# build_stat_strip_data — payload shape + extraction
# ---------------------------------------------------------------------------


def test_build_extracts_value_and_label() -> None:
    facts = [
        {
            "fact_id": "f1",
            "memory_text": "Campaign produced 2,396 actions in week 1.",
            "importance": 9,
            "message_ts": "2026-04-26T10:00:00Z",
        }
    ]
    data = build_stat_strip_data(facts)
    assert data["label"] == "Stats"
    assert data["renderer_kind"] == "frontend"
    assert len(data["stats"]) == 1
    stat = data["stats"][0]
    assert stat["value"] == "2,396"
    assert stat["label"] == "actions"
    assert stat["fact_id"] == "f1"
    assert stat["raw_value"] == 2396.0


def test_build_handles_currency_with_k_suffix() -> None:
    facts = [
        {
            "fact_id": "f1",
            "memory_text": "HK$130k paid-media equivalent.",
            "importance": 8,
        },
    ]
    data = build_stat_strip_data(facts)
    assert len(data["stats"]) == 1
    stat = data["stats"][0]
    assert stat["value"] == "HK$130k"
    assert stat["raw_value"] == 130_000.0


def test_build_dedupes_same_value_and_label() -> None:
    """Two facts mentioning the same metric with the same trailing
    noun should produce one card, not two."""
    facts = [
        {"fact_id": "f1", "memory_text": "Saw 534k impressions."},
        {"fact_id": "f2", "memory_text": "Across the campaign 534k impressions."},
    ]
    data = build_stat_strip_data(facts)
    # Both mention "534k impressions" — dedup by (value, label) leaves
    # one stat card.
    values = [(s["value"], s["label"].lower()) for s in data["stats"]]
    assert ("534k", "impressions") in values
    assert len(values) == 1


def test_build_caps_at_5_stats() -> None:
    facts = [
        {
            "fact_id": f"f{i}",
            "memory_text": f"Metric #{i}: {1000 + i} widgets.",
            "importance": 9,
        }
        for i in range(10)
    ]
    data = build_stat_strip_data(facts)
    assert len(data["stats"]) == 5


def test_build_skips_stat_when_no_label_extractable() -> None:
    """A bare number with no following noun should NOT render as a
    card — labelless cards confuse readers."""
    facts = [
        {"fact_id": "f1", "memory_text": "534k"},  # no label
    ]
    data = build_stat_strip_data(facts)
    assert data["stats"] == []


def test_build_period_from_contributing_facts() -> None:
    facts = [
        {
            "fact_id": "f1",
            "memory_text": "Generated 2,396 actions in week 1.",
            "importance": 9,
            "message_ts": "2026-04-26T10:00:00Z",
        },
        {
            "fact_id": "f2",
            "memory_text": "Reached 534k impressions in week 2.",
            "importance": 9,
            "message_ts": "2026-05-02T10:00:00Z",
        },
    ]
    data = build_stat_strip_data(facts)
    assert data["period"]["from"] == "2026-04-26"
    assert data["period"]["to"] == "2026-05-02"


def test_build_handles_empty_inputs() -> None:
    assert build_stat_strip_data([])["stats"] == []
    assert build_stat_strip_data(None)["stats"] == []  # type: ignore[arg-type]


def test_build_skips_duration_words_in_label() -> None:
    """Numbers followed by date/time units (years, hours, minutes)
    are durations, not metrics — skip rather than surface as cards."""
    facts = [
        {"fact_id": "f1", "memory_text": "Process took 1,200 minutes total."},
    ]
    data = build_stat_strip_data(facts)
    assert data["stats"] == []


def test_build_strips_leading_filler_from_label() -> None:
    """Number followed by a stopword + noun should produce label
    starting at the noun."""
    facts = [
        {"fact_id": "f1", "memory_text": "Processed 2,396 of the requests."},
    ]
    data = build_stat_strip_data(facts)
    # The "of the" stopwords should be skipped — label is "requests".
    assert len(data["stats"]) == 1
    assert data["stats"][0]["label"] == "requests"


def test_build_high_importance_facts_win_seats() -> None:
    """When more than 5 distinct stats exist, the highest-importance
    facts should win the limited stat slots."""
    facts = [
        # 6 facts, only 5 stats fit — the importance=1 fact loses.
        {
            "fact_id": f"f{i}",
            "memory_text": f"Hit {1000 + i} new users this period.",
            "importance": 9 if i < 5 else 1,
        }
        for i in range(6)
    ]
    data = build_stat_strip_data(facts)
    assert len(data["stats"]) == 5
    # The dropped fact had importance=1.
    fact_ids = {s["fact_id"] for s in data["stats"]}
    assert "f5" not in fact_ids


# ---------------------------------------------------------------------------
# Planner-signal integration — compute_signals exposes numeric_fact_count
# and glossary_terms_used so the predicates fire correctly.
# ---------------------------------------------------------------------------


def test_compute_signals_exposes_numeric_fact_count() -> None:
    from beever_atlas.wiki.modules.planner import compute_signals

    cluster = {
        "title": "Campaign report",
        "member_facts": [
            {"memory_text": "Generated 2,396 actions."},
            {"memory_text": "Reached 534k impressions."},
            {"memory_text": "HK$130k paid-media equivalent."},
            {"memory_text": "No numbers in this fact at all."},
        ],
    }
    signals = compute_signals(cluster=cluster)
    assert signals["numeric_fact_count"] == 3


def test_compute_signals_exposes_glossary_terms_used() -> None:
    from beever_atlas.wiki.modules.planner import compute_signals

    glossary = [
        {"term": "MFA", "definition": "Multi-Factor Authentication"},
        {"term": "OIDC", "definition": "OpenID Connect"},
        {"term": "SAML", "definition": "unused"},
    ]
    cluster = {
        "title": "Auth migration",
        "member_facts": [
            {"memory_text": "We rolled out MFA org-wide."},
            {"memory_text": "OIDC was the chosen protocol."},
        ],
    }
    signals = compute_signals(cluster=cluster, glossary=glossary)
    assert signals["glossary_terms_used"] == 2


def test_compute_signals_glossary_zero_when_not_passed() -> None:
    """Passing no glossary should produce ``glossary_terms_used = 0``
    rather than raising."""
    from beever_atlas.wiki.modules.planner import compute_signals

    signals = compute_signals(cluster={"title": "T", "member_facts": [{"memory_text": "MFA used"}]})
    assert signals["glossary_terms_used"] == 0


# ---------------------------------------------------------------------------
# Phase 3 — structured-first numeric_values path
# ---------------------------------------------------------------------------


def test_build_prefers_structured_numeric_values_over_regex() -> None:
    """When a fact carries structured ``numeric_values``, the builder
    surfaces them directly without re-running the regex over
    memory_text. This avoids label-extraction misses on awkwardly-
    phrased text."""
    facts = [
        {
            "fact_id": "f1",
            "memory_text": "Numbers are buried in awkward phrasing.",
            "importance": 9,
            "message_ts": "2026-04-26T10:00:00Z",
            "numeric_values": [
                {
                    "label": "stars",
                    "value": "2,396",
                    "raw_value": 2396,
                    "unit": "stars",
                },
                {
                    "label": "paid-media equivalent",
                    "value": "HK$130k",
                    "raw_value": 130000,
                    "unit": "HKD",
                },
            ],
        }
    ]
    data = build_stat_strip_data(facts)
    assert len(data["stats"]) == 2
    assert data["stats"][0]["value"] == "2,396"
    assert data["stats"][0]["label"] == "stars"
    assert data["stats"][0]["raw_value"] == 2396
    assert data["stats"][0]["unit"] == "stars"
    assert data["stats"][1]["value"] == "HK$130k"
    assert data["stats"][1]["unit"] == "HKD"


def test_build_falls_back_to_regex_when_no_structured_values() -> None:
    """Pre-Phase-3 facts (no ``numeric_values`` key, or empty list)
    fall through to the legacy regex path. Verifies the structured-
    first short-circuit doesn't skip cases with regex-only data."""
    facts = [
        {
            "fact_id": "f1",
            "memory_text": "Generated 2,396 actions in week 1.",
            "importance": 9,
            "message_ts": "2026-04-26T10:00:00Z",
            # Note: no `numeric_values` key — pre-Phase-3 doc.
        },
        {
            "fact_id": "f2",
            "memory_text": "Reached 534k impressions in week 2.",
            "importance": 9,
            "message_ts": "2026-05-02T10:00:00Z",
            "numeric_values": [],  # explicit empty — also fall through
        },
    ]
    data = build_stat_strip_data(facts)
    # Regex path produces 2 stats from the two messages.
    assert len(data["stats"]) == 2
    values = {s["value"] for s in data["stats"]}
    assert values == {"2,396", "534k"}


def test_build_structured_dedups_by_value_and_label() -> None:
    """When two facts emit the same ``(value, label)`` structured
    pair, only one card surfaces — same dedup contract as the
    regex path."""
    facts = [
        {
            "fact_id": "f1",
            "memory_text": "Repo grew.",
            "numeric_values": [
                {"label": "stars", "value": "2,396", "raw_value": 2396, "unit": None}
            ],
        },
        {
            "fact_id": "f2",
            "memory_text": "Same repo, different fact.",
            "numeric_values": [
                {"label": "Stars", "value": "2,396", "raw_value": 2396, "unit": None}
            ],
        },
    ]
    data = build_stat_strip_data(facts)
    assert len(data["stats"]) == 1


def test_build_structured_caps_at_5() -> None:
    """The ``_MAX_STATS = 5`` cap applies to the structured path too."""
    facts = [
        {
            "fact_id": "f1",
            "memory_text": "Many metrics.",
            "numeric_values": [
                {
                    "label": f"metric{i}",
                    "value": f"{1000 + i}",
                    "raw_value": 1000 + i,
                    "unit": None,
                }
                for i in range(10)
            ],
        }
    ]
    data = build_stat_strip_data(facts)
    assert len(data["stats"]) == 5


def test_build_structured_skips_malformed_entries() -> None:
    """Defensive: missing ``value`` or ``label`` fields are skipped
    rather than crashing the builder."""
    facts = [
        {
            "fact_id": "f1",
            "memory_text": "Test.",
            "numeric_values": [
                {"value": "2,396"},  # no label
                {"label": "stars"},  # no value
                {"label": "actions", "value": "1,200", "raw_value": 1200, "unit": None},
                "not a dict",  # wrong type
            ],
        }
    ]
    data = build_stat_strip_data(facts)
    assert len(data["stats"]) == 1
    assert data["stats"][0]["label"] == "actions"


def test_build_structured_period_from_contributing_facts() -> None:
    """The ``period`` field uses contributing facts' message_ts for
    the structured path too — same as regex path."""
    facts = [
        {
            "fact_id": "f1",
            "memory_text": "Week 1.",
            "message_ts": "2026-04-26T10:00:00Z",
            "numeric_values": [
                {"label": "actions", "value": "2,396", "raw_value": 2396, "unit": None}
            ],
        },
        {
            "fact_id": "f2",
            "memory_text": "Week 2.",
            "message_ts": "2026-05-02T10:00:00Z",
            "numeric_values": [
                {"label": "impressions", "value": "534k", "raw_value": 534000, "unit": None}
            ],
        },
    ]
    data = build_stat_strip_data(facts)
    assert data["period"]["from"] == "2026-04-26"
    assert data["period"]["to"] == "2026-05-02"
