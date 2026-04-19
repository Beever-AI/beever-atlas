"""Tests for MCP per-tool audit logging and rate-limit integration (Phase 7, task 7.5).

Covers:
- A tool call emits exactly one mcp_tool_call event with all expected keys.
- A rate-limited call has outcome="rate_limited" in the audit log.
- An access-denied call has outcome="channel_access_denied".

Implementation note on log capture:
    The beever_atlas loggers use StructuredFormatter which writes directly to
    stderr (bypassing the root logger's caplog handler). We therefore capture
    audit events by adding a temporary in-memory ListHandler to the module
    logger rather than relying on pytest's caplog fixture.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.infra import mcp_rate_limit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ListHandler(logging.Handler):
    """Simple handler that collects LogRecord objects into a list."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _capture_logger(name: str):
    """Temporarily attach a ListHandler to *name* logger and yield it."""
    handler = _ListHandler()
    lg = logging.getLogger(name)
    lg.addHandler(handler)
    try:
        yield handler
    finally:
        lg.removeHandler(handler)


def _req(principal_id: str = "mcp:testhash00000000"):
    m = MagicMock()
    m.scope = {
        "state": {
            "mcp_principal_id": principal_id,
            "mcp_request_id": "req-test-1234",
        }
    }
    return m


def _ctx(principal_id: str = "mcp:testhash00000000"):
    c = MagicMock()
    c.info = AsyncMock()
    c.warning = AsyncMock()
    return c


def _get_tool_fn(mcp, name: str):
    for key, tool in mcp._local_provider._components.items():
        if key.startswith(f"tool:{name}@") or key == f"tool:{name}":
            return tool.fn
    raise KeyError(f"No tool named '{name}' found in registry")


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    mcp_rate_limit.reset_state()
    yield
    mcp_rate_limit.reset_state()


# ---------------------------------------------------------------------------
# 7.5.1: Tool call emits exactly one mcp_tool_call event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tool_call_emits_audit_event():
    """A successful tool call emits exactly one mcp_tool_call structured log entry."""
    from beever_atlas.api.mcp_server import build_mcp

    with patch(
        "fastmcp.server.dependencies.get_http_request", return_value=_req()
    ), patch(
        "beever_atlas.capabilities.connections.list_connections",
        new=AsyncMock(return_value=[]),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "whoami")

        with _capture_logger("beever_atlas.api.mcp_server") as handler:
            result = await fn(ctx=_ctx())

    # Verify the tool returned successfully
    assert result.get("error") != "rate_limited"

    # Find mcp_tool_call log entries (exclude mcp_tool_call_metric lines)
    audit_records = [
        r for r in handler.records
        if getattr(r, "data", {}).get("event") == "mcp_tool_call"
    ]
    assert len(audit_records) == 1, (
        f"Expected exactly 1 mcp_tool_call audit record, got {len(audit_records)}: "
        f"{[r.getMessage() for r in handler.records]}"
    )

    msg = audit_records[0].getMessage()
    assert "principal=" in msg
    assert "tool=whoami" in msg
    assert "outcome=" in msg
    assert "duration_ms=" in msg


@pytest.mark.asyncio
async def test_audit_event_has_all_required_keys():
    """The structured data dict on the audit record contains all required keys."""
    from beever_atlas.api.mcp_server import build_mcp

    with patch(
        "fastmcp.server.dependencies.get_http_request", return_value=_req()
    ), patch(
        "beever_atlas.capabilities.connections.list_connections",
        new=AsyncMock(return_value=[]),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "whoami")

        with _capture_logger("beever_atlas.api.mcp_server") as handler:
            await fn(ctx=_ctx())

    audit_records = [
        r for r in handler.records
        if getattr(r, "data", {}).get("event") == "mcp_tool_call"
    ]
    assert len(audit_records) >= 1

    data = audit_records[0].data
    for key in ("event", "request_id", "principal", "tool", "outcome", "duration_ms"):
        assert key in data, f"Missing key '{key}' in audit data: {data}"


