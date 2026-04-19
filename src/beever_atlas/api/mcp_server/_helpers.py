"""Shared helpers used across all MCP tool-group submodules."""

from __future__ import annotations

import logging
import re
from importlib.metadata import PackageNotFoundError, version

from fastmcp import Context

logger = logging.getLogger(__name__)

_INPUT_REGEX = re.compile(r"^[A-Za-z0-9_:\-]{1,128}$")


def _atlas_version() -> str:
    """Best-effort package version for the MCP ``initialize`` server-info block."""
    try:
        return version("beever-atlas")
    except PackageNotFoundError:
        return "0.1.0"


def _get_principal_id(ctx: Context) -> str | None:
    """Extract ``mcp_principal_id`` from the ASGI scope injected by MCPAuthMiddleware."""
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        state = request.scope.get("state") or {}
        return state.get("mcp_principal_id")
    except Exception:
        return None


def _get_principal_id_from_resource() -> str | None:
    """Extract ``mcp_principal_id`` inside a resource handler."""
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        state = request.scope.get("state") or {}
        return state.get("mcp_principal_id")
    except Exception:
        return None


def _validate_id(value: str, field: str) -> dict | None:
    """Return a structured ``invalid_parameter`` error if *value* fails the regex."""
    if not _INPUT_REGEX.match(value):
        return {"error": "invalid_parameter", "parameter": field}
    return None


__all__ = [
    "_atlas_version",
    "_get_principal_id",
    "_get_principal_id_from_resource",
    "_validate_id",
    "logger",
]
