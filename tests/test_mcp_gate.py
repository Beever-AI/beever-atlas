"""Regression tests for the BEEVER_MCP_ENABLED mount gate.

The /mcp mount is currently UNAUTHENTICATED (see openspec change
'atlas-mcp-server' for the full auth rewrite). This hotfix gates it
behind BEEVER_MCP_ENABLED so the mount is off by default. These tests
verify the toggle — not the MCP protocol surface itself.
"""

from __future__ import annotations

import importlib
import sys

from fastapi.testclient import TestClient

from beever_atlas.infra import config as config_mod


def _reload_app():
    """Reload the cached app module so module-level `_settings = get_settings()`
    re-reads the current environment."""
    config_mod.get_settings.cache_clear()
    mod_name = "beever_atlas.server.app"
    if mod_name in sys.modules:
        return importlib.reload(sys.modules[mod_name])
    return importlib.import_module(mod_name)


def test_mcp_disabled_by_default(monkeypatch):
    """With BEEVER_MCP_ENABLED unset/false, /mcp/ returns 404."""
    monkeypatch.delenv("BEEVER_MCP_ENABLED", raising=False)
    app_mod = _reload_app()
    try:
        client = TestClient(app_mod.app)
        response = client.get("/mcp/")
        assert response.status_code == 404
    finally:
        # Restore a clean state so subsequent tests get the default app.
        config_mod.get_settings.cache_clear()
        importlib.reload(sys.modules["beever_atlas.server.app"])


def test_mcp_enabled_when_flag_true(monkeypatch):
    """With BEEVER_MCP_ENABLED=true, the /mcp mount is reachable.

    The MCP protocol may reject a plain GET with 400/405/415 — or FastMCP
    may raise an internal error because its lifespan wasn't hooked up in
    the test harness. Either way, anything OTHER than a clean 404 proves
    the mount toggle works. Without the gate, /mcp/ 404s.
    """
    monkeypatch.setenv("BEEVER_MCP_ENABLED", "true")
    app_mod = _reload_app()
    try:
        # raise_server_exceptions=False converts internal errors into 500
        # responses instead of bubbling them — FastMCP's StreamableHTTP
        # app needs its own lifespan and will raise RuntimeError if it
        # reaches the handler without one. Either a 5xx or a protocol
        # response proves the mount is live.
        client = TestClient(app_mod.app, raise_server_exceptions=False)
        response = client.get("/mcp/")
        assert response.status_code != 404, (
            f"expected /mcp/ to be mounted, got 404 (body={response.text!r})"
        )
    finally:
        # Restore a clean state so subsequent tests get the default app.
        monkeypatch.delenv("BEEVER_MCP_ENABLED", raising=False)
        config_mod.get_settings.cache_clear()
        importlib.reload(sys.modules["beever_atlas.server.app"])
