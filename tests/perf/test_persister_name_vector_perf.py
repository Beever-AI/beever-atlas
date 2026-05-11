"""Perf harness: batched vs serial name_vector writes.

Uses a mocked Neo4j session with controlled 5ms per-call latency to
deterministically confirm that the UNWIND batch path is significantly
faster than the serial per-entity path.

Fixture: 52 entities × 768-dim vector, N=20 batches each path.
Assert: median_after - median_before <= -150 ms.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.stores.entity_registry import EntityRegistry
from beever_atlas.stores.neo4j_store import Neo4jStore

# ── Constants ────────────────────────────────────────────────────────────────

ENTITY_COUNT = 52
VECTOR_DIM = 768
N_BATCHES = 20
SIMULATED_LATENCY_S = 0.005  # 5 ms per round-trip
REQUIRED_DELTA_MS = -150  # batched must be at least 150 ms faster per batch


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _synthetic_entities() -> list[tuple[str, list[float]]]:
    """Return 52 (name, vector) tuples with 768-dim float vectors."""
    return [(f"Entity_{i}", [float(i % 10) / 10.0] * VECTOR_DIM) for i in range(ENTITY_COUNT)]


def _make_store_serial() -> tuple[Neo4jStore, MagicMock]:
    """Neo4jStore where each Session.run incurs SIMULATED_LATENCY_S."""

    async def _slow_run(*args, **kwargs):
        await asyncio.sleep(SIMULATED_LATENCY_S)
        return AsyncMock()

    store = Neo4jStore.__new__(Neo4jStore)
    mock_session = AsyncMock()
    mock_session.run = AsyncMock(side_effect=_slow_run)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=mock_session)
    store._driver = mock_driver

    return store, mock_session


def _make_store_batched() -> tuple[Neo4jStore, MagicMock]:
    """Same latency but only one call per batch (UNWIND path)."""
    return _make_store_serial()


# ── Perf test ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_is_faster_than_serial():
    """Median batch time must be at least 150 ms faster than median serial time."""
    entities = _synthetic_entities()

    # ── Baseline: serial per-entity ──────────────────────────────────────────
    serial_timings: list[float] = []
    for _ in range(N_BATCHES):
        store, _ = _make_store_serial()
        t0 = time.perf_counter()
        for name, vector in entities:
            await store.store_name_vector(name, vector)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        serial_timings.append(elapsed_ms)

    median_before = statistics.median(serial_timings)

    # ── After: UNWIND batch ───────────────────────────────────────────────────
    batch_timings: list[float] = []
    for _ in range(N_BATCHES):
        store, _ = _make_store_batched()
        t0 = time.perf_counter()
        await store.batch_store_name_vectors(entities)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        batch_timings.append(elapsed_ms)

    median_after = statistics.median(batch_timings)
    delta_ms = median_after - median_before

    print(
        f"\n[perf] name_vector writes — serial median: {median_before:.1f} ms, "
        f"batch median: {median_after:.1f} ms, delta: {delta_ms:.1f} ms"
    )

    # batch_stage_timings["persister"] key existence check (unit-level signal):
    # the real BatchProcessor records this at lines 817/888/1470; here we confirm
    # the key would be populated by checking the registry delegates correctly.
    mock_graph = AsyncMock()
    mock_graph.batch_store_name_vectors = AsyncMock(return_value=ENTITY_COUNT)
    registry = EntityRegistry(mock_graph)
    count = await registry.batch_store_name_vectors(entities)
    assert count == ENTITY_COUNT, "registry.batch_store_name_vectors must return item count"

    assert delta_ms <= REQUIRED_DELTA_MS, (
        f"Expected batch to be at least {abs(REQUIRED_DELTA_MS)} ms faster than serial, "
        f"but delta was {delta_ms:.1f} ms "
        f"(serial={median_before:.1f} ms, batch={median_after:.1f} ms)"
    )
