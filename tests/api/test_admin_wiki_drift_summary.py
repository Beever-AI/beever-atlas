"""Tests for ``GET /api/admin/wiki-drift/summary?days=N``.

Spec: ``openspec/changes/close-the-soak-loop/specs/wiki-soak-instrumentation/``
covers §5: aggregation + pass-criterion + freshness + days clamp.

Convention: ``pyproject.toml`` sets ``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api.admin import router as admin_router
from beever_atlas.infra import auth as auth_mod
from beever_atlas.stores import init_stores


_ADMIN_TOKEN = "admin-token-abc"


def _patch_admin(monkeypatch, token: str = _ADMIN_TOKEN) -> None:
    fake = SimpleNamespace(admin_token=token)
    monkeypatch.setattr(auth_mod, "get_settings", lambda: fake)


@pytest.fixture
def fake_stores(monkeypatch):
    """Inject a Mongo fake whose ``aggregate_wiki_drift_summary`` returns
    canned per-channel rows. Tests append rows + record the days argument
    they were called with."""
    state = {"rows": [], "days_called_with": []}

    class _Mongo:
        async def aggregate_wiki_drift_summary(self, days: int):
            state["days_called_with"].append(days)
            return list(state["rows"])

    container = SimpleNamespace(mongodb=_Mongo())
    init_stores(container)  # type: ignore[arg-type]
    return state


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


def _row(channel_id: str, p50_median: float, p95_median: float, *, fresh: bool = True) -> dict:
    last_run = datetime.now(tz=UTC) - (timedelta(minutes=5) if fresh else timedelta(hours=24))
    return {
        "channel_id": channel_id,
        "page_count": 10,
        "levenshtein_section_p50_median": p50_median,
        "levenshtein_section_p95_median": p95_median,
        "last_run_ts": last_run,
    }


# ---------------------------------------------------------------------------
# 5.7 — All channels meeting threshold → pass=true
# ---------------------------------------------------------------------------


def test_all_channels_pass(client: TestClient, fake_stores) -> None:
    fake_stores["rows"] = [
        _row("C1", 0.10, 0.22),
        _row("C2", 0.12, 0.25),
        _row("C3", 0.05, 0.10),
    ]
    resp = client.get("/api/admin/wiki-drift/summary", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pass"] is True
    assert body["data_fresh"] is True
    assert all(c["pass_criterion_met"] for c in body["channels"])


# ---------------------------------------------------------------------------
# 5.8 — One failing channel → pass=false
# ---------------------------------------------------------------------------


def test_one_failing_channel(client: TestClient, fake_stores) -> None:
    fake_stores["rows"] = [
        _row("A", 0.10, 0.20),
        _row("B", 0.18, 0.35),  # over threshold on both
    ]
    resp = client.get("/api/admin/wiki-drift/summary", headers=_admin_headers())
    body = resp.json()
    assert body["pass"] is False
    by_id = {c["channel_id"]: c for c in body["channels"]}
    assert by_id["A"]["pass_criterion_met"] is True
    assert by_id["B"]["pass_criterion_met"] is False


# ---------------------------------------------------------------------------
# 5.9 — data_fresh=false when last_run_ts > 60 min old
# ---------------------------------------------------------------------------


def test_data_fresh_false_when_stale(client: TestClient, fake_stores) -> None:
    fake_stores["rows"] = [
        _row("A", 0.10, 0.20, fresh=True),
        _row("B", 0.10, 0.20, fresh=False),  # stale
    ]
    resp = client.get("/api/admin/wiki-drift/summary", headers=_admin_headers())
    body = resp.json()
    assert body["pass"] is True  # both meet threshold
    assert body["data_fresh"] is False


# ---------------------------------------------------------------------------
# 5.10 — Empty collection returns documented shape with HTTP 200
# ---------------------------------------------------------------------------


def test_empty_collection_returns_empty_shape(client: TestClient, fake_stores) -> None:
    fake_stores["rows"] = []
    resp = client.get("/api/admin/wiki-drift/summary", headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"channels": [], "pass": False, "data_fresh": False}


# ---------------------------------------------------------------------------
# 5.11 — Non-admin token rejected
# ---------------------------------------------------------------------------


def test_non_admin_rejected(client: TestClient) -> None:
    resp = client.get("/api/admin/wiki-drift/summary")
    assert resp.status_code == 401
    resp = client.get(
        "/api/admin/wiki-drift/summary",
        headers={"X-Admin-Token": "wrong"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 5.12 — days param clamped at 60
# ---------------------------------------------------------------------------


def test_days_param_clamped_to_max(client: TestClient, fake_stores) -> None:
    resp = client.get("/api/admin/wiki-drift/summary?days=10000", headers=_admin_headers())
    assert resp.status_code == 200
    # The aggregator was called with 60, not 10000.
    assert fake_stores["days_called_with"] == [60]
