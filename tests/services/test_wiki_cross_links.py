"""Tests for the `[[wikilink]]` parser, resolver, and Neo4j post-processor.

Covers tasks §4.7 / §4.8 / §4.9 / §4.10 from
``openspec/changes/wiki-llm-native-redesign/tasks.md``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from beever_atlas.models.persistence import WikiPage, WikiPageSection
from beever_atlas.services import wiki_maintainer as wm_mod
from beever_atlas.services.wiki_maintainer import (
    WikiMaintainer,
    _build_page_index,
    _parse_wikilinks,
    _resolve_wikilink_against_index,
)


# ---------------------------------------------------------------------------
# §4.7 — _parse_wikilinks
# ---------------------------------------------------------------------------


def test_parse_wikilinks_returns_titles_in_document_order() -> None:
    md = "See [[Authentication]] and [[Session Policy]] for context."
    assert _parse_wikilinks(md) == ["Authentication", "Session Policy"]


def test_parse_wikilinks_returns_empty_list_for_empty_input() -> None:
    assert _parse_wikilinks("") == []
    assert _parse_wikilinks("no brackets here") == []


def test_parse_wikilinks_ignores_unbalanced_brackets() -> None:
    md = "incomplete [[Foo and [trailing]] only [[Bar]]"
    out = _parse_wikilinks(md)
    # Only the well-formed [[Bar]] should land. The unbalanced
    # [[Foo and [trailing]] mixes single + double brackets so the regex
    # rejects it.
    assert "Bar" in out
    assert "Foo" not in out


def test_parse_wikilinks_strips_whitespace_inside_brackets() -> None:
    md = "[[  Auth  ]] and [[\nMulti-line title\n]]"
    out = _parse_wikilinks(md)
    assert "Auth" in out
    # Newline-containing content fails the regex (no internal newlines
    # allowed) — that's the documented contract.
    assert "Multi-line title" not in out


def test_parse_wikilinks_keeps_duplicates_in_doc_order() -> None:
    md = "[[Auth]] then [[Auth]] again then [[Other]]"
    assert _parse_wikilinks(md) == ["Auth", "Auth", "Other"]


# ---------------------------------------------------------------------------
# §4.8 — _resolve_wikilink: exact / case-insensitive / fuzzy
# ---------------------------------------------------------------------------


def _page_for_index(
    *, page_id: str, title: str, slug: str | None = None, updated_at: datetime | None = None
) -> WikiPage:
    return WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id=page_id,
        title=title,
        slug=slug or page_id.replace(":", "-"),
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
        updated_at=updated_at or datetime.now(tz=UTC),
    )


def test_resolve_wikilink_exact_title_match() -> None:
    pages = [_page_for_index(page_id="topic:auth", title="Authentication")]
    index = _build_page_index(pages)
    assert _resolve_wikilink_against_index("Authentication", index) == "topic-auth"


def test_resolve_wikilink_case_insensitive_match() -> None:
    pages = [_page_for_index(page_id="topic:auth", title="Authentication")]
    index = _build_page_index(pages)
    assert _resolve_wikilink_against_index("authentication", index) == "topic-auth"
    assert _resolve_wikilink_against_index("AUTHENTICATION", index) == "topic-auth"


def test_resolve_wikilink_plural_aware_match() -> None:
    pages = [_page_for_index(page_id="decisions", title="Decisions")]
    index = _build_page_index(pages)
    # Singular "Decision" should match the plural-titled page.
    assert _resolve_wikilink_against_index("Decision", index) == "decisions"


def test_resolve_wikilink_fuzzy_match_within_threshold() -> None:
    pages = [_page_for_index(page_id="topic:auth", title="Authentication")]
    index = _build_page_index(pages)
    # One-character typo well within the 0.85 difflib ratio.
    assert _resolve_wikilink_against_index("Authenticatoin", index) == "topic-auth"


def test_resolve_wikilink_returns_none_below_fuzzy_threshold() -> None:
    pages = [_page_for_index(page_id="topic:auth", title="Authentication")]
    index = _build_page_index(pages)
    # Far away — below 0.85.
    assert _resolve_wikilink_against_index("Logging", index) is None


def test_resolve_wikilink_tie_break_prefers_most_recently_updated() -> None:
    older = _page_for_index(
        page_id="topic:authA",
        title="Authentication",
        slug="authentication-old",
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    newer = _page_for_index(
        page_id="topic:authB",
        title="Authentication",
        slug="authentication-new",
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    index = _build_page_index([older, newer])
    # _build_page_index sorts by updated_at DESC and registers the
    # first occurrence per key, so the newer page wins on a tie.
    assert _resolve_wikilink_against_index("Authentication", index) == "authentication-new"


# ---------------------------------------------------------------------------
# §4.9 — Unresolvable title lands in cross_links_broken
# ---------------------------------------------------------------------------


async def test_persist_cross_links_resolved_and_broken_split_correctly() -> None:
    """Cross-link resolution writes resolved slugs to ``cross_links``
    and unresolvable titles to ``cross_links_broken`` (and NOT the
    other way around)."""
    other = _page_for_index(page_id="topic:auth", title="Authentication")

    page_store = AsyncMock()
    page_store.list_pages = AsyncMock(return_value=[other])
    maintainer = WikiMaintainer(page_store=page_store)

    page = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="entity:alice",
        title="Alice",
        slug="entity-alice",
        sections=[
            WikiPageSection(
                id="overview",
                title="Overview",
                content_md=(
                    "Alice owns [[Authentication]] and proposed [[Logging Strategy]] (no page yet)."
                ),
            )
        ],
    )

    resolved, broken = await maintainer._persist_cross_links(page, target_lang="en")
    assert resolved == {"Authentication": "topic-auth"}
    assert broken == ["Logging Strategy"]
    # And the page object was mutated in place — the caller will save
    # both fields in one Mongo write.
    assert page.cross_links == {"Authentication": "topic-auth"}
    assert page.cross_links_broken == ["Logging Strategy"]


async def test_persist_cross_links_skips_self_reference() -> None:
    """A page that mentions [[its own title]] does NOT cross-link to
    itself — self-edges are noise in the graph view."""
    self_page = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="topic:auth",
        title="Authentication",
        slug="topic-auth",
        sections=[
            WikiPageSection(
                id="overview",
                title="Overview",
                content_md="See [[Authentication]] for the canonical write-up.",
            )
        ],
    )
    page_store = AsyncMock()
    # ``list_pages`` returns the page itself — _build_page_index excludes
    # it via ``exclude_self_page_id``.
    page_store.list_pages = AsyncMock(return_value=[self_page])
    maintainer = WikiMaintainer(page_store=page_store)

    resolved, broken = await maintainer._persist_cross_links(self_page, target_lang="en")
    assert resolved == {}
    # The title fails to resolve against any OTHER page → broken.
    assert broken == ["Authentication"]


async def test_persist_cross_links_dedupes_repeated_references() -> None:
    other = _page_for_index(page_id="topic:auth", title="Authentication")
    page_store = AsyncMock()
    page_store.list_pages = AsyncMock(return_value=[other])
    maintainer = WikiMaintainer(page_store=page_store)

    page = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="topic:overview",
        title="Overview",
        slug="topic-overview",
        sections=[
            WikiPageSection(
                id="a",
                title="A",
                content_md="[[Authentication]] then [[Authentication]] again",
            ),
            WikiPageSection(
                id="b",
                title="B",
                content_md="more [[Authentication]]",
            ),
        ],
    )
    resolved, broken = await maintainer._persist_cross_links(page, target_lang="en")
    # First-occurrence wins for the title→slug mapping; the same title
    # repeated three times still becomes a single entry.
    assert resolved == {"Authentication": "topic-auth"}
    assert broken == []


# ---------------------------------------------------------------------------
# §4.10 — Integration: apply_update with [[Alice]] reference writes the
# Neo4j REFERENCES edge.
# ---------------------------------------------------------------------------


def _patch_settings(monkeypatch: pytest.MonkeyPatch, *, redesign_on: bool) -> None:
    fake = SimpleNamespace(
        wiki_drift_ab=False,
        wiki_drift_ab_rate_limit_seconds=60,
        wiki_llm_native_redesign=redesign_on,
    )
    monkeypatch.setattr("beever_atlas.infra.config.get_settings", lambda: fake)


async def test_apply_merge_redirects_falls_back_to_slug_when_target_missing() -> None:
    """Defensive: if an operator merged into a target that was later
    deleted out-of-band, ``_apply_merge_redirects`` MUST NOT crash —
    it falls back to using the target slug as the page_id key so the
    next apply_update creates a fresh page rather than dropping the
    fact route entirely."""
    from unittest.mock import AsyncMock as _AM

    page_store = AsyncMock()
    # The source page exists with merged_into pointing at "topic-tgt",
    # but list_pages returns ONLY the source — the target was deleted.
    src = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="topic:src",
        title="Source",
        slug="topic-src",
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
        merged_into="topic-tgt",
    )
    page_store.list_pages = _AM(return_value=[src])
    maintainer = WikiMaintainer(page_store=page_store)

    plan = {"topic:src": ["f1", "f2"]}
    redirected = await maintainer._apply_merge_redirects(plan, channel_id="C1", target_lang="en")
    # Source key was rewritten — the orphan target slug is used as the
    # new page_id key. Future apply_update on this key will first-touch
    # the page, which is the desired graceful-degradation behaviour.
    assert "topic:src" not in redirected
    assert redirected.get("topic-tgt") == ["f1", "f2"]


async def test_apply_update_writes_neo4j_reference_edge_for_resolved_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: redesign flag ON, page rewritten with ``[[Alice]]``
    in the body → page saved to Mongo → ``upsert_wiki_page_node`` and
    ``upsert_wiki_reference_edge`` both called on the graph store."""
    _patch_settings(monkeypatch, redesign_on=True)

    # Pre-existing entity page so the [[Alice]] reference resolves.
    alice_page = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="entity:alice",
        title="Alice",
        slug="entity-alice",
        kind="entity",
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
    )
    page_store = AsyncMock()
    # `get_page` for the rewritten page returns None (first-touch),
    # `list_pages` returns [Alice] for the cross-link resolver.
    page_store.get_page = AsyncMock(return_value=None)
    page_store.list_pages = AsyncMock(return_value=[alice_page])
    page_store.save_page = AsyncMock()

    graph_store = AsyncMock()
    graph_store.upsert_wiki_page_node = AsyncMock(return_value="node-1")
    graph_store.upsert_wiki_reference_edge = AsyncMock()

    maintainer = WikiMaintainer(page_store=page_store, graph_store=graph_store)

    async def _fake_load_facts(channel_id: str, ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "id": "f1",
                "memory_text": "the auth flow",
                "cluster_id": "auth",
                "entity_tags": [],
                "fact_type": "observation",
                "source_message_id": "m1",
            }
        ]

    maintainer._load_facts = _fake_load_facts  # type: ignore[method-assign]

    canned = {
        "affected_sections": [
            {
                "id": "overview",
                "title": "Overview",
                "content_md": "Auth uses OAuth2; [[Alice]] owns the implementation.",
            }
        ],
        "kind_schema": {
            "summary": "Auth uses OAuth2",
            "key_decisions": [],
            "key_people": ["Alice"],
            "key_dates": [],
            "open_questions": [],
        },
    }

    async def _stub_llm(prompt: str) -> str:
        return json.dumps(canned)

    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]

    applied = await maintainer.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    assert applied is True

    # Page node upserted exactly once.
    graph_store.upsert_wiki_page_node.assert_awaited_once()
    node_call = graph_store.upsert_wiki_page_node.await_args.kwargs
    assert node_call["channel_id"] == "C1"
    assert node_call["kind"] == "topic"

    # REFERENCES edge written for the resolved Alice link.
    graph_store.upsert_wiki_reference_edge.assert_awaited_once()
    edge_call = graph_store.upsert_wiki_reference_edge.await_args.kwargs
    assert edge_call["channel_id"] == "C1"
    assert edge_call["dst_slug"] == "entity-alice"
    # Source is the rewritten page's slug.
    assert edge_call["src_slug"]  # non-empty

    # And the cross_links / cross_links_broken were persisted on the page.
    saved: WikiPage = page_store.save_page.call_args.args[0]
    assert saved.cross_links == {"Alice": "entity-alice"}
    assert saved.cross_links_broken == []


