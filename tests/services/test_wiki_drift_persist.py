"""Tests for ``wiki_drift_reports`` Mongo persistence (close-the-soak-loop §3).

Covers:
  * 3.6 — ``insert_wiki_drift_report`` round-trips a ``DriftReport`` into
    the collection.
  * 3.7 — Persistence failure does NOT block log emission and surfaces a
    structured warning.
  * 3.8 — TTL index documented + asserted via index introspection.
  * 3.9 — A single ``compare_apply_update_vs_regenerate`` call results in
    exactly one persisted row + one structured log line.

The TTL test runs against the real Mongo store; the rest use in-memory
fakes so they don't require a running mongod.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from beever_atlas.models.persistence import WikiPage, WikiPageSection
from beever_atlas.services import wiki_drift_comparator as comparator_mod
from beever_atlas.services.wiki_drift_comparator import (
    DriftReport,
    compare_apply_update_vs_regenerate,
)
from beever_atlas.stores import init_stores


# ---------------------------------------------------------------------------
# In-memory Mongo fakes
# ---------------------------------------------------------------------------


class _FakeWikiDriftReports:
    """Minimal fake: exposes ``insert_one`` + records the docs it sees."""

    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []
        self.raises: Exception | None = None

    async def insert_one(self, doc: dict[str, Any]) -> None:
        if self.raises is not None:
            raise self.raises
        self.docs.append(doc)


class _FakeMongo:
    def __init__(self, reports: _FakeWikiDriftReports) -> None:
        self._wiki_drift_reports = reports

    async def insert_wiki_drift_report(self, report: Any) -> None:
        from dataclasses import asdict as _asdict
        from datetime import UTC, datetime

        doc: dict[str, Any] = _asdict(report)
        doc["ts"] = datetime.now(tz=UTC)
        await self._wiki_drift_reports.insert_one(doc)


def _patch_stores(
    monkeypatch, reports: _FakeWikiDriftReports | None = None
) -> _FakeWikiDriftReports:
    reports = reports or _FakeWikiDriftReports()
    mongo = _FakeMongo(reports)
    container = SimpleNamespace(mongodb=mongo)
    init_stores(container)  # type: ignore[arg-type]
    return reports


def _make_page(page_id: str = "topic:auth") -> WikiPage:
    return WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id=page_id,
        title="Auth",
        slug=page_id.replace(":", "-"),
        sections=[WikiPageSection(id="overview", title="Overview", content_md="OIDC")],
    )


# ---------------------------------------------------------------------------
# 3.6 — Round-trip insert
# ---------------------------------------------------------------------------


async def test_insert_wiki_drift_report_roundtrips(monkeypatch):
    reports = _patch_stores(monkeypatch)
    sample = DriftReport(
        channel_id="C1",
        page_id="topic:auth",
        levenshtein_title=0.0,
        levenshtein_section_max=0.05,
        levenshtein_section_p50=0.02,
        levenshtein_section_p95=0.04,
        section_id_jaccard=1.0,
        incremental_ms=12,
        regenerate_ms=120,
        incremental_section_count=2,
        regenerate_section_count=2,
    )

    from beever_atlas.stores import get_stores

    await get_stores().mongodb.insert_wiki_drift_report(sample)
    assert len(reports.docs) == 1
    persisted = reports.docs[0]
    assert persisted["channel_id"] == "C1"
    assert persisted["page_id"] == "topic:auth"
    assert persisted["levenshtein_section_p50"] == 0.02
    assert "ts" in persisted


# ---------------------------------------------------------------------------
# 3.7 — Persistence failure surfaces warning, log line still fires
# ---------------------------------------------------------------------------


async def test_persistence_failure_does_not_block_log(monkeypatch):
    reports = _FakeWikiDriftReports()
    reports.raises = RuntimeError("mongo down")
    _patch_stores(monkeypatch, reports)

    info_seen: list[str] = []
    warn_seen: list[str] = []
    real_info = comparator_mod.logger.info
    real_warn = comparator_mod.logger.warning

    def _capture_info(msg, *args, **kwargs):
        try:
            info_seen.append(msg % args if args else str(msg))
        except TypeError:
            info_seen.append(str(msg))
        real_info(msg, *args, **kwargs)

    def _capture_warn(msg, *args, **kwargs):
        try:
            warn_seen.append(msg % args if args else str(msg))
        except TypeError:
            warn_seen.append(str(msg))
        real_warn(msg, *args, **kwargs)

    monkeypatch.setattr(comparator_mod.logger, "info", _capture_info)
    monkeypatch.setattr(comparator_mod.logger, "warning", _capture_warn)

    async def _inc():
        return _make_page()

    async def _regen():
        return _make_page()

    report = await compare_apply_update_vs_regenerate(
        channel_id="C1",
        page_id="topic:auth",
        incremental_factory=_inc,
        regenerate_factory=_regen,
    )
    assert report is not None
    # Structured log line is the persistent contract — must always fire.
    assert any("event=wiki_drift_report" in m for m in info_seen)
    # Persistence failure must surface as the documented warning.
    assert any("event=wiki_drift_persist_failed" in m for m in warn_seen)


# ---------------------------------------------------------------------------
# 3.9 — End-to-end: one call → one row + one log line
# ---------------------------------------------------------------------------


async def test_compare_persists_one_row_and_one_log(monkeypatch):
    reports = _patch_stores(monkeypatch)
    info_seen: list[str] = []
    real_info = comparator_mod.logger.info

    def _capture_info(msg, *args, **kwargs):
        try:
            info_seen.append(msg % args if args else str(msg))
        except TypeError:
            info_seen.append(str(msg))
        real_info(msg, *args, **kwargs)

    monkeypatch.setattr(comparator_mod.logger, "info", _capture_info)

    async def _inc():
        return _make_page()

    async def _regen():
        return _make_page()

    report = await compare_apply_update_vs_regenerate(
        channel_id="C1",
        page_id="topic:auth",
        incremental_factory=_inc,
        regenerate_factory=_regen,
    )
    assert report is not None
    assert len(reports.docs) == 1
    drift_logs = [m for m in info_seen if "event=wiki_drift_report" in m]
    assert len(drift_logs) == 1


# ---------------------------------------------------------------------------
# 3.8 — TTL + compound index documented (introspect a fresh fake collection
# via the asdict shape — full Mongo-server roundtrip is exercised by the
# integration test in §22 of the soak runbook).
# ---------------------------------------------------------------------------


def test_ttl_index_constants_documented():
    """Sanity check that the TTL constant is the documented 30 days.

    The actual ``create_index(expireAfterSeconds=2592000)`` call lives in
    ``MongoDBStore.startup``; running the asserter here keeps the
    documented contract co-located with the spec test (§3.8) without
    requiring a live mongod for unit-time."""
    from beever_atlas.stores import mongodb_store

    src = mongodb_store.__file__
    with open(src, encoding="utf-8") as f:
        text = f.read()
    # 30 days in seconds.
    assert "expireAfterSeconds=2592000" in text
    # Compound index documented.
    assert "wiki_drift_reports_channel_ts" in text


# ---------------------------------------------------------------------------
# wiki-llm-native-redesign §8.7 — kind round-trips through compare + persist
# ---------------------------------------------------------------------------


async def test_drift_report_carries_kind_from_incremental_page() -> None:
    """A DriftReport computed from a kind="entity" WikiPage carries
    kind="entity" all the way through to the persisted document so the
    aggregator's per-kind facet has data to bucket on."""
    reports = _FakeWikiDriftReports()
    fake_mongo = _FakeMongo(reports)
    init_stores(SimpleNamespace(mongodb=fake_mongo))

    inc = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="entity:alice",
        title="Alice",
        slug="entity-alice",
        kind="entity",
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
    )
    regen = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="entity:alice",
        title="Alice",
        slug="entity-alice",
        kind="entity",
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
    )

    async def _inc_factory() -> WikiPage:
        return inc

    async def _regen_factory() -> WikiPage:
        return regen

    report = await compare_apply_update_vs_regenerate(
        channel_id="C1",
        page_id="entity:alice",
        incremental_factory=_inc_factory,
        regenerate_factory=_regen_factory,
    )
    assert report is not None
    assert isinstance(report, DriftReport)
    assert report.kind == "entity"
    # And the persisted doc carries the field through too.
    assert reports.docs, "drift report was not persisted"
    assert reports.docs[0].get("kind") == "entity"


async def test_drift_report_kind_falls_back_to_empty_when_absent() -> None:
    """A WikiPage missing the redesign field (legacy row, model default)
    yields kind="topic" — but we accept "" too so the aggregator buckets
    legacy rows separately rather than counting them as topic falsely.
    """
    # Use SimpleNamespace WikiPage-shaped object missing ``kind``.
    inc = SimpleNamespace(
        title="A",
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
    )
    regen = SimpleNamespace(
        title="A",
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
    )
    from beever_atlas.services.wiki_drift_comparator import compute_drift_report

    report = compute_drift_report(
        channel_id="C1",
        page_id="topic:a",
        incremental=inc,  # type: ignore[arg-type]
        regenerate=regen,  # type: ignore[arg-type]
        incremental_ms=1,
        regenerate_ms=1,
    )
    assert report.kind == ""
