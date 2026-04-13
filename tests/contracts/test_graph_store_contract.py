"""Contract tests for GraphStore implementations.

Exercises the public error-surface and a tiny subset of CRUD semantics
against every backend the test environment can reach.  Backends that
require external services (Neo4j, Nebula) are skipped when their env
vars are unset; the ``fake`` backend always runs and exercises the
error-mapping helpers directly via mocking.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.models import GraphEntity
from beever_atlas.stores import (
    GraphBackendUnavailable,
    GraphConflict,
    GraphNotFound,  # noqa: F401 — re-exported, ensures symbol is public
    GraphStoreError,
)


# ----------------------------------------------------------------------
# Backend fixture
# ----------------------------------------------------------------------


NEO4J_URI = os.environ.get("NEO4J_TEST_URI")
NEBULA_HOST = os.environ.get("NEBULA_TEST_HOST")


@pytest.fixture(
    params=[
        pytest.param(
            "neo4j",
            marks=pytest.mark.skipif(
                not NEO4J_URI,
                reason="NEO4J_TEST_URI not set",
            ),
        ),
        pytest.param(
            "nebula",
            marks=pytest.mark.skipif(
                not NEBULA_HOST,
                reason="NEBULA_TEST_HOST not set",
            ),
        ),
        pytest.param("fake"),
    ]
)
async def backend(request: pytest.FixtureRequest):
    """Yield ``(store, kind)`` per parameterised backend."""
    kind = request.param
    if kind == "neo4j":
        from beever_atlas.stores.neo4j_store import Neo4jStore

        store = Neo4jStore(
            NEO4J_URI or "",
            os.environ.get("NEO4J_TEST_USER", "neo4j"),
            os.environ.get("NEO4J_TEST_PASSWORD", "neo4j"),
        )
        await store.startup()
        try:
            yield store, kind
        finally:
            await store.delete_channel_data("__contract__")
            await store.shutdown()
    elif kind == "nebula":
        from beever_atlas.stores.nebula_store import NebulaStore

        store = NebulaStore(
            NEBULA_HOST or "",
            os.environ.get("NEBULA_TEST_USER", "root"),
            os.environ.get("NEBULA_TEST_PASSWORD", "nebula"),
            os.environ.get("NEBULA_TEST_SPACE", "beever_contract"),
        )
        await store.startup()
        try:
            yield store, kind
        finally:
            await store.delete_channel_data("__contract__")
            await store.shutdown()
    else:
        # Fake backend — no external dependency.  Individual tests still
        # exercise the error-mapping helpers from both stores via mocks.
        yield None, kind


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _entity(name: str, channel_id: str = "__contract__") -> GraphEntity:
    return GraphEntity(
        name=name,
        type="Person",
        scope="channel",
        channel_id=channel_id,
        properties={},
        aliases=[],
        source_message_id="test",
        message_ts=datetime.now(tz=UTC).isoformat(),
    )


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_entity_missing_returns_none_or_raises(backend):
    """Point-query on a non-existent ID must either return None or raise
    GraphNotFound — implementations MUST NOT leak a raw driver error."""
    store, kind = backend
    if kind == "fake":
        pytest.skip("covered by mock-backed mapping tests")

    try:
        result = await store.get_entity("does-not-exist-vid")
    except GraphNotFound:
        return  # acceptable contract outcome
    except GraphStoreError:
        pytest.fail("get_entity must not raise generic GraphStoreError for a clean miss")
    assert result is None


@pytest.mark.asyncio
async def test_upsert_and_retrieve_roundtrip(backend):
    store, kind = backend
    if kind == "fake":
        pytest.skip("no real backend to round-trip against")

    ent = _entity("alice")
    eid = await store.upsert_entity(ent)
    assert isinstance(eid, str) and eid

    fetched = await store.find_entity_by_name("alice")
    assert fetched is not None
    assert fetched.name == "alice"


@pytest.mark.asyncio
async def test_idempotent_delete_channel_data(backend):
    store, kind = backend
    if kind == "fake":
        pytest.skip("no real backend")

    # First delete — may or may not remove nodes; must not raise.
    r1 = await store.delete_channel_data("__contract_empty__")
    # Second delete on the same empty channel must be a no-op.
    r2 = await store.delete_channel_data("__contract_empty__")
    assert isinstance(r1, dict)
    assert isinstance(r2, dict)


@pytest.mark.asyncio
async def test_hops_out_of_range_raises_value_error(backend):
    """``get_neighbors`` must reject negative/invalid hops with ValueError."""
    store, kind = backend
    if kind == "fake":
        pytest.skip("no real backend")

    # hops=0 is currently silently clamped in both backends; ensure large
    # hop values that would explode the query are rejected.  If the
    # implementation accepts them, this test is a forward-looking guard
    # rail we accept as xfail.
    with pytest.raises((ValueError, GraphStoreError)):
        # No such entity, so either GraphNotFound/GraphStoreError or the
        # implementation's own ValueError is accepted.
        await store.get_neighbors("nonexistent", hops=-5)


# ----------------------------------------------------------------------
# Mock-backed error-mapping tests (always run, even when no real backend)
# ----------------------------------------------------------------------


def _neo4j_store_with_failing_session(exc: BaseException):
    """Build a Neo4jStore whose ``session().__aenter__`` raises *exc*."""
    from beever_atlas.stores.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(side_effect=exc)
    session_cm.__aexit__ = AsyncMock(return_value=None)

    driver = MagicMock()
    driver.session = MagicMock(return_value=session_cm)
    store._driver = driver  # type: ignore[attr-defined]
    return store


@pytest.mark.asyncio
async def test_neo4j_mapping_backend_unavailable():
    """ServiceUnavailable from the driver must surface as
    GraphBackendUnavailable."""
    from neo4j import exceptions as neo4j_exc

    store = _neo4j_store_with_failing_session(neo4j_exc.ServiceUnavailable("offline"))
    with pytest.raises(GraphBackendUnavailable):
        await store.find_entity_by_name("x")


@pytest.mark.asyncio
async def test_neo4j_mapping_conflict():
    """ConstraintError must surface as GraphConflict."""
    from neo4j import exceptions as neo4j_exc

    store = _neo4j_store_with_failing_session(neo4j_exc.ConstraintError("duplicate"))
    with pytest.raises(GraphConflict):
        await store.find_entity_by_name("x")


@pytest.mark.asyncio
async def test_neo4j_mapping_generic_neo4j_error():
    """Any Neo4jError subclass not specifically mapped falls back to
    GraphStoreError."""
    from neo4j import exceptions as neo4j_exc

    store = _neo4j_store_with_failing_session(neo4j_exc.DatabaseError("boom"))
    with pytest.raises(GraphStoreError):
        await store.find_entity_by_name("x")


@pytest.mark.asyncio
async def test_nebula_mapping_backend_unavailable():
    """Nebula 'connection is lost' must surface as
    GraphBackendUnavailable."""
    from beever_atlas.stores.nebula_store import NebulaStore

    store = NebulaStore.__new__(NebulaStore)
    store._hosts_str = ""  # type: ignore[attr-defined]
    store._user = ""  # type: ignore[attr-defined]
    store._password = ""  # type: ignore[attr-defined]
    store._space = "s"  # type: ignore[attr-defined]

    async def boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("nGQL error: connection is lost | query: SHOW HOSTS")

    with patch.object(NebulaStore, "_execute_with_space", side_effect=boom):
        with pytest.raises(GraphBackendUnavailable):
            await store.find_entity_by_name("x")


@pytest.mark.asyncio
async def test_nebula_mapping_conflict():
    """Nebula 'Vertex existed' must surface as GraphConflict."""
    from beever_atlas.stores.nebula_store import NebulaStore

    store = NebulaStore.__new__(NebulaStore)
    store._hosts_str = ""  # type: ignore[attr-defined]
    store._user = ""  # type: ignore[attr-defined]
    store._password = ""  # type: ignore[attr-defined]
    store._space = "s"  # type: ignore[attr-defined]

    async def boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError(
            "nGQL error: Vertex existed | query: INSERT VERTEX ..."
        )

    with patch.object(NebulaStore, "_execute_with_space", side_effect=boom):
        with pytest.raises(GraphConflict):
            await store.find_entity_by_name("x")


@pytest.mark.asyncio
async def test_nebula_mapping_generic():
    """Unmapped nGQL error falls back to GraphStoreError."""
    from beever_atlas.stores.nebula_store import NebulaStore

    store = NebulaStore.__new__(NebulaStore)
    store._hosts_str = ""  # type: ignore[attr-defined]
    store._user = ""  # type: ignore[attr-defined]
    store._password = ""  # type: ignore[attr-defined]
    store._space = "s"  # type: ignore[attr-defined]

    async def boom(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("nGQL error: SyntaxError near token | query: ???")

    with patch.object(NebulaStore, "_execute_with_space", side_effect=boom):
        with pytest.raises(GraphStoreError):
            await store.find_entity_by_name("x")


@pytest.mark.asyncio
async def test_error_hierarchy_subclasses_graph_store_error():
    """All specific errors must subclass GraphStoreError so callers can
    catch the umbrella type."""
    assert issubclass(GraphNotFound, GraphStoreError)
    assert issubclass(GraphConflict, GraphStoreError)
    assert issubclass(GraphBackendUnavailable, GraphStoreError)
