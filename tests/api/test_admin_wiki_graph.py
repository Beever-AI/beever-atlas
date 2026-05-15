"""Tests for the wiki-graph endpoint + Neo4j formatter (§6.3–§6.5).

Covers:
  - §6.3: empty channel returns empty arrays with HTTP 200
  - §6.4: 5 pages + 7 cross-links → 5 wiki nodes + 7 references_wiki edges
  - §6.5: entity cross-edges have data.kind="references_entity"
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock


# ---------------------------------------------------------------------------
# Neo4j fake — captures driver session + query/record shape
# ---------------------------------------------------------------------------


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data.get(key)


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = [_FakeRecord(r) for r in records]

    def __aiter__(self):
        return self

    async def __anext__(self) -> _FakeRecord:
        if not self._records:
            raise StopAsyncIteration
        return self._records.pop(0)


class _FakeSession:
    """Records every query and returns canned results matched by substring."""

    def __init__(self, page_records, edge_records, entity_records) -> None:
        self._page_records = page_records
        self._edge_records = edge_records
        self._entity_records = entity_records
        self.queries: list[str] = []

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args, **kwargs) -> None:
        return None

    async def run(self, query: str, **params) -> _FakeResult:
        self.queries.append(query)
        # Dispatch by query content — order matters: REFERENCES before
        # Entity because the entity query does not contain REFERENCES
        # but does contain WikiPage; the references query contains both
        # WikiPage AND REFERENCES.
        if "REFERENCES" in query:
            return _FakeResult(self._edge_records)
        if "Entity" in query:
            return _FakeResult(self._entity_records)
        # Default: the WikiPage-node query.
        return _FakeResult(self._page_records)


class _FakeDriver:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def session(self) -> _FakeSession:
        return self._session


def _make_neo4j_store(session: _FakeSession):
    from beever_atlas.stores.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)
    store._driver = _FakeDriver(session)  # type: ignore[attr-defined]
    return store


# ---------------------------------------------------------------------------
# §6.3 — empty channel
# ---------------------------------------------------------------------------


async def test_get_wiki_graph_empty_channel_returns_empty_arrays() -> None:
    session = _FakeSession(page_records=[], edge_records=[], entity_records=[])
    store = _make_neo4j_store(session)
    payload = await store.get_wiki_graph("C1")
    assert payload["channel_id"] == "C1"
    assert payload["nodes"] == []
    assert payload["edges"] == []


# ---------------------------------------------------------------------------
# §6.4 — 5 pages + 7 cross-links
# ---------------------------------------------------------------------------


async def test_get_wiki_graph_returns_pages_and_references_edges() -> None:
    pages = [
        {
            "slug": f"topic-{i}",
            "title": f"Topic {i}",
            "kind": "topic",
            "version": i,
            "last_updated": "2026-05-01T00:00:00Z",
        }
        for i in range(1, 6)
    ]
    # 7 references_wiki edges spanning the five nodes (a tiny DAG).
    raw_edges = [
        ("topic-1", "topic-2"),
        ("topic-1", "topic-3"),
        ("topic-2", "topic-4"),
        ("topic-2", "topic-5"),
        ("topic-3", "topic-4"),
        ("topic-4", "topic-5"),
        ("topic-5", "topic-1"),
    ]
    edges = [{"src_slug": s, "dst_slug": d} for s, d in raw_edges]
    session = _FakeSession(page_records=pages, edge_records=edges, entity_records=[])
    store = _make_neo4j_store(session)

    payload = await store.get_wiki_graph("C1")
    assert len(payload["nodes"]) == 5
    assert {n["data"]["id"] for n in payload["nodes"]} == {
        "topic-1",
        "topic-2",
        "topic-3",
        "topic-4",
        "topic-5",
    }
    # All wiki nodes carry kind="wiki" and the page_kind from Neo4j.
    for node in payload["nodes"]:
        assert node["data"]["kind"] == "wiki"
        assert node["data"]["page_kind"] == "topic"

    assert len(payload["edges"]) == 7
    edge_pairs = {(e["data"]["source"], e["data"]["target"]) for e in payload["edges"]}
    assert edge_pairs == set(raw_edges)
    # All references_wiki edges carry the right kind tag.
    for edge in payload["edges"]:
        assert edge["data"]["kind"] == "references_wiki"
        assert edge["data"]["id"].startswith("e:")


async def test_get_wiki_graph_dedupes_repeated_edges() -> None:
    """Defensive — Neo4j MERGE prevents duplicate edges, but the
    formatter must not double-emit the same pair if the underlying
    query ever returns it twice."""
    pages = [{"slug": "a", "title": "A", "kind": "topic", "version": 1, "last_updated": ""}]
    edges = [
        {"src_slug": "a", "dst_slug": "a"},
        {"src_slug": "a", "dst_slug": "a"},
    ]
    session = _FakeSession(page_records=pages, edge_records=edges, entity_records=[])
    store = _make_neo4j_store(session)
    payload = await store.get_wiki_graph("C1")
    assert len(payload["edges"]) == 1


# ---------------------------------------------------------------------------
# §6.5 — entity cross-edges carry data.kind="references_entity"
# ---------------------------------------------------------------------------


async def test_entity_cross_edges_emit_references_entity_kind() -> None:
    pages = [
        {
            "slug": "topic-auth",
            "title": "Authentication",
            "kind": "topic",
            "version": 3,
            "last_updated": "2026-05-01",
        }
    ]
    entity_rows = [
        {
            "src_slug": "topic-auth",
            "entity_name": "Alice",
            "entity_type": "Person",
        }
    ]
    session = _FakeSession(page_records=pages, edge_records=[], entity_records=entity_rows)
    store = _make_neo4j_store(session)
    payload = await store.get_wiki_graph("C1")

    # 1 wiki node + 1 entity node = 2.
    assert len(payload["nodes"]) == 2
    entity_nodes = [n for n in payload["nodes"] if n["data"]["kind"] == "entity"]
    assert len(entity_nodes) == 1
    assert entity_nodes[0]["data"]["id"] == "entity:Alice"
    assert entity_nodes[0]["data"]["entity_type"] == "Person"

    # 1 references_entity edge.
    assert len(payload["edges"]) == 1
    edge = payload["edges"][0]
    assert edge["data"]["kind"] == "references_entity"
    assert edge["data"]["source"] == "topic-auth"
    assert edge["data"]["target"] == "entity:Alice"


# ---------------------------------------------------------------------------
# Endpoint behaviour — graceful degradation
# ---------------------------------------------------------------------------


def _fake_page(*, slug, title, kind="topic", cross_links=None, page_id=None):
    """Build a minimal WikiPage fake with the fields the graph endpoint reads."""
    from datetime import UTC, datetime as _dt
    from types import SimpleNamespace as _SN

    return _SN(
        slug=slug,
        title=title,
        kind=kind,
        version=1,
        page_id=page_id or f"topic:{slug}",
        cross_links=cross_links or {},
        updated_at=_dt(2026, 5, 1, tzinfo=UTC),
        # _page_has_renderable_content checks content/narrative_sections/modules;
        # supply a non-empty string so the page is not filtered out of the graph.
        content="stub",
        parent_id=None,
    )


def _patch_endpoint_deps(*, pages, graph_backend):
    """Common mock setup: WikiPageStore returns ``pages``; ``stores.graph``
    is the supplied backend; assert_channel_access is a no-op; settings
    + cache _load_page_store path is short-circuited."""
    from unittest.mock import patch

    async def _ok(*args, **kwargs):
        return None

    fake_store = AsyncMock()
    fake_store.list_pages_by_kind = AsyncMock(return_value=list(pages))
    fake_stores = type("S", (), {"graph": graph_backend})()

    return patch.multiple(
        "beever_atlas.api.wiki",
        _load_page_store=AsyncMock(return_value=fake_store),
        get_stores=lambda: fake_stores,
        _resolve_target_lang=AsyncMock(return_value="en"),
    ), patch(
        "beever_atlas.infra.channel_access.assert_channel_access",
        new=AsyncMock(side_effect=_ok),
    )


async def test_endpoint_returns_empty_when_no_pages_and_no_neo4j_parity() -> None:
    """Empty Mongo + bare graph backend → empty payload."""
    from beever_atlas.api.wiki import get_wiki_graph

    class _BareGraph:
        pass

    principal = type("P", (), {"id": "u-1"})()
    deps_patch, auth_patch = _patch_endpoint_deps(pages=[], graph_backend=_BareGraph())
    with deps_patch, auth_patch:
        result = await get_wiki_graph("C1", target_lang="en", principal=principal)
    assert result == {"channel_id": "C1", "nodes": [], "edges": []}


async def test_endpoint_swallows_neo4j_errors_keeping_wiki_pages() -> None:
    """A live Neo4j hiccup must not 500 the route — wiki page nodes
    + their cross_links edges still come back from Mongo."""
    from beever_atlas.api.wiki import get_wiki_graph

    class _BoomGraph:
        async def get_wiki_graph(self, channel_id):
            raise RuntimeError("neo down")

    pages = [
        _fake_page(slug="topic-auth", title="Authentication"),
        _fake_page(
            slug="topic-sessions",
            title="Sessions",
            cross_links={"Authentication": "topic-auth"},
        ),
    ]
    principal = type("P", (), {"id": "u-1"})()
    deps_patch, auth_patch = _patch_endpoint_deps(pages=pages, graph_backend=_BoomGraph())
    with deps_patch, auth_patch:
        result = await get_wiki_graph("C1", target_lang="en", principal=principal)
    # Both wiki pages survived the Neo4j failure (the channel hub is
    # added below the wiki nodes, expected on every non-empty graph).
    wiki_ids = {n["data"]["id"] for n in result["nodes"] if n["data"]["kind"] == "wiki"}
    assert wiki_ids == {"topic-auth", "topic-sessions"}
    # And the references_wiki edge from sessions -> auth landed.
    edges = [e for e in result["edges"] if e["data"]["kind"] == "references_wiki"]
    assert len(edges) == 1
    assert edges[0]["data"]["source"] == "topic-sessions"
    assert edges[0]["data"]["target"] == "topic-auth"


async def test_endpoint_builds_wiki_nodes_and_edges_from_mongo() -> None:
    """Source-of-truth check — the endpoint reads wiki_pages from Mongo
    and produces a node per page + a references_wiki edge per
    cross_links entry. Works on legacy installs where Neo4j has no
    WikiPage nodes yet."""
    from beever_atlas.api.wiki import get_wiki_graph

    class _BareGraph:
        pass

    pages = [
        _fake_page(slug="topic-auth", title="Authentication"),
        _fake_page(
            slug="topic-sessions",
            title="Sessions",
            cross_links={"Authentication": "topic-auth"},
        ),
        _fake_page(
            slug="entity-alice",
            title="Alice",
            kind="entity",
            cross_links={
                "Authentication": "topic-auth",
                "Sessions": "topic-sessions",
            },
        ),
    ]
    principal = type("P", (), {"id": "u-1"})()
    deps_patch, auth_patch = _patch_endpoint_deps(pages=pages, graph_backend=_BareGraph())
    with deps_patch, auth_patch:
        result = await get_wiki_graph("C1", target_lang="en", principal=principal)

    # 3 wiki nodes + 1 channel hub on every non-empty graph.
    wiki_nodes = [n for n in result["nodes"] if n["data"]["kind"] == "wiki"]
    assert len(wiki_nodes) == 3
    edges = [e for e in result["edges"] if e["data"]["kind"] == "references_wiki"]
    assert len(edges) == 3
    pairs = {(e["data"]["source"], e["data"]["target"]) for e in edges}
    assert pairs == {
        ("topic-sessions", "topic-auth"),
        ("entity-alice", "topic-auth"),
        ("entity-alice", "topic-sessions"),
    }


async def test_endpoint_drops_dangling_cross_link_edges() -> None:
    """A cross_links entry pointing at a non-existent slug must NOT
    emit a phantom edge — the renderer would attach the source to a
    target node that doesn't exist."""
    from beever_atlas.api.wiki import get_wiki_graph

    class _BareGraph:
        pass

    pages = [
        _fake_page(
            slug="topic-auth",
            title="Authentication",
            cross_links={"Logging Strategy": "topic-logging"},  # dangling
        ),
    ]
    principal = type("P", (), {"id": "u-1"})()
    deps_patch, auth_patch = _patch_endpoint_deps(pages=pages, graph_backend=_BareGraph())
    with deps_patch, auth_patch:
        result = await get_wiki_graph("C1", target_lang="en", principal=principal)
    wiki_nodes = [n for n in result["nodes"] if n["data"]["kind"] == "wiki"]
    assert len(wiki_nodes) == 1
    # No references_wiki edge — the dangling cross_link target was
    # dropped. The channel hub still gets a belongs_to edge from the
    # orphan wiki node, but no references_wiki edge.
    references_wiki = [e for e in result["edges"] if e["data"]["kind"] == "references_wiki"]
    assert references_wiki == []