# ---------------------------------------------------------------------------
# 7.5.2: Rate-limited call has outcome="rate_limited"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limited_call_audit_outcome():
    """When a principal hits the trigger_sync rate limit, audit outcome='rate_limited'."""
    from beever_atlas.api.mcp_server import build_mcp

    principal = "mcp:ratelimitedprincipal"

    with patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=_req(principal_id=principal),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "trigger_sync")

        # Exhaust the 5/min trigger_sync limit
        with patch(
            "beever_atlas.capabilities.sync.trigger_sync",
            new=AsyncMock(return_value={"job_id": "j1", "status": "queued"}),
        ):
            for _ in range(5):
                await fn(channel_id="ch-test", ctx=_ctx(principal_id=principal))

        # 6th call — should be rate-limited; capture the audit log
        with _capture_logger("beever_atlas.api.mcp_server") as handler:
            result = await fn(channel_id="ch-test", ctx=_ctx(principal_id=principal))

    assert result.get("error") == "rate_limited"
    assert "retry_after_seconds" in result

    rate_limited_audits = [
        r for r in handler.records
        if getattr(r, "data", {}).get("event") == "mcp_tool_call"
        and getattr(r, "data", {}).get("outcome") == "rate_limited"
    ]
    assert len(rate_limited_audits) >= 1, (
        f"Expected at least one rate_limited audit entry, got: "
        f"{[r.getMessage() for r in handler.records]}"
    )


# ---------------------------------------------------------------------------
# 7.5.3: Access-denied call has outcome="channel_access_denied"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_access_denied_call_audit_outcome():
    """When a tool returns channel_access_denied, audit records outcome accordingly."""
    from beever_atlas.api.mcp_server import build_mcp
    from beever_atlas.capabilities.errors import ChannelAccessDenied

    with patch(
        "fastmcp.server.dependencies.get_http_request", return_value=_req()
    ), patch(
        "beever_atlas.capabilities.memory.search_channel_facts",
        new=AsyncMock(side_effect=ChannelAccessDenied("ch-secret")),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "search_channel_facts")

        with _capture_logger("beever_atlas.api.mcp_server") as handler:
            result = await fn(
                channel_id="ch-secret",
                query="anything",
                ctx=_ctx(),
            )

    assert result.get("error") == "channel_access_denied"

    denied_audits = [
        r for r in handler.records
        if getattr(r, "data", {}).get("event") == "mcp_tool_call"
        and getattr(r, "data", {}).get("outcome") == "channel_access_denied"
    ]
    assert len(denied_audits) >= 1, (
        f"Expected at least one channel_access_denied audit entry, got: "
        f"{[r.getMessage() for r in handler.records]}"
    )


# ---------------------------------------------------------------------------
# 7.3: rate_limited return shape is exactly correct
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limited_return_shape():
    """Rate-limited response is EXACTLY {error: 'rate_limited', retry_after_seconds: int}."""
    from beever_atlas.api.mcp_server import build_mcp

    principal = "mcp:shapetestprincipal"

    with patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=_req(principal_id=principal),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "ask_channel")

        with patch(
            "beever_atlas.infra.channel_access.assert_channel_access",
            new=AsyncMock(return_value=None),
        ):
            # Exhaust ask_channel limit (30/min)
            for _ in range(30):
                await fn(
                    channel_id="ch-x",
                    question="test",
                    ctx=_ctx(principal_id=principal),
                )

            result = await fn(
                channel_id="ch-x",
                question="test",
                ctx=_ctx(principal_id=principal),
            )

    assert set(result.keys()) == {"error", "retry_after_seconds"}, (
        f"Unexpected keys in rate_limited result: {result}"
    )
    assert result["error"] == "rate_limited"
    assert isinstance(result["retry_after_seconds"], int)
    assert result["retry_after_seconds"] >= 1