async def test_apply_update_neo4j_failure_does_not_crash_apply_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Neo4j hiccup logs but apply_update still returns True — the
    page content already landed in Mongo before the graph upsert runs."""
    _patch_settings(monkeypatch, redesign_on=True)

    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=None)
    page_store.list_pages = AsyncMock(return_value=[])  # no resolvable targets
    page_store.save_page = AsyncMock()

    graph_store = AsyncMock()
    graph_store.upsert_wiki_page_node = AsyncMock(side_effect=RuntimeError("neo down"))
    graph_store.upsert_wiki_reference_edge = AsyncMock()

    maintainer = WikiMaintainer(page_store=page_store, graph_store=graph_store)

    async def _fake_load(channel_id: str, ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "id": "f1",
                "memory_text": "x",
                "cluster_id": "c",
                "entity_tags": [],
                "fact_type": "observation",
                "source_message_id": "m",
            }
        ]

    maintainer._load_facts = _fake_load  # type: ignore[method-assign]

    canned = {
        "affected_sections": [{"id": "overview", "title": "Overview", "content_md": "body"}],
        "kind_schema": {
            "summary": "ok",
            "key_decisions": [],
            "key_people": [],
            "key_dates": [],
            "open_questions": [],
        },
    }

    async def _stub_llm(prompt: str) -> str:
        return json.dumps(canned)

    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]

    applied = await maintainer.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    # Page still saved, apply_update returned True.
    assert applied is True
    page_store.save_page.assert_awaited_once()


async def test_apply_update_skips_neo4j_when_graph_store_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No graph store → no upsert calls (no exception either)."""
    _patch_settings(monkeypatch, redesign_on=True)

    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=None)
    page_store.list_pages = AsyncMock(return_value=[])
    page_store.save_page = AsyncMock()

    maintainer = WikiMaintainer(page_store=page_store, graph_store=None)

    async def _fake_load(channel_id: str, ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "id": "f1",
                "memory_text": "x",
                "cluster_id": "c",
                "entity_tags": [],
                "fact_type": "observation",
                "source_message_id": "m",
            }
        ]

    maintainer._load_facts = _fake_load  # type: ignore[method-assign]

    canned = {
        "affected_sections": [{"id": "overview", "title": "Overview", "content_md": "body"}],
        "kind_schema": {
            "summary": "ok",
            "key_decisions": [],
            "key_people": [],
            "key_dates": [],
            "open_questions": [],
        },
    }

    async def _stub_llm(prompt: str) -> str:
        return json.dumps(canned)

    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]

    applied = await maintainer.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    assert applied is True