async def test_endpoint_enriches_with_entity_edges_from_neo4j() -> None:
    """When Neo4j has ``references_entity`` edges for visible wiki
    nodes, the endpoint pulls them in and adds the entity nodes."""
    from beever_atlas.api.wiki import get_wiki_graph

    class _GraphWithEntityEdges:
        async def get_wiki_graph(self, channel_id):
            return {
                "channel_id": channel_id,
                "nodes": [
                    {
                        "data": {
                            "id": "entity:Bob",
                            "label": "Bob",
                            "kind": "entity",
                            "entity_type": "Person",
                        }
                    }
                ],
                "edges": [
                    {
                        "data": {
                            "id": "e:topic-auth->entity:Bob",
                            "source": "topic-auth",
                            "target": "entity:Bob",
                            "kind": "references_entity",
                        }
                    }
                ],
            }

    pages = [_fake_page(slug="topic-auth", title="Authentication")]
    principal = type("P", (), {"id": "u-1"})()
    deps_patch, auth_patch = _patch_endpoint_deps(
        pages=pages, graph_backend=_GraphWithEntityEdges()
    )
    with deps_patch, auth_patch:
        result = await get_wiki_graph("C1", target_lang="en", principal=principal)

    # Entity node + entity edge both present alongside the wiki page
    # and the channel hub.
    visible_ids = {n["data"]["id"] for n in result["nodes"]}
    assert "topic-auth" in visible_ids
    assert "entity:Bob" in visible_ids
    entity_edges = [e for e in result["edges"] if e["data"]["kind"] == "references_entity"]
    assert len(entity_edges) == 1
    assert entity_edges[0]["data"]["source"] == "topic-auth"
    assert entity_edges[0]["data"]["target"] == "entity:Bob"
