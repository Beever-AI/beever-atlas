"""Tests for ``GET /api/admin/extraction-worker/metrics``.

Spec: ``openspec/changes/oss-redesign-production-wiring/specs/wiki-soak-instrumentation/``

Covers §20 of the production-wiring change: an admin-token-gated
observability endpoint that returns rolling claim/success rates,
per-channel queue depth, breaker state, and recent failures so
operators can spot a flapping worker without grepping logs.

Convention: no ``@pytest.mark.asyncio`` decorators; ``pyproject.toml``
sets ``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api.admin import router as admin_router
from beever_atlas.infra import auth as auth_mod
from beever_atlas.services.extraction_worker import (
    ExtractionWorker,
    init_extraction_worker,
)
from beever_atlas.stores import init_stores


_ADMIN_TOKEN = "admin-token-abc"


def _patch_admin(monkeypatch, token: str = _ADMIN_TOKEN):
    fake = SimpleNamespace(admin_token=token)
    monkeypatch.setattr(auth_mod, "get_settings", lambda: fake)


class _FakeAggregateCursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


@pytest.fixture
def fake_stores(monkeypatch):
    """Wire up an in-memory ``stores.mongodb._channel_messages.aggregate``."""
    aggregate_rows: list[dict] = []

    def _aggregate(_pipeline):
        return _FakeAggregateCursor(list(aggregate_rows))

    fake_messages = SimpleNamespace(aggregate=_aggregate)
    mongodb = SimpleNamespace(_channel_messages=fake_messages)
    container = SimpleNamespace(mongodb=mongodb)
    init_stores(container)  # type: ignore[arg-type]
    return container, aggregate_rows


@pytest.fixture
def app(monkeypatch, fake_stores):  # noqa: ARG001
    _patch_admin(monkeypatch)
    app = FastAPI()
    app.include_router(admin_router)
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": _ADMIN_TOKEN}


def test_metrics_idle_worker_returns_zeros(client: TestClient, monkeypatch) -> None:
    """No worker singleton → endpoint returns the documented zero shape
    without erroring."""
    # Make get_extraction_worker return None (uninitialized).
    monkeypatch.setattr(
        "beever_atlas.services.extraction_worker.get_extraction_worker",
        lambda: None,
    )
    resp = client.get("/api/admin/extraction-worker/metrics", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queue_depth_per_channel"] == {}
    assert body["claim_rate_5min"] == 0
    assert body["success_rate_5min"] == 1.0
    assert body["breaker_state"] == "unknown"
    assert body["recent_failures"] == []


def test_metrics_healthy_worker_reports_rates_and_breaker(
    client: TestClient, monkeypatch, fake_stores
) -> None:
    """Worker with one tick recorded reports a positive claim_rate +
    success_rate of 1.0 + the breaker's current state."""
    _, aggregate_rows = fake_stores
    aggregate_rows.extend(
        [
            {"_id": "C1", "count": 30},
            {"_id": "C2", "count": 5},
        ]
    )

    fake_breaker = MagicMock()
    fake_breaker.state.return_value = "closed"

    worker = ExtractionWorker(breaker=fake_breaker)
    worker._record_tick_metrics({"claimed": 100, "succeeded": 100, "failed": 0})
    init_extraction_worker(worker)

    resp = client.get("/api/admin/extraction-worker/metrics", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["queue_depth_per_channel"] == {"C1": 30, "C2": 5}
    assert body["breaker_state"] == "closed"
    assert body["success_rate_5min"] == 1.0
    # 100 claims in the 5-minute window → ~0.33/sec
    assert body["claim_rate_5min"] > 0


def test_metrics_records_recent_failures(client: TestClient, monkeypatch) -> None:
    """The worker's per-row failure recorder feeds the endpoint's
    ``recent_failures`` field, capped at 10 entries."""
    fake_breaker = MagicMock()
    fake_breaker.state.return_value = "open"

    worker = ExtractionWorker(breaker=fake_breaker)
    for i in range(15):
        worker._record_failure(
            message_id=f"msg-{i}",
            channel_id="C1",
            error_class="ServerError",
        )
    init_extraction_worker(worker)

    resp = client.get("/api/admin/extraction-worker/metrics", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    # Capped at 10 entries (most recent), breaker state passed through.
    assert len(body["recent_failures"]) == 10
    assert body["recent_failures"][0]["message_id"].startswith("msg-")
    assert body["recent_failures"][-1]["message_id"] == "msg-14"
    assert body["breaker_state"] == "open"


def test_metrics_non_admin_token_rejected(client: TestClient) -> None:
    resp = client.get("/api/admin/extraction-worker/metrics")
    assert resp.status_code == 401
    resp = client.get(
        "/api/admin/extraction-worker/metrics",
        headers={"X-Admin-Token": "wrong"},
    )
    assert resp.status_code == 401


def test_metrics_resilient_to_aggregate_failure(
    client: TestClient, monkeypatch, fake_stores
) -> None:
    """If the queue-depth aggregate raises, the endpoint still returns a
    valid response with empty queue_depth_per_channel."""

    def _raise_aggregate(_pipeline):
        class _BadCursor:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("mongo down")

        return _BadCursor()

    container, _ = fake_stores
    container.mongodb._channel_messages.aggregate = _raise_aggregate
    init_stores(container)

    fake_breaker = MagicMock()
    fake_breaker.state.return_value = "closed"
    worker = ExtractionWorker(breaker=fake_breaker)
    init_extraction_worker(worker)

    resp = client.get("/api/admin/extraction-worker/metrics", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["queue_depth_per_channel"] == {}
    assert body["breaker_state"] == "closed"
