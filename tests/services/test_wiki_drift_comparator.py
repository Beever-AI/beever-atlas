"""Tests for ``services/wiki_drift_comparator.py``.

Covers §19 of oss-redesign-production-wiring — the page-voice drift A/B
comparator that gates flipping ``WIKI_MAINTENANCE_MODE=auto`` to default
ON. Tests verify the math (Levenshtein on title + sections, Jaccard on
section-id sets, percentiles) plus the orchestrator's failure-isolation
contract: a comparator failure MUST NOT propagate to the caller.

Convention: no ``@pytest.mark.asyncio`` decorators; ``pyproject.toml``
sets ``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from beever_atlas.models.persistence import WikiPage, WikiPageSection
from beever_atlas.services.wiki_drift_comparator import (
    _levenshtein,
    _normalized_distance,
    _percentile,
    compare_apply_update_vs_regenerate,
    compute_drift_report,
)


# ─────────────────────────────────────────────────────────────────────────────
# Math primitives
# ─────────────────────────────────────────────────────────────────────────────


def test_levenshtein_identical_strings_zero():
    assert _levenshtein("abc", "abc") == 0


def test_levenshtein_one_substitution():
    assert _levenshtein("abc", "abd") == 1


def test_levenshtein_one_insertion():
    assert _levenshtein("abc", "abcd") == 1


def test_levenshtein_empty_to_nonempty():
    assert _levenshtein("", "hello") == 5


def test_normalized_distance_identical_zero():
    assert _normalized_distance("hello", "hello") == 0.0


def test_normalized_distance_completely_different_one():
    # Two strings with no overlap, equal length → distance == length, normalized == 1.0
    assert _normalized_distance("aaaa", "bbbb") == 1.0


def test_normalized_distance_both_empty_zero():
    assert _normalized_distance("", "") == 0.0


def test_normalized_distance_one_empty_one():
    assert _normalized_distance("", "abc") == 1.0
    assert _normalized_distance("abc", "") == 1.0


def test_percentile_empty_list_zero():
    assert _percentile([], 0.5) == 0.0


def test_percentile_single_value():
    assert _percentile([0.42], 0.5) == 0.42


def test_percentile_p50_p95_on_known_distribution():
    values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    p50 = _percentile(values, 0.5)
    p95 = _percentile(values, 0.95)
    # p50 should sit around 0.55 (interpolated between 0.5 and 0.6)
    assert 0.5 < p50 < 0.6
    # p95 should sit very close to 1.0
    assert 0.9 < p95 <= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# compute_drift_report — full reports
# ─────────────────────────────────────────────────────────────────────────────


def _make_page(
    page_id: str,
    title: str,
    sections: list[tuple[str, str]],
) -> WikiPage:
    return WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id=page_id,
        title=title,
        slug=page_id.replace(":", "-"),
        sections=[
            WikiPageSection(id=sid, title=sid.title(), content_md=content)
            for sid, content in sections
        ],
    )


def test_drift_report_identical_pages_zero():
    inc = _make_page("topic:auth", "Auth", [("overview", "Use OIDC."), ("decisions", "Keycloak")])
    regen = _make_page("topic:auth", "Auth", [("overview", "Use OIDC."), ("decisions", "Keycloak")])
    report = compute_drift_report(
        channel_id="C1",
        page_id="topic:auth",
        incremental=inc,
        regenerate=regen,
        incremental_ms=42,
        regenerate_ms=420,
    )
    assert report.levenshtein_title == 0.0
    assert report.levenshtein_section_max == 0.0
    assert report.levenshtein_section_p50 == 0.0
    assert report.levenshtein_section_p95 == 0.0
    assert report.section_id_jaccard == 1.0


def test_drift_report_completely_different_titles_distance_one():
    inc = _make_page("topic:auth", "Authentication", [("overview", "Use OIDC.")])
    regen = _make_page("topic:auth", "Billing", [("overview", "Use OIDC.")])
    report = compute_drift_report(
        channel_id="C1",
        page_id="topic:auth",
        incremental=inc,
        regenerate=regen,
        incremental_ms=42,
        regenerate_ms=420,
    )
    # Title distance non-zero; section content unchanged so section distances zero.
    assert report.levenshtein_title > 0.5
    assert report.levenshtein_section_max == 0.0


def test_drift_report_section_id_jaccard():
    """If incremental has sections {a, b} and regenerate has {b, c}, Jaccard = 1/3."""
    inc = _make_page("topic:auth", "T", [("a", "x"), ("b", "y")])
    regen = _make_page("topic:auth", "T", [("b", "y"), ("c", "z")])
    report = compute_drift_report(
        channel_id="C1",
        page_id="topic:auth",
        incremental=inc,
        regenerate=regen,
        incremental_ms=42,
        regenerate_ms=420,
    )
    # Intersection {b}, union {a, b, c} → 1/3
    assert abs(report.section_id_jaccard - 1 / 3) < 0.01


def test_drift_report_section_only_on_regenerate_counts_as_full_divergence():
    """A section present in regenerate but missing from incremental contributes
    a 1.0 distance to the section distance distribution."""
    inc = _make_page("topic:auth", "T", [("a", "shared")])
    regen = _make_page("topic:auth", "T", [("a", "shared"), ("b", "extra")])
    report = compute_drift_report(
        channel_id="C1",
        page_id="topic:auth",
        incremental=inc,
        regenerate=regen,
        incremental_ms=42,
        regenerate_ms=420,
    )
    # Section "a" matches (distance 0); section "b" present only on regenerate
    # contributes 1.0. p95 over [0.0, 1.0] is just below 1.0 (interpolated).
    assert report.levenshtein_section_max == 1.0
    assert report.levenshtein_section_p95 > 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator — failure isolation
# ─────────────────────────────────────────────────────────────────────────────


async def test_compare_returns_none_on_factory_failure():
    """If either factory raises, the comparator returns None (does NOT
    propagate). The maintainer's primary path is unaffected."""

    async def _ok():
        return _make_page("topic:auth", "T", [("a", "x")])

    async def _bad():
        raise RuntimeError("regenerate kaboom")

    report = await compare_apply_update_vs_regenerate(
        channel_id="C1",
        page_id="topic:auth",
        incremental_factory=_ok,
        regenerate_factory=_bad,
    )
    assert report is None


async def test_compare_emits_drift_report_on_success(monkeypatch):
    """Successful run emits a structured log line tagged ``wiki_drift_report``."""
    seen_logs: list[str] = []

    async def _inc():
        return _make_page("topic:auth", "Auth", [("overview", "OIDC.")])

    async def _regen():
        return _make_page("topic:auth", "Auth", [("overview", "OIDC.")])

    from beever_atlas.services import wiki_drift_comparator as mod

    real_info = mod.logger.info

    def _capture(msg, *args, **kwargs):
        try:
            seen_logs.append(msg % args if args else msg)
        except TypeError:
            seen_logs.append(str(msg))
        real_info(msg, *args, **kwargs)

    monkeypatch.setattr(mod.logger, "info", _capture)

    report = await compare_apply_update_vs_regenerate(
        channel_id="C1",
        page_id="topic:auth",
        incremental_factory=_inc,
        regenerate_factory=_regen,
    )
    assert report is not None
    assert any("event=wiki_drift_report" in line for line in seen_logs)
    assert any("channel_id=C1" in line for line in seen_logs)