async def test_apply_update_flag_off_does_not_resolve_or_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy flag-OFF path: cross-link resolution and Neo4j upsert
    are both skipped — byte-identical to pre-redesign behaviour."""
    _patch_settings(monkeypatch, redesign_on=False)

    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=None)
    page_store.list_pages = AsyncMock(return_value=[])
    page_store.save_page = AsyncMock()

    graph_store = AsyncMock()
    graph_store.upsert_wiki_page_node = AsyncMock()
    graph_store.upsert_wiki_reference_edge = AsyncMock()

    maintainer = WikiMaintainer(page_store=page_store, graph_store=graph_store)

    async def _fake_load(channel_id: str, ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "id": "f1",
                "memory_text": "x",
                "cluster_id": "c",
                "entity_tags": [],
                "fact_type": "observation",
                "source_message_id": "m",
            }
        ]

    maintainer._load_facts = _fake_load  # type: ignore[method-assign]

    legacy_payload = {
        "affected_sections": [{"id": "overview", "title": "Overview", "content_md": "legacy body"}],
    }

    async def _stub_llm(prompt: str) -> str:
        return json.dumps(legacy_payload)

    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]

    applied = await maintainer.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    assert applied is True
    # No Neo4j calls on the legacy path.
    graph_store.upsert_wiki_page_node.assert_not_awaited()
    graph_store.upsert_wiki_reference_edge.assert_not_awaited()
    # And list_pages was never called (cross-link resolution skipped).
    page_store.list_pages.assert_not_awaited()


# ---------------------------------------------------------------------------
# Smoke test for the convenience _resolve_wikilink wrapper
# ---------------------------------------------------------------------------


async def test_resolve_wikilink_method_resolves_via_page_store() -> None:
    pages = [_page_for_index(page_id="topic:auth", title="Authentication")]
    page_store = AsyncMock()
    page_store.list_pages = AsyncMock(return_value=pages)
    maintainer = WikiMaintainer(page_store=page_store)

    slug = await maintainer._resolve_wikilink("C1", "en", "Authentication")
    assert slug == "topic-auth"
    # And the misses are honest.
    miss = await maintainer._resolve_wikilink("C1", "en", "Nonexistent")
    assert miss is None


# Reference to keep ``wm_mod`` import usage explicit (for type-checking).
def test_module_has_expected_helpers() -> None:
    assert callable(wm_mod._parse_wikilinks)
    assert callable(wm_mod._resolve_wikilink_against_index)
    assert callable(wm_mod._build_page_index)
