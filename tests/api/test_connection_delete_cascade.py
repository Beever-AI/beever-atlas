"""Tests for the connection-delete cascade (delete-channel-v2 Wave 3, AC#13).

``DELETE /api/connections/{connection_id}`` hard-purges channels SOLELY owned
by the connection being deleted (no OTHER connection lists them in
``selected_channels``). Shared channels are left alone. The cascade is
best-effort: a purge failure must NOT 500 the connection delete (still 204).

The store fan-out is unit-tested in Wave 2; here we monkeypatch
``purge_channel`` and assert WHICH channels are purged + that the 204 contract
survives a purge failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

import beever_atlas.api.connections as connections_mod
import beever_atlas.services.channel_deletion as deletion_mod
from beever_atlas.models.platform_connection import PlatformConnection
from beever_atlas.server.app import app


def _conn(
    *, connection_id: str, selected: list[str], owner: str = "user:test"
) -> PlatformConnection:
    return PlatformConnection(
        id=connection_id,
        platform="slack",
        source="ui",
        display_name=connection_id,
        status="connected",
        selected_channels=selected,
        encrypted_credentials=b"",
        credential_iv=b"",
        credential_tag=b"",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        owner_principal_id=owner,
    )


@pytest.fixture
async def client(mock_stores):  # noqa: ARG001 — wires the global stores
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _wire(monkeypatch, *, target, others, purge=None):
    """Wire fake stores + side-effect-free adapter/proxy helpers.

    ``target`` is the connection being deleted; ``others`` are the remaining
    connections (used to decide sharing).
    """
    stores = connections_mod.get_stores()
    all_conns = [target, *others]
    stores.platform.get_connection = AsyncMock(return_value=target)
    stores.platform.list_connections = AsyncMock(return_value=all_conns)
    stores.platform.delete_connection = AsyncMock(return_value=True)

    monkeypatch.setattr(connections_mod, "_unregister_adapter", AsyncMock(return_value=None))
    monkeypatch.setattr(connections_mod, "_refresh_proxy_hosts", AsyncMock(return_value=None))

    spy = purge or AsyncMock(return_value={"channel_id": "x", "status": "completed", "errors": {}})
    # The endpoint does a LAZY ``from beever_atlas.services.channel_deletion
    # import purge_channel`` inside the handler, so the import resolves to the
    # source module's symbol — patch THAT, not the connections module.
    monkeypatch.setattr(deletion_mod, "purge_channel", spy)
    return spy


# ---------------------------------------------------------------------------
# AC#13 — sole-owned purged, shared NOT purged, 204 preserved
# ---------------------------------------------------------------------------


async def test_cascade_purges_sole_owned_not_shared(client, monkeypatch):
    # X is solely owned by c1; Y is shared with c2.
    target = _conn(connection_id="c1", selected=["X", "Y"])
    other = _conn(connection_id="c2", selected=["Y", "Z"])
    spy = _wire(monkeypatch, target=target, others=[other])

    resp = await client.delete("/api/connections/c1")
    assert resp.status_code == 204, resp.text

    purged = {call.args[0] for call in spy.await_args_list}
    assert purged == {"X"}, f"expected only X purged, got {purged}"


async def test_cascade_disabled_purges_nothing(client, monkeypatch):
    target = _conn(connection_id="c1", selected=["X", "Y"])
    other = _conn(connection_id="c2", selected=["Y"])
    spy = _wire(monkeypatch, target=target, others=[other])

    resp = await client.delete("/api/connections/c1?cascade=false")
    assert resp.status_code == 204, resp.text
    spy.assert_not_called()


async def test_cascade_purge_failure_does_not_500(client, monkeypatch):
    """A purge that RAISES for the sole-owned channel must not fail the
    connection delete — it stays 204."""
    target = _conn(connection_id="c1", selected=["X", "Y"])
    other = _conn(connection_id="c2", selected=["Y"])

    async def _boom(channel_id, *, principal_id):  # noqa: ARG001
        if channel_id == "X":
            raise RuntimeError("weaviate down")
        return {"channel_id": channel_id, "status": "completed", "errors": {}}

    spy = AsyncMock(side_effect=_boom)
    _wire(monkeypatch, target=target, others=[other], purge=spy)

    resp = await client.delete("/api/connections/c1")
    assert resp.status_code == 204, resp.text
    # Purge WAS attempted for X (and raised), but the delete still succeeded.
    purged = {call.args[0] for call in spy.await_args_list}
    assert "X" in purged


async def test_cascade_all_shared_purges_nothing(client, monkeypatch):
    """When every channel of the deleted connection is also listed by another
    connection, the cascade purges nothing."""
    target = _conn(connection_id="c1", selected=["X", "Y"])
    other = _conn(connection_id="c2", selected=["X", "Y"])
    spy = _wire(monkeypatch, target=target, others=[other])

    resp = await client.delete("/api/connections/c1")
    assert resp.status_code == 204, resp.text
    spy.assert_not_called()


async def test_cascade_skips_channel_without_delete_authz(client, monkeypatch):
    """Per-channel destructive authz: a sole-owned channel the principal may
    NOT delete is SKIPPED (purge_channel not called for it), while an allowed
    sole-owned channel is still purged. A denial must NOT 403 the whole
    connection delete — it stays 204."""
    # Both X and Y are sole-owned by c1 (no other connection lists them).
    target = _conn(connection_id="c1", selected=["X", "Y"])
    other = _conn(connection_id="c2", selected=["Z"])
    spy = _wire(monkeypatch, target=target, others=[other])

    # Authz allows Y but denies X (raises HTTPException 403 for X). The endpoint
    # does a LAZY ``from beever_atlas.infra.channel_access import
    # assert_channel_delete_access``, so the import resolves to the source
    # module's symbol — patch THAT, not the connections module.
    import beever_atlas.infra.channel_access as channel_access_mod

    async def _authz(principal, channel_id):  # noqa: ARG001
        if channel_id == "X":
            from fastapi import HTTPException

            raise HTTPException(status_code=403, detail="denied")

    monkeypatch.setattr(
        channel_access_mod, "assert_channel_delete_access", AsyncMock(side_effect=_authz)
    )

    resp = await client.delete("/api/connections/c1")
    assert resp.status_code == 204, resp.text

    purged = {call.args[0] for call in spy.await_args_list}
    assert purged == {"Y"}, f"expected only Y purged (X denied), got {purged}"


async def test_delete_missing_connection_404(client, monkeypatch):
    stores = connections_mod.get_stores()
    stores.platform.get_connection = AsyncMock(return_value=None)
    spy = AsyncMock()
    monkeypatch.setattr(deletion_mod, "purge_channel", spy)

    resp = await client.delete("/api/connections/does-not-exist")
    assert resp.status_code == 404, resp.text
    spy.assert_not_called()


async def test_delete_connection_denied_for_non_owner(client, monkeypatch):
    """IDOR guard (CRITICAL): a principal that does not own the connection gets
    403, the connection is NOT deleted, and NO channel is cascade-purged.

    The test client authenticates as ``user:test`` (conftest); the target is
    owned by a different principal, so ``assert_connection_owned`` denies it
    (explicit non-matching owner is rejected in both single- and multi-tenant
    mode — the single-tenant fallback only admits un-owned/legacy connections).
    """
    target = _conn(connection_id="conn-other", selected=["C1"], owner="user:intruder")
    spy = _wire(monkeypatch, target=target, others=[])
    delete_mock = AsyncMock(return_value=True)
    connections_mod.get_stores().platform.delete_connection = delete_mock

    resp = await client.delete("/api/connections/conn-other")

    assert resp.status_code == 403, resp.text
    spy.assert_not_called()  # no channel was cascade-purged
    delete_mock.assert_not_called()  # the connection itself was NOT deleted
