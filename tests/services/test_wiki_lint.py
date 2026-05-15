"""Unit tests for the wiki lint service (PR-G).

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/wiki-lint-and-tensions/``

Covers all four checks:
  * orphan detection — page references a deleted cluster
  * staleness scoring — page hasn't been edited in N days
  * duplicate-section detection — two pages cover overlapping content
  * bounded LLM coherence pass — at most ONE call per page
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from beever_atlas.models.persistence import WikiPage, WikiPageSection
from beever_atlas.services.wiki_lint import (
    LintReport,
    coherence_check_page,
    find_duplicate_sections,
    find_orphans,
    find_stale_pages,
    lint_channel_wiki,
)


def _page(
    page_id: str,
    *,
    sections: list[WikiPageSection] | None = None,
    updated_at: datetime | None = None,
) -> WikiPage:
    return WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id=page_id,
        title=page_id,
        slug=page_id.replace(":", "-"),
        sections=sections or [],
        updated_at=updated_at or datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# Orphan detection
# ---------------------------------------------------------------------------


def test_find_orphans_flags_pages_for_deleted_clusters() -> None:
    """Spec scenario: ``Orphan detection identifies pages with no live
    cluster reference``."""
    pages = [
        _page("topic:auth"),
        _page("topic:billing"),
        _page("entity:alice"),  # not a topic page; never an orphan candidate
    ]
    findings = find_orphans(pages, live_cluster_ids={"auth"})
    assert len(findings) == 1
    assert findings[0].page_id == "topic:billing"
    assert findings[0].category == "orphan"
    assert findings[0].severity == "warning"


def test_find_orphans_returns_empty_when_all_clusters_live() -> None:
    pages = [_page("topic:auth"), _page("topic:billing")]
    findings = find_orphans(pages, live_cluster_ids={"auth", "billing"})
    assert findings == []


def test_find_orphans_skips_non_topic_pages() -> None:
    """Entity / decisions / faq pages aren't governed by cluster_id."""
    pages = [_page("entity:alice"), _page("decisions"), _page("faq")]
    findings = find_orphans(pages, live_cluster_ids=set())
    assert findings == []


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_find_stale_pages_flags_old_pages() -> None:
    """Spec scenario: ``Staleness scoring uses existing _compute_staleness``."""
    now = datetime(2026, 5, 1, tzinfo=UTC)
    pages = [
        _page("recent", updated_at=now - timedelta(days=5)),
        _page("stale-1", updated_at=now - timedelta(days=45)),
        _page("stale-2", updated_at=now - timedelta(days=120)),
    ]
    findings = find_stale_pages(pages, threshold_days=30, now=now)
    assert {f.page_id for f in findings} == {"stale-1", "stale-2"}
    assert all(f.severity == "info" for f in findings)


def test_find_stale_pages_returns_empty_when_all_recent() -> None:
    now = datetime(2026, 5, 1, tzinfo=UTC)
    pages = [_page("p1", updated_at=now - timedelta(days=1))]
    assert find_stale_pages(pages, threshold_days=30, now=now) == []


# ---------------------------------------------------------------------------
# Duplicate sections
# ---------------------------------------------------------------------------


def test_find_duplicate_sections_flags_overlapping_content() -> None:
    """Spec scenario: ``Duplicate-section detection uses cosine on
    embedded section text`` — covered structurally here via the
    word-set signature heuristic."""
    shared_content = (
        "alice owns the authentication service we deployed last sprint "
        "with three new oauth providers wired in"
    )
    pages = [
        _page(
            "topic:auth",
            sections=[WikiPageSection(id="overview", title="Overview", content_md=shared_content)],
        ),
        _page(
            "entity:alice",
            sections=[WikiPageSection(id="role", title="Role", content_md=shared_content)],
        ),
    ]
    findings = find_duplicate_sections(pages)
    # The first occurrence stays canonical; the second is flagged.
    assert len(findings) == 1
    assert findings[0].category == "duplicate_section"


def test_find_duplicate_sections_skips_short_sections() -> None:
    """Tiny sections produce too-noisy a signature to be a useful
    duplicate signal."""
    pages = [
        _page("a", sections=[WikiPageSection(id="x", content_md="hi")]),
        _page("b", sections=[WikiPageSection(id="y", content_md="hi")]),
    ]
    findings = find_duplicate_sections(pages)
    assert findings == []


def test_find_duplicate_sections_reports_co_occurrences() -> None:
    """Three pages with identical content → two of them flagged."""
    text = "alice and bob shipped the new authentication architecture last quarter together"
    pages = [
        _page("a", sections=[WikiPageSection(id="x", content_md=text)]),
        _page("b", sections=[WikiPageSection(id="x", content_md=text)]),
        _page("c", sections=[WikiPageSection(id="x", content_md=text)]),
    ]
    findings = find_duplicate_sections(pages)
    assert len(findings) == 2


