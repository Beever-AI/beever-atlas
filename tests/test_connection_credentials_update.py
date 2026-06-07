"""Tests for PATCH /api/connections/{id}/credentials — non-destructive
credential rotation (e.g. flipping Slack to Socket Mode without a data-purging
delete). The endpoint merges non-empty provided keys over the stored
credentials, re-registers the adapter, and persists."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import beever_atlas.api.connections as conns
from beever_atlas.api.connections import (
    UpdateCredentialsRequest,
    update_connection_credentials,
)


def _conn(**overrides) -> SimpleNamespace:
    base = dict(
        id="c1",
        platform="slack",
        display_name="Slack",
        selected_channels=[],
        status="connected",
        error_message=None,
        source="ui",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def store(monkeypatch):
    st = MagicMock()
    st.get_connection = AsyncMock(return_value=_conn())
    st.decrypt_connection_credentials = MagicMock(
        return_value={"bot_token": "xoxb-1", "signing_secret": "s1"}
    )
    st.update_connection = AsyncMock(
        return_value=_conn(updated_at=datetime(2026, 1, 2, tzinfo=UTC))
    )
    monkeypatch.setattr(conns, "get_stores", lambda: SimpleNamespace(platform=st))
    monkeypatch.setattr(conns, "_register_adapter", AsyncMock())
    monkeypatch.setattr(conns, "_refresh_proxy_hosts", AsyncMock())
    from beever_atlas.infra import channel_access

    monkeypatch.setattr(channel_access, "assert_connection_owned", AsyncMock())
    return st


_PRINCIPAL = SimpleNamespace(id="u1")


async def test_merges_app_token_over_existing_and_reregisters(store):
    resp = await update_connection_credentials(
        "c1",
        UpdateCredentialsRequest(credentials={"app_token": "xapp-1", "signing_secret": ""}),
        principal=_PRINCIPAL,
    )
    # signing_secret="" is ignored (keep existing); app_token merged in.
    _, kwargs = store.update_connection.call_args
    assert kwargs["credentials"] == {
        "bot_token": "xoxb-1",
        "signing_secret": "s1",
        "app_token": "xapp-1",
    }
    # Adapter re-registered with the merged credentials so the bot rebuilds.
    conns._register_adapter.assert_awaited_once()
    assert resp.id == "c1"


async def test_empty_payload_is_422(store):
    with pytest.raises(HTTPException) as ei:
        await update_connection_credentials(
            "c1",
            UpdateCredentialsRequest(credentials={"app_token": "   "}),
            principal=_PRINCIPAL,
        )
    assert ei.value.status_code == 422
    # Must not touch the adapter or persist when there's nothing to do.
    conns._register_adapter.assert_not_awaited()
    store.update_connection.assert_not_awaited()


async def test_unknown_connection_is_404(store):
    store.get_connection = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as ei:
        await update_connection_credentials(
            "missing",
            UpdateCredentialsRequest(credentials={"app_token": "xapp-1"}),
            principal=_PRINCIPAL,
        )
    assert ei.value.status_code == 404


async def test_owner_gate_denies_non_owner(store, monkeypatch):
    from beever_atlas.capabilities.errors import ConnectionAccessDenied
    from beever_atlas.infra import channel_access

    monkeypatch.setattr(
        channel_access,
        "assert_connection_owned",
        AsyncMock(side_effect=ConnectionAccessDenied("nope")),
    )
    with pytest.raises(HTTPException) as ei:
        await update_connection_credentials(
            "c1",
            UpdateCredentialsRequest(credentials={"app_token": "xapp-1"}),
            principal=_PRINCIPAL,
        )
    assert ei.value.status_code == 403
    # Denied before any write.
    store.update_connection.assert_not_awaited()
