"""Tests for ``GET /api/admin/wiki-maintainer/metrics``.

Spec: ``openspec/changes/close-the-soak-loop/specs/wiki-soak-instrumentation/``

Covers §4 of close-the-soak-loop: an admin-token-gated observability
endpoint mirroring the extraction-worker shape.

Convention: ``pyproject.toml`` sets ``asyncio_mode = "auto"``; no
``@pytest.mark.asyncio`` decorators required.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api.admin import router as admin_router
from beever_atlas.infra import auth as auth_mod
from beever_atlas.services import wiki_maintainer as wm_mod
from beever_atlas.services.wiki_maintainer import (
    WikiMaintainer,
    init_wiki_maintainer,
)
from beever_atlas.stores import init_stores


_ADMIN_TOKEN = "admin-token-abc"


def _patch_admin(monkeypatch, token: str = _ADMIN_TOKEN) -> None:
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
    """Inject a Mongo fake whose ``wiki_pages`` aggregate returns canned
    rows. Tests can append to the closure list to control the response."""
    rows: list[dict] = []
    raise_on_aggregate = {"flag": False}

    class _WikiPagesCol:
        def aggregate(self, _pipeline):
            if raise_on_aggregate["flag"]:

                class _Bad:
                    def __aiter__(self):
                        return self

                    async def __anext__(self):
                        raise RuntimeError("mongo down")

                return _Bad()
            return _FakeAggregateCursor(list(rows))

    class _Db:
        def __getitem__(self, name):
            assert name == "wiki_pages"
            return _WikiPagesCol()

    mongodb = SimpleNamespace(db=_Db())
    container = SimpleNamespace(mongodb=mongodb)
    init_stores(container)  # type: ignore[arg-type]
    return container, rows, raise_on_aggregate


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


def _fresh_maintainer() -> WikiMaintainer:
    page_store = AsyncMock()
    return WikiMaintainer(page_store=page_store)


@pytest.fixture(autouse=True)
def _reset_maintainer_singleton():
    """Snapshot + restore the WikiMaintainer module singleton per test.

    Without this fixture, tests that call ``init_wiki_maintainer(...)``
    leave their fake maintainer registered for every subsequent test.
    Under non-deterministic test ordering (xdist or alphabetical), a
    later test that expects ``get_wiki_maintainer() is None`` (e.g. the
    zeroed-shape regression test below) would fail intermittently.
    """
    saved = wm_mod._maintainer_instance
    yield
    wm_mod._maintainer_instance = saved


# ---------------------------------------------------------------------------
# 4.10 — Idle maintainer returns zeroed shape
# ---------------------------------------------------------------------------


def test_idle_maintainer_zero_counters(client: TestClient) -> None:
    init_wiki_maintainer(_fresh_maintainer())
    resp = client.get("/api/admin/wiki-maintainer/metrics", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["apply_update_count_5min"] == 0
    assert body["apply_update_count_15min"] == 0
    assert body["apply_update_count_60min"] == 0
    assert body["mark_dirty_count_5min"] == 0
    assert body["apply_update_failures"] == []
    assert body["rewrite_count_by_page_kind"] == {
        "topic": 0,
        "entity": 0,
        "decisions": 0,
        "faq": 0,
        "action_items": 0,
    }
    assert body["pending_dirty_pages_per_channel"] == {}


# ---------------------------------------------------------------------------
# 4.11 — apply_update success populates by-kind counter
# ---------------------------------------------------------------------------


def test_apply_update_success_increments_by_kind(client: TestClient) -> None:
    m = _fresh_maintainer()
    m._record_apply_update_success("topic:auth")
    m._record_apply_update_success("topic:billing")
    m._record_apply_update_success("entity:alice")
    m._record_apply_update_success("entity:bob")
    m._record_apply_update_success("entity:carol")
    init_wiki_maintainer(m)

    resp = client.get("/api/admin/wiki-maintainer/metrics", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rewrite_count_by_page_kind"]["topic"] == 2
    assert body["rewrite_count_by_page_kind"]["entity"] == 3
    assert body["apply_update_count_5min"] == 5


# ---------------------------------------------------------------------------
# 4.12 — Failures list capped at 10 (oldest dropped)
# ---------------------------------------------------------------------------


def test_failures_capped_at_ten(client: TestClient) -> None:
    m = _fresh_maintainer()
    for i in range(15):
        m._record_apply_update_failure("C1", f"topic:p{i}", RuntimeError("boom"))
    init_wiki_maintainer(m)

    resp = client.get("/api/admin/wiki-maintainer/metrics", headers=_admin_headers())
    body = resp.json()
    assert len(body["apply_update_failures"]) == 10
    # Oldest dropped first → first remaining is index 5.
    assert body["apply_update_failures"][0]["page_id"] == "topic:p5"
    assert body["apply_update_failures"][-1]["page_id"] == "topic:p14"


# ---------------------------------------------------------------------------
# 4.13 — Rolling-window trim drops entries older than 60 min
# ---------------------------------------------------------------------------


def test_rolling_window_trims_at_60min(client: TestClient) -> None:
    m = _fresh_maintainer()
    # Inject one record older than the 60-min cutoff and one fresh one.
    fake_now = time.monotonic()
    m._apply_update_records.append((fake_now - 4000.0, "topic"))
    m._apply_update_records.append((fake_now, "entity"))
    init_wiki_maintainer(m)

    resp = client.get("/api/admin/wiki-maintainer/metrics", headers=_admin_headers())
    body = resp.json()
    # Only the fresh one survives the trim.
    assert body["apply_update_count_60min"] == 1
    assert body["rewrite_count_by_page_kind"]["entity"] == 1
    assert body["rewrite_count_by_page_kind"]["topic"] == 0


# ---------------------------------------------------------------------------
# 4.14 — Non-admin token rejected
# ---------------------------------------------------------------------------


def test_non_admin_rejected(client: TestClient) -> None:
    resp = client.get("/api/admin/wiki-maintainer/metrics")
    assert resp.status_code == 401
    resp = client.get(
        "/api/admin/wiki-maintainer/metrics",
        headers={"X-Admin-Token": "wrong"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 4.15 — Admin + idle maintainer returns documented zeroed shape (singleton
# absent path).
# ---------------------------------------------------------------------------


def test_admin_idle_maintainer_returns_zeroed_shape(client: TestClient, monkeypatch) -> None:
    # Force ``get_wiki_maintainer`` to return None (lifespan not yet run).
    wm_mod._maintainer_instance = None
    resp = client.get("/api/admin/wiki-maintainer/metrics", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["apply_update_count_5min"] == 0
    assert body["apply_update_failures"] == []
    assert body["pending_dirty_pages_per_channel"] == {}


# ---------------------------------------------------------------------------
# 4.16 — Populated maintainer reports real counts
# ---------------------------------------------------------------------------


def test_populated_maintainer_reports_real_counts(client: TestClient, fake_stores) -> None:
    _, rows, _ = fake_stores
    rows.extend(
        [
            {"_id": "C1", "count": 4},
            {"_id": "C2", "count": 1},
        ]
    )

    m = _fresh_maintainer()
    m._record_apply_update_success("topic:auth")
    m._record_apply_update_success("decisions")
    m._record_mark_dirty(2)
    init_wiki_maintainer(m)

    resp = client.get("/api/admin/wiki-maintainer/metrics", headers=_admin_headers())
    body = resp.json()
    assert body["apply_update_count_5min"] == 2
    assert body["mark_dirty_count_5min"] == 2
    assert body["pending_dirty_pages_per_channel"] == {"C1": 4, "C2": 1}


# ---------------------------------------------------------------------------
# 4.17 — Aggregate failure resilience
# ---------------------------------------------------------------------------


def test_aggregate_failure_resilient(client: TestClient, fake_stores) -> None:
    _, _, raise_flag = fake_stores
    raise_flag["flag"] = True

    m = _fresh_maintainer()
    m._record_apply_update_success("topic:auth")
    init_wiki_maintainer(m)

    resp = client.get("/api/admin/wiki-maintainer/metrics", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    # Other metrics still computed.
    assert body["apply_update_count_5min"] == 1
    # Pending-dirty defaulted to empty.
    assert body["pending_dirty_pages_per_channel"] == {}