# ---------------------------------------------------------------------------
# Coherence pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coherence_check_flags_empty_sections_in_fallback_mode() -> None:
    """Spec contract: ``Bounded LLM coherence pass per page`` — when
    no LLM provider is wired, the structural fallback flags sections
    with a title but empty content_md (a likely placeholder)."""
    page = _page(
        "topic:auth",
        sections=[
            WikiPageSection(id="overview", title="Overview", content_md="real content"),
            WikiPageSection(id="decisions", title="Decisions", content_md=""),
        ],
    )
    findings = await coherence_check_page(page, llm_provider=None)
    assert len(findings) == 1
    assert findings[0].section_id == "decisions"


@pytest.mark.asyncio
async def test_coherence_check_does_not_flag_sections_with_content() -> None:
    page = _page(
        "topic:auth",
        sections=[
            WikiPageSection(id="overview", title="Overview", content_md="x"),
        ],
    )
    findings = await coherence_check_page(page, llm_provider=None)
    assert findings == []


# ---------------------------------------------------------------------------
# Aggregator — lint_channel_wiki
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lint_channel_wiki_returns_empty_for_healthy_channel() -> None:
    """Spec scenario: ``Lint endpoint returns findings for a healthy channel``."""
    page_store = AsyncMock()
    page_store.list_pages = AsyncMock(
        return_value=[
            _page(
                "topic:auth",
                sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
            )
        ]
    )
    report = await lint_channel_wiki(
        channel_id="C1",
        page_store=page_store,
        live_cluster_ids={"auth"},
    )
    assert isinstance(report, LintReport)
    assert report.findings == []
    assert report.pages_scanned == 1


@pytest.mark.asyncio
async def test_lint_channel_wiki_returns_findings_for_degraded_channel() -> None:
    """Spec scenario: ``Lint endpoint returns findings for a degraded channel``."""
    now = datetime(2026, 5, 1, tzinfo=UTC)
    page_store = AsyncMock()
    page_store.list_pages = AsyncMock(
        return_value=[
            # Orphan
            _page(
                "topic:dead-cluster",
                sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
            ),
            # Stale
            _page(
                "topic:auth",
                sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
                updated_at=now - timedelta(days=120),
            ),
        ]
    )
    report = await lint_channel_wiki(
        channel_id="C1",
        page_store=page_store,
        live_cluster_ids={"auth"},
    )
    categories = {f.category for f in report.findings}
    assert "orphan" in categories
    assert "stale" in categories


@pytest.mark.asyncio
async def test_lint_channel_wiki_sorts_by_severity() -> None:
    """Errors should appear before warnings should appear before info."""
    page_store = AsyncMock()
    page_store.list_pages = AsyncMock(
        return_value=[
            _page(
                "topic:dead-cluster",  # orphan → warning
                sections=[
                    WikiPageSection(id="overview", title="Overview", content_md="x"),
                    WikiPageSection(id="empty", title="Empty", content_md=""),  # info
                ],
            ),
        ]
    )
    report = await lint_channel_wiki(
        channel_id="C1",
        page_store=page_store,
        live_cluster_ids=set(),
    )
    severities = [f.severity for f in report.findings]
    assert severities == sorted(severities, key=lambda s: {"error": 0, "warning": 1, "info": 2}[s])


@pytest.mark.asyncio
async def test_lint_channel_wiki_with_run_coherence_check_off_skips_coherence() -> None:
    page_store = AsyncMock()
    page_store.list_pages = AsyncMock(
        return_value=[
            _page(
                "topic:auth",
                sections=[WikiPageSection(id="empty", title="Empty", content_md="")],
            )
        ]
    )
    report = await lint_channel_wiki(
        channel_id="C1",
        page_store=page_store,
        live_cluster_ids={"auth"},
        run_coherence_check=False,
    )
    assert all(f.category != "coherence" for f in report.findings)


@pytest.mark.asyncio
async def test_lint_channel_wiki_continues_on_per_page_coherence_failure() -> None:
    """Spec contract: a coherence-pass exception on one page MUST NOT
    break the whole report. Partial findings are better than none."""
    pages = [
        _page("topic:auth", sections=[WikiPageSection(id="x", content_md="ok")]),
        _page("topic:bug", sections=[WikiPageSection(id="x", content_md="ok")]),
    ]
    page_store = AsyncMock()
    page_store.list_pages = AsyncMock(return_value=pages)

    # Patch coherence_check_page to raise on the first page.
    from unittest.mock import patch

    counter = {"calls": 0}

    async def _flaky(page, llm_provider=None):
        counter["calls"] += 1
        if counter["calls"] == 1:
            raise RuntimeError("flaky LLM call")
        return []

    with patch(
        "beever_atlas.services.wiki_lint.coherence_check_page",
        side_effect=_flaky,
    ):
        report = await lint_channel_wiki(
            channel_id="C1",
            page_store=page_store,
            live_cluster_ids={"auth", "bug"},
        )
    # Did not raise; both pages were scanned.
    assert report.pages_scanned == 2
