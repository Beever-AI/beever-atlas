"""Tests for the WikiPage → Entity bridge edges.

The writer side lives in :meth:`WikiMaintainer._upsert_wiki_graph` and
emits a ``:REFERENCES_ENTITY`` edge for every wikilink in the page body
that resolves against the entity registry. These tests drive
``_upsert_wiki_graph`` directly with a fake graph store and assert
the produced calls.

Acceptance:
  1. ``[[Redis]]`` + ``[[Alice]]`` → both edges land when those entities
     exist in the registry.
  2. ``[[Ghost]]`` (no entity) → no edge, no stub.
  3. Re-running on the same page does NOT multiply edges (idempotency
     comes from the underlying Cypher MERGE on the edge).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.models.persistence import WikiPage, WikiPageSection
from beever_atlas.services.wiki_maintainer import WikiMaintainer


# ── fake graph store ────────────────────────────────────────────────────────


def _make_fake_graph_store(known_entity_names: set[str] | None = None) -> MagicMock:
    """Build a fake graph store with the methods ``_upsert_wiki_graph`` calls.

    The fake records every ``upsert_wiki_reference_entity_edge`` invocation
    so the test can count edges + assert idempotency.
    """
    known = known_entity_names or set()

    store = MagicMock()
    store.entity_edge_calls = []  # type: ignore[attr-defined]

    async def fake_upsert_wiki_page_node(**kwargs):
        return "wid-test"

    async def fake_upsert_wiki_reference_edge(**kwargs):
        return None

    async def fake_find_entity_by_name_or_alias(name):
        return name if name in known else None

    async def fake_upsert_wiki_reference_entity_edge(**kwargs):
        store.entity_edge_calls.append(dict(kwargs))  # type: ignore[attr-defined]
        return None

    store.upsert_wiki_page_node = AsyncMock(side_effect=fake_upsert_wiki_page_node)
    store.upsert_wiki_reference_edge = AsyncMock(side_effect=fake_upsert_wiki_reference_edge)
    store.find_entity_by_name_or_alias = AsyncMock(side_effect=fake_find_entity_by_name_or_alias)
    store.upsert_wiki_reference_entity_edge = AsyncMock(
        side_effect=fake_upsert_wiki_reference_entity_edge
    )
    return store


def _make_page(content_md: str) -> WikiPage:
    return WikiPage(
        channel_id="C-test",
        target_lang="en",
        page_id="topic:auth",
        title="Auth",
        slug="topic-auth",
        kind="topic",
        version=1,
        sections=[
            WikiPageSection(
                id="overview",
                title="Overview",
                content_md=content_md,
            )
        ],
    )


# ── 1. Both wikilinks resolve → both entity edges land ─────────────────────


@pytest.mark.asyncio
async def test_resolved_wikilinks_produce_entity_edges():
    graph_store = _make_fake_graph_store(known_entity_names={"Redis", "Alice"})
    maintainer = WikiMaintainer(page_store=AsyncMock(), graph_store=graph_store)
    page = _make_page("Discussion of [[Redis]] adoption by [[Alice]] this week.")

    page.updated_at = datetime.now(tz=UTC)
    await maintainer._upsert_wiki_graph(page, resolved_slugs=[], target_lang="en")

    # Both entities produced an edge.
    edge_targets = {c["entity_name"] for c in graph_store.entity_edge_calls}
    assert edge_targets == {"Redis", "Alice"}, (
        f"expected edges for Redis and Alice; got {edge_targets}"
    )
    # Each call carries channel/lang/src_slug consistently.
    for call in graph_store.entity_edge_calls:
        assert call["channel_id"] == "C-test"
        assert call["target_lang"] == "en"
        assert call["src_slug"] == "topic-auth"


# ── 2. Wikilink to non-existent entity → no edge, no stub ──────────────────


@pytest.mark.asyncio
async def test_unresolved_wikilink_does_not_create_edge():
    graph_store = _make_fake_graph_store(known_entity_names=set())
    maintainer = WikiMaintainer(page_store=AsyncMock(), graph_store=graph_store)
    page = _make_page("Mention of [[Ghost]] (no entity behind it).")

    page.updated_at = datetime.now(tz=UTC)
    await maintainer._upsert_wiki_graph(page, resolved_slugs=[], target_lang="en")

    # No edge produced; the maintainer doesn't manufacture stubs for
    # bare wikilinks.
    assert graph_store.entity_edge_calls == []


# ── 3. Rebuilding the same page does not multiply edges ────────────────────


@pytest.mark.asyncio
async def test_idempotent_rebuild_does_not_multiply_edges():
    """Calling ``_upsert_wiki_graph`` twice on the same page produces
    the same edge writes twice — but each call is a Cypher MERGE on the
    edge, so the graph itself does not gain duplicate edges. We assert
    on the call shape (kwargs equal across the two invocations) which
    is the contract the underlying Cypher MERGE relies on.
    """
    graph_store = _make_fake_graph_store(known_entity_names={"Redis"})
    maintainer = WikiMaintainer(page_store=AsyncMock(), graph_store=graph_store)
    page = _make_page("[[Redis]] adoption.")
    page.updated_at = datetime.now(tz=UTC)

    await maintainer._upsert_wiki_graph(page, resolved_slugs=[], target_lang="en")
    first_calls = list(graph_store.entity_edge_calls)
    # Second run on the same page.
    await maintainer._upsert_wiki_graph(page, resolved_slugs=[], target_lang="en")
    second_calls = list(graph_store.entity_edge_calls)

    # First run produced exactly one edge.
    assert len(first_calls) == 1
    # Second run added one more (because we record every CALL, not every
    # delta) — but the kwargs are identical, which is what makes the
    # underlying Cypher MERGE idempotent.
    assert len(second_calls) == 2
    assert first_calls[0] == second_calls[1]
