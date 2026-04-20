"""Task 7.6: operator view for MCP tool call volume by principal.

Covers the ``/api/admin/mcp-metrics`` endpoint and the rolling-window
aggregation in :mod:`beever_atlas.infra.mcp_metrics`. The React page was
intentionally deferred; ops engineers hit this endpoint with the admin
token directly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api.admin import router as admin_router
from beever_atlas.infra import auth as auth_mod
from beever_atlas.infra import mcp_metrics as metrics_mod


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics_mod.reset_counters()
    yield
    metrics_mod.reset_counters()


def _patch_admin(monkeypatch, token: str = "admin-token-abc"):
    fake = SimpleNamespace(admin_token=token)
    monkeypatch.setattr(auth_mod, "get_settings", lambda: fake)


def _app():
    app = FastAPI()
    app.include_router(admin_router)
    return app


# ---------------------------------------------------------------------------
# snapshot_counters (pure aggregation logic)
# ---------------------------------------------------------------------------


def test_snapshot_empty_buffer_returns_zero_state():
    snap = metrics_mod.snapshot_counters()
    assert snap["total_calls"] == 0
    assert snap["distinct_principals"] == 0
    assert snap["by_principal_tool"] == []


def test_record_tool_call_accumulates_into_snapshot():
    metrics_mod.record_tool_call(
        tool_name="ask_channel",
        principal_hash="mcp:alice",
        outcome="ok",
        duration_ms=42.0,
    )
    metrics_mod.record_tool_call(
        tool_name="ask_channel",
        principal_hash="mcp:alice",
        outcome="ok",
        duration_ms=58.0,
    )
    metrics_mod.record_tool_call(
        tool_name="trigger_sync",
        principal_hash="mcp:bob",
        outcome="rate_limited",
        duration_ms=3.0,
    )

    snap = metrics_mod.snapshot_counters()
    assert snap["total_calls"] == 3
    assert snap["distinct_principals"] == 2
    # Outcomes aggregated.
    assert snap["by_outcome"]["ok"] == 2
    assert snap["by_outcome"]["rate_limited"] == 1
    # Per-(principal, tool, outcome) rows present.
    alice_ok = next(
        r
        for r in snap["by_principal_tool"]
        if r["principal"] == "mcp:alice" and r["tool"] == "ask_channel"
    )
    assert alice_ok["count"] == 2
    assert alice_ok["outcome"] == "ok"
    # Latency stats per tool.
    ask = snap["by_tool_latency"]["ask_channel"]
    assert ask["count"] == 2
    assert ask["avg_ms"] == 50.0


def test_snapshot_prunes_events_outside_window(monkeypatch):
    # Seed one stale event and one fresh event; assert only the fresh one counts.
    import time as real_time

    now = real_time.time()
    # Stale event (2 hours ago), fresh event (now).
    with metrics_mod._buffer_lock:
        metrics_mod._event_buffer.append((now - 7200, "mcp:alice", "ask_channel", "ok", 10.0))
        metrics_mod._event_buffer.append((now, "mcp:bob", "ask_channel", "ok", 20.0))

    snap = metrics_mod.snapshot_counters(now=now)
    assert snap["total_calls"] == 1
    assert snap["distinct_principals"] == 1


# ---------------------------------------------------------------------------
# Admin endpoint
# ---------------------------------------------------------------------------


def test_mcp_metrics_requires_admin_token(monkeypatch):
    _patch_admin(monkeypatch, token="admin-token-abc")
    client = TestClient(_app())
    r = client.get("/api/admin/mcp-metrics")
    assert r.status_code == 401


def test_mcp_metrics_rejects_wrong_admin_token(monkeypatch):
    _patch_admin(monkeypatch, token="admin-token-abc")
    client = TestClient(_app())
    r = client.get("/api/admin/mcp-metrics", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401


def test_mcp_metrics_returns_snapshot_with_valid_admin(monkeypatch):
    _patch_admin(monkeypatch, token="admin-token-abc")
    # Seed some data.
    metrics_mod.record_tool_call("ask_channel", "mcp:alice", "ok", 42.0)
    metrics_mod.record_tool_call("trigger_sync", "mcp:bob", "rate_limited", 3.0)

    client = TestClient(_app())
    r = client.get(
        "/api/admin/mcp-metrics",
        headers={"X-Admin-Token": "admin-token-abc"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 2
    assert body["distinct_principals"] == 2
    assert body["by_outcome"]["ok"] == 1
    assert body["by_outcome"]["rate_limited"] == 1
    assert body["window_seconds"] == 3600


def test_mcp_metrics_reset_clears_buffer(monkeypatch):
    _patch_admin(monkeypatch, token="admin-token-abc")
    metrics_mod.record_tool_call("ask_channel", "mcp:alice", "ok", 42.0)
    assert metrics_mod.snapshot_counters()["total_calls"] == 1

    client = TestClient(_app())
    r = client.post(
        "/api/admin/mcp-metrics/reset",
        headers={"X-Admin-Token": "admin-token-abc"},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "reset"}
    assert metrics_mod.snapshot_counters()["total_calls"] == 0


def test_mcp_metrics_principal_hash_exposed_verbatim(monkeypatch):
    """Principal ids are mcp:<hash16> — non-reversible — safe to surface to admin."""
    _patch_admin(monkeypatch, token="admin-token-abc")
    metrics_mod.record_tool_call("ask_channel", "mcp:deadbeef12345678", "ok", 10.0)
    client = TestClient(_app())
    r = client.get(
        "/api/admin/mcp-metrics",
        headers={"X-Admin-Token": "admin-token-abc"},
    )
    body = r.json()
    assert body["by_principal_tool"][0]["principal"] == "mcp:deadbeef12345678"
