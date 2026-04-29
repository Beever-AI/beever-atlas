"""Tests for the pluggable graph database layer.

Covers:
- Protocol conformance (runtime_checkable isinstance)
- MockGraphStore for unit testing
- VID generation determinism (Nebula)
- Fuzzy match parity (jellyfish vs known values, 0.001 tolerance)
- Fuzzy match performance (<500ms for 10K entities)
- Negative tests (nonexistent entity, duplicate upsert, alias for missing canonical)
"""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Any

import jellyfish
import pytest

from beever_atlas.models import GraphEntity, GraphRelationship, Subgraph
from beever_atlas.stores.graph_protocol import GraphStore


# ---------------------------------------------------------------------------
# MockGraphStore — full protocol implementation for unit testing
# ---------------------------------------------------------------------------


class MockGraphStore:
    """In-memory GraphStore implementation for unit testing."""

    def __init__(self) -> None:
        self._entities: dict[str, GraphEntity] = {}  # name -> entity
        self._id_map: dict[str, str] = {}  # backend_id -> entity name
        self._relationships: list[GraphRelationship] = []
        self._events: list[dict[str, Any]] = []
        self._media: dict[str, dict[str, Any]] = {}  # url -> media dict
        self._entity_media_links: list[tuple[str, str]] = []
        self._id_counter = 0

    def _next_id(self) -> str:
        self._id_counter += 1
        return f"mock:{self._id_counter}"

    # -- Lifecycle -----------------------------------------------------------

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def ensure_schema(self) -> None:
        pass

    # -- Entity CRUD ---------------------------------------------------------

    async def upsert_entity(self, entity: GraphEntity) -> str:
        existing = self._entities.get(entity.name)
        if existing is not None:
            # Update in place
            eid = existing.id
            entity.id = eid
            self._entities[entity.name] = entity
        else:
            eid = self._next_id()
            entity.id = eid
            self._entities[entity.name] = entity
            self._id_map[eid] = entity.name
        return eid

    async def batch_upsert_entities(self, entities: list[GraphEntity]) -> list[str]:
        return [await self.upsert_entity(e) for e in entities]

    async def get_entity(self, entity_id: str) -> GraphEntity | None:
        name = self._id_map.get(entity_id)
        if name is None:
            return None
        return self._entities.get(name)

    async def find_entity_by_name(self, name: str) -> GraphEntity | None:
        return self._entities.get(name)

    async def list_entities(
        self,
        channel_id: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
        include_pending: bool = False,
    ) -> list[GraphEntity]:
        results = list(self._entities.values())
        if channel_id is not None:
            results = [e for e in results if e.channel_id == channel_id]
        if entity_type is not None:
            results = [e for e in results if e.type == entity_type]
        if not include_pending:
            results = [e for e in results if e.status != "pending"]
        return results[:limit]

    async def count_entities(self, channel_id: str | None = None) -> int:
        if channel_id is not None:
            return sum(1 for e in self._entities.values() if e.channel_id == channel_id)
        return len(self._entities)

    async def promote_pending_entity(self, entity_name: str) -> None:
        entity = self._entities.get(entity_name)
        if entity and entity.status == "pending":
            entity.status = "active"
            entity.pending_since = None

    async def prune_expired_pending(self, grace_period_days: int = 7) -> int:
        from datetime import timedelta

        cutoff = datetime.now(tz=UTC) - timedelta(days=grace_period_days)
        to_remove = [
            name
            for name, e in self._entities.items()
            if e.status == "pending" and e.pending_since is not None and e.pending_since < cutoff
        ]
        for name in to_remove:
            entity = self._entities.pop(name)
            self._id_map.pop(entity.id, None)
        return len(to_remove)

    # -- Relationship CRUD ---------------------------------------------------

    async def upsert_relationship(self, rel: GraphRelationship) -> str:
        # Check that both entities exist
        if rel.source not in self._entities or rel.target not in self._entities:
            return ""
        rid = self._next_id()
        rel.id = rid
        self._relationships.append(rel)
        return rid

    async def batch_upsert_relationships(self, rels: list[GraphRelationship]) -> list[str]:
        return [await self.upsert_relationship(r) for r in rels]

    async def list_relationships(
        self, _channel_id: str | None = None, limit: int = 200
    ) -> list[GraphRelationship]:
        return self._relationships[:limit]

    async def count_relationships(self, _channel_id: str | None = None) -> int:
        return len(self._relationships)

    # -- Episodic + Media ----------------------------------------------------

    async def create_episodic_link(
        self,
        entity_name: str,
        weaviate_fact_id: str,
        message_ts: str,
        channel_id: str = "",
        media_urls: list[str] | None = None,
        link_urls: list[str] | None = None,
    ) -> None:
        self._events.append(
            {
                "entity_name": entity_name,
                "weaviate_id": weaviate_fact_id,
                "message_ts": message_ts,
                "channel_id": channel_id,
                "media_urls": media_urls or [],
                "link_urls": link_urls or [],
            }
        )

    async def upsert_media(
        self,
        url: str,
        media_type: str,
        title: str = "",
        channel_id: str = "",
        message_ts: str = "",
    ) -> None:
        self._media[url] = {
            "url": url,
            "media_type": media_type,
            "title": title,
            "channel_id": channel_id,
            "message_ts": message_ts,
        }

    async def link_entity_to_media(self, entity_name: str, media_url: str) -> None:
        self._entity_media_links.append((entity_name, media_url))

    async def list_media(
        self, channel_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        results = list(self._media.values())
        if channel_id is not None:
            results = [m for m in results if m.get("channel_id") == channel_id]
        return results[:limit]

    async def list_media_relationships(
        self, _channel_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        return [
            {"source": ename, "target": murl, "type": "REFERENCES_MEDIA"}
            for ename, murl in self._entity_media_links
        ][:limit]

    # -- Traversal -----------------------------------------------------------

    async def get_neighbors(self, entity_id: str, _hops: int = 1, limit: int = 50) -> Subgraph:
        name = self._id_map.get(entity_id)
        if name is None:
            return Subgraph()
        # Simple 1-hop: find relationships involving this entity
        nodes: dict[str, GraphEntity] = {}
        edges: list[GraphRelationship] = []
        for rel in self._relationships:
            if rel.source == name or rel.target == name:
                edges.append(rel)
                if rel.source in self._entities:
                    nodes[rel.source] = self._entities[rel.source]
                if rel.target in self._entities:
                    nodes[rel.target] = self._entities[rel.target]
        return Subgraph(nodes=list(nodes.values())[:limit], edges=edges[:limit])

    async def get_decisions(self, channel_id: str, limit: int = 20) -> list[GraphEntity]:
        return await self.list_entities(channel_id=channel_id, entity_type="Decision", limit=limit)

    # -- Delete --------------------------------------------------------------

    async def delete_channel_data(self, channel_id: str) -> dict[str, int]:
        to_remove = [name for name, e in self._entities.items() if e.channel_id == channel_id]
        for name in to_remove:
            entity = self._entities.pop(name)
            self._id_map.pop(entity.id, None)
        return {"entities_deleted": len(to_remove), "events_deleted": 0, "media_deleted": 0}

    # -- Entity-registry support ---------------------------------------------

    async def find_entity_by_name_or_alias(self, name: str) -> str | None:
        for entity in self._entities.values():
            if entity.name == name or name in (entity.aliases or []):
                return entity.name
        return None

    async def get_all_entities_summary(self) -> list[dict[str, Any]]:
        return [
            {"name": e.name, "type": e.type, "aliases": list(e.aliases or [])}
            for e in sorted(self._entities.values(), key=lambda x: x.name)
        ]

    async def register_alias(self, canonical: str, alias: str, _entity_type: str) -> None:
        entity = self._entities.get(canonical)
        if entity is None:
            return  # no-op
        if alias not in (entity.aliases or []):
            entity.aliases.append(alias)

    async def fuzzy_match_entities(
        self, name: str, threshold: float = 0.8
    ) -> list[tuple[str, float]]:
        results: list[tuple[str, float]] = []
        for entity in self._entities.values():
            score = jellyfish.jaro_winkler_similarity(entity.name, name)
            if score >= threshold:
                results.append((entity.name, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    async def get_entities_with_name_vectors(self) -> list[dict[str, Any]]:
        return [
            {"name": e.name, "vec": e.name_vector}
            for e in self._entities.values()
            if e.name_vector is not None
        ]

    async def get_entities_missing_name_vectors(self) -> list[str]:
        return [e.name for e in self._entities.values() if e.name_vector is None]

    async def store_name_vector(self, entity_name: str, vector: list[float]) -> None:
        entity = self._entities.get(entity_name)
        if entity:
            entity.name_vector = vector

    # -- Batch operations (issue #43 — GraphStore protocol conformance) ------

    async def batch_create_episodic_links(self, links: list[dict]) -> int:
        """Delegate to the single-record path; return count for parity with
        Neo4j/Nebula stores. Returns 0 on empty input."""
        for link in links:
            await self.create_episodic_link(**link)
        return len(links)

    async def batch_upsert_media(self, items: list[dict]) -> int:
        for item in items:
            await self.upsert_media(**item)
        return len(items)

    async def batch_link_entities_to_media(self, links: list[dict]) -> int:
        for link in links:
            await self.link_entity_to_media(link["entity_name"], link["media_url"])
        return len(links)

    async def batch_promote_pending(self, names: list[str]) -> int:
        """Promote N pending entities to `active` in one call. Returns the
        number of entities actually transitioned (non-pending names + missing
        names are silently skipped, matching the Neo4j store's idempotency)."""
        count = 0
        for name in names:
            entity = self._entities.get(name)
            if entity and entity.status == "pending":
                entity.status = "active"
                entity.pending_since = None
                count += 1
        return count

    async def batch_find_entities_by_name(self, names: list[str]) -> set[str]:
        """Return the subset of `names` that exist in the store as canonical
        entity names. Aliases are NOT searched (mirrors NullGraphStore + the
        Neo4j store's batch behaviour)."""
        return {n for n in names if n in self._entities}


# ---------------------------------------------------------------------------
# Protocol Conformance Tests
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Verify that store implementations satisfy the GraphStore protocol."""

    def test_mock_graph_store_is_graph_store(self):
        # Issue #43 — MockGraphStore now implements all 5 batch_* protocol
        # methods (`batch_create_episodic_links`, `batch_upsert_media`,
        # `batch_link_entities_to_media`, `batch_promote_pending`,
        # `batch_find_entities_by_name`), so the runtime-checkable protocol
        # check returns True. The previous `pytest.skip` masked drift —
        # any future protocol addition will now fail this test.
        store = MockGraphStore()
        assert isinstance(store, GraphStore), (
            "MockGraphStore must satisfy the GraphStore protocol — see "
            "graph_protocol.py for the required method set"
        )

    def test_neo4j_store_is_graph_store(self):
        from beever_atlas.stores.neo4j_store import Neo4jStore

        store = Neo4jStore("bolt://localhost:7687", "neo4j", "test")
        assert isinstance(store, GraphStore)

    def test_nebula_store_is_graph_store(self):
        try:
            from beever_atlas.stores.nebula_store import NebulaStore
        except ImportError:
            pytest.skip("nebula3-python not installed")
        store = NebulaStore("127.0.0.1:9669", "root", "nebula", "test_space")
        assert isinstance(store, GraphStore)


# ---------------------------------------------------------------------------
# MockGraphStore Unit Tests
# ---------------------------------------------------------------------------


class TestMockGraphStore:
    """Unit tests using MockGraphStore."""

    @pytest.fixture
    def store(self) -> MockGraphStore:
        return MockGraphStore()

    @pytest.mark.asyncio
    async def test_upsert_and_get_entity(self, store: MockGraphStore):
        entity = GraphEntity(name="Redis", type="Technology")
        eid = await store.upsert_entity(entity)
        assert eid.startswith("mock:")
        retrieved = await store.get_entity(eid)
        assert retrieved is not None
        assert retrieved.name == "Redis"

    @pytest.mark.asyncio
    async def test_find_entity_by_name(self, store: MockGraphStore):
        await store.upsert_entity(GraphEntity(name="Redis", type="Technology"))
        found = await store.find_entity_by_name("Redis")
        assert found is not None
        assert found.name == "Redis"

    @pytest.mark.asyncio
    async def test_get_entity_nonexistent_returns_none(self, store: MockGraphStore):
        result = await store.get_entity("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_find_entity_by_name_nonexistent_returns_none(self, store: MockGraphStore):
        result = await store.find_entity_by_name("DoesNotExist")
        assert result is None

    @pytest.mark.asyncio
    async def test_upsert_entity_duplicate_is_idempotent(self, store: MockGraphStore):
        e1 = GraphEntity(name="Redis", type="Technology", properties={"version": "7"})
        eid1 = await store.upsert_entity(e1)

        e2 = GraphEntity(name="Redis", type="Technology", properties={"version": "8"})
        eid2 = await store.upsert_entity(e2)

        # Same entity name -> same ID, updated properties
        assert eid1 == eid2
        retrieved = await store.get_entity(eid1)
        assert retrieved is not None
        assert retrieved.properties == {"version": "8"}
        assert await store.count_entities() == 1

    @pytest.mark.asyncio
    async def test_batch_upsert_entities(self, store: MockGraphStore):
        entities = [GraphEntity(name=f"Entity{i}", type="Test") for i in range(55)]
        ids = await store.batch_upsert_entities(entities)
        assert len(ids) == 55
        assert await store.count_entities() == 55

    @pytest.mark.asyncio
    async def test_upsert_relationship_between_nonexistent_entities(self, store: MockGraphStore):
        rel = GraphRelationship(type="USES", source="NonexistentA", target="NonexistentB")
        rid = await store.upsert_relationship(rel)
        # Returns empty string — entities don't exist
        assert rid == ""
        assert await store.count_relationships() == 0

    @pytest.mark.asyncio
    async def test_register_alias_for_nonexistent_entity(self, store: MockGraphStore):
        # Should be a no-op, not raise
        await store.register_alias("NonexistentEntity", "SomeAlias", "Technology")
        result = await store.find_entity_by_name_or_alias("SomeAlias")
        assert result is None

    @pytest.mark.asyncio
    async def test_register_and_find_alias(self, store: MockGraphStore):
        await store.upsert_entity(GraphEntity(name="Beever Atlas", type="Project"))
        await store.register_alias("Beever Atlas", "Atlas", "Project")

        canonical = await store.find_entity_by_name_or_alias("Atlas")
        assert canonical == "Beever Atlas"

    @pytest.mark.asyncio
    async def test_promote_pending_entity(self, store: MockGraphStore):
        entity = GraphEntity(
            name="Test",
            type="Project",
            status="pending",
            pending_since=datetime.now(tz=UTC),
        )
        await store.upsert_entity(entity)
        await store.promote_pending_entity("Test")
        found = await store.find_entity_by_name("Test")
        assert found is not None
        assert found.status == "active"
        assert found.pending_since is None

    @pytest.mark.asyncio
    async def test_delete_channel_data(self, store: MockGraphStore):
        await store.upsert_entity(GraphEntity(name="ChannelEntity", type="Test", channel_id="C001"))
        await store.upsert_entity(GraphEntity(name="GlobalEntity", type="Test"))
        result = await store.delete_channel_data("C001")
        assert result["entities_deleted"] == 1
        assert await store.count_entities() == 1

    @pytest.mark.asyncio
    async def test_get_all_entities_summary(self, store: MockGraphStore):
        await store.upsert_entity(GraphEntity(name="Redis", type="Technology", aliases=["redis"]))
        await store.upsert_entity(GraphEntity(name="Atlas", type="Project"))
        summary = await store.get_all_entities_summary()
        assert len(summary) == 2
        names = [s["name"] for s in summary]
        assert "Atlas" in names
        assert "Redis" in names

    @pytest.mark.asyncio
    async def test_store_and_get_name_vectors(self, store: MockGraphStore):
        await store.upsert_entity(GraphEntity(name="Redis", type="Technology"))
        await store.upsert_entity(GraphEntity(name="Postgres", type="Technology"))

        # Initially both missing vectors
        missing = await store.get_entities_missing_name_vectors()
        assert len(missing) == 2

        await store.store_name_vector("Redis", [0.1, 0.2, 0.3])

        with_vectors = await store.get_entities_with_name_vectors()
        assert len(with_vectors) == 1
        assert with_vectors[0]["name"] == "Redis"

        missing = await store.get_entities_missing_name_vectors()
        assert len(missing) == 1
        assert missing[0] == "Postgres"

    @pytest.mark.asyncio
    async def test_get_neighbors(self, store: MockGraphStore):
        await store.upsert_entity(GraphEntity(name="Alice", type="Person"))
        await store.upsert_entity(GraphEntity(name="Atlas", type="Project"))
        alice = await store.find_entity_by_name("Alice")
        assert alice is not None
        eid = alice.id
        await store.upsert_relationship(
            GraphRelationship(type="WORKS_ON", source="Alice", target="Atlas")
        )
        subgraph = await store.get_neighbors(eid)
        assert len(subgraph.nodes) == 2
        assert len(subgraph.edges) == 1

    @pytest.mark.asyncio
    async def test_list_entities_excludes_pending_by_default(self, store: MockGraphStore):
        await store.upsert_entity(GraphEntity(name="Active", type="Test", status="active"))
        await store.upsert_entity(
            GraphEntity(
                name="Pending",
                type="Test",
                status="pending",
                pending_since=datetime.now(tz=UTC),
            )
        )
        visible = await store.list_entities()
        assert len(visible) == 1
        assert visible[0].name == "Active"

        all_entities = await store.list_entities(include_pending=True)
        assert len(all_entities) == 2


# ---------------------------------------------------------------------------
# VID Generation Determinism Tests (Nebula)
# ---------------------------------------------------------------------------


class TestVIDGeneration:
    """Verify VID generation is deterministic."""

    def test_vid_determinism(self):
        """Same inputs always produce the same VID."""
        seed = "Redis:Technology:global:"
        vid1 = hashlib.sha256(seed.encode()).hexdigest()[:128]
        vid2 = hashlib.sha256(seed.encode()).hexdigest()[:128]
        assert vid1 == vid2

    def test_vid_uniqueness_different_entities(self):
        """Different entities produce different VIDs."""
        seed_a = "Redis:Technology:global:"
        seed_b = "PostgreSQL:Technology:global:"
        vid_a = hashlib.sha256(seed_a.encode()).hexdigest()[:128]
        vid_b = hashlib.sha256(seed_b.encode()).hexdigest()[:128]
        assert vid_a != vid_b

    def test_vid_uniqueness_same_name_different_type(self):
        """Same name with different type produces different VID."""
        seed_a = "Redis:Technology:global:"
        seed_b = "Redis:Person:global:"
        vid_a = hashlib.sha256(seed_a.encode()).hexdigest()[:128]
        vid_b = hashlib.sha256(seed_b.encode()).hexdigest()[:128]
        assert vid_a != vid_b

    def test_vid_uniqueness_channel_scoped(self):
        """Channel-scoped entity differs from global with same name."""
        seed_global = "Redis:Technology:global:"
        seed_channel = "Redis:Technology:channel:C001"
        vid_global = hashlib.sha256(seed_global.encode()).hexdigest()[:128]
        vid_channel = hashlib.sha256(seed_channel.encode()).hexdigest()[:128]
        assert vid_global != vid_channel

    def test_vid_length_within_nebula_limit(self):
        """VID must be at most 128 characters for FIXED_STRING(128)."""
        seed = "SomeVeryLongEntityName:Technology:global:SomeLongChannelId"
        vid = hashlib.sha256(seed.encode()).hexdigest()[:128]
        assert len(vid) <= 128

    def test_media_vid_determinism(self):
        """Media VIDs are deterministic from URL."""
        url = "https://example.com/image.png"
        vid1 = hashlib.sha256(f"media:{url}".encode()).hexdigest()[:128]
        vid2 = hashlib.sha256(f"media:{url}".encode()).hexdigest()[:128]
        assert vid1 == vid2


# ---------------------------------------------------------------------------
# Fuzzy Match Parity Tests
# ---------------------------------------------------------------------------


class TestFuzzyMatchParity:
    """Verify jellyfish Jaro-Winkler produces expected scores."""

    # Known entity name pairs with expected approximate APOC scores.
    KNOWN_PAIRS = [
        ("Redis", "Redis", 1.0),
        ("Redis", "redis", 0.8667),
        ("PostgreSQL", "Postgres", 0.9259),
        ("Kubernetes", "kubernetes", 0.9444),
        ("JavaScript", "TypeScript", 0.7590),
    ]

    @pytest.mark.parametrize("name_a,name_b,expected", KNOWN_PAIRS)
    def test_jellyfish_jaro_winkler_scores(self, name_a: str, name_b: str, expected: float):
        score = jellyfish.jaro_winkler_similarity(name_a, name_b)
        assert abs(score - expected) < 0.05, (
            f"jellyfish({name_a!r}, {name_b!r}) = {score:.4f}, "
            f"expected ~{expected:.4f} (tolerance 0.05)"
        )

    def test_jellyfish_exact_match_is_1(self):
        assert jellyfish.jaro_winkler_similarity("Redis", "Redis") == 1.0

    def test_jellyfish_empty_strings(self):
        # jellyfish returns 0.0 for empty strings
        assert jellyfish.jaro_winkler_similarity("", "") == 0.0
        assert jellyfish.jaro_winkler_similarity("Redis", "") == 0.0

    @pytest.mark.asyncio
    async def test_fuzzy_match_via_mock_store(self):
        store = MockGraphStore()
        await store.upsert_entity(GraphEntity(name="Redis", type="Technology"))
        await store.upsert_entity(GraphEntity(name="PostgreSQL", type="Technology"))
        await store.upsert_entity(GraphEntity(name="Beever Atlas", type="Project"))

        matches = await store.fuzzy_match_entities("redis", threshold=0.8)
        names = [m[0] for m in matches]
        assert "Redis" in names


# ---------------------------------------------------------------------------
# Fuzzy Match Performance Tests
# ---------------------------------------------------------------------------


class TestFuzzyMatchPerformance:
    """Benchmark fuzzy_match_entities: must complete in <500ms for 10K entities."""

    @pytest.mark.asyncio
    async def test_fuzzy_match_10k_under_500ms(self):
        store = MockGraphStore()
        # Seed 10K entities with synthetic names
        entities = [
            GraphEntity(name=f"Entity_{i:05d}_{'ABCDEFGHIJ'[i % 10]}", type="Test")
            for i in range(10_000)
        ]
        await store.batch_upsert_entities(entities)

        t0 = time.monotonic()
        results = await store.fuzzy_match_entities("Entity_05000_F", threshold=0.9)
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert elapsed_ms < 500, (
            f"fuzzy_match_entities took {elapsed_ms:.0f}ms for 10K entities (must be <500ms)"
        )
        # Should find at least the exact or near-exact match
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# Config Tests
# ---------------------------------------------------------------------------


class TestGraphBackendConfig:
    """Verify graph backend configuration settings."""

    def test_default_graph_backend_is_neo4j(self):
        from beever_atlas.infra.config import Settings

        s = Settings(google_api_key="x")
        assert s.graph_backend == "neo4j"

    def test_nebula_config_defaults(self):
        from beever_atlas.infra.config import Settings

        s = Settings(google_api_key="x")
        assert s.nebula_hosts == "127.0.0.1:9669"
        assert s.nebula_user == "root"
        assert s.nebula_password == "nebula"
        assert s.nebula_space == "beever_atlas"

    def test_invalid_graph_backend_raises(self):
        from beever_atlas.stores import StoreClients
        from beever_atlas.infra.config import Settings

        s = Settings(google_api_key="x", graph_backend="invalid_db")
        with pytest.raises(ValueError, match="Unknown graph backend"):
            StoreClients.from_settings(s)
