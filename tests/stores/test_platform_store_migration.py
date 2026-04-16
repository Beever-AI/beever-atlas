"""Migration-compat tests for PlatformStore (RES-177 H1).

Covers the `channel-access-control` migration scenarios:
- A `PlatformConnection` document stored BEFORE the change (no
  `owner_principal_id` field) deserialises cleanly as `None`.
- `backfill_legacy_owners()` rewrites legacy rows to ``"legacy:shared"``
  and is idempotent (second call produces zero writes).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.models.platform_connection import PlatformConnection
from beever_atlas.stores.platform_store import PlatformStore


def test_pre_migration_document_deserialises_without_error():
    """A dict that predates RES-177 (no owner_principal_id key) must load."""
    legacy_doc = {
        "id": "c1",
        "platform": "slack",
        "source": "ui",
        "display_name": "legacy",
        "status": "connected",
        "selected_channels": ["C1"],
        "encrypted_credentials": b"",
        "credential_iv": b"",
        "credential_tag": b"",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    # No `owner_principal_id` field — Pydantic must default it to None.
    conn = PlatformConnection(**legacy_doc)
    assert conn.owner_principal_id is None


def test_pre_migration_document_with_null_owner_deserialises():
    legacy_doc = {
        "id": "c1",
        "platform": "slack",
        "source": "ui",
        "display_name": "legacy",
        "status": "connected",
        "selected_channels": ["C1"],
        "encrypted_credentials": b"",
        "credential_iv": b"",
        "credential_tag": b"",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "owner_principal_id": None,
    }
    conn = PlatformConnection(**legacy_doc)
    assert conn.owner_principal_id is None


@pytest.mark.asyncio
async def test_backfill_legacy_owners_sets_sentinel_and_is_idempotent():
    """Mock the mongo collection; confirm update_many targets legacy rows only
    and that the second call reports zero writes."""
    mock_col = MagicMock()
    # First call: simulate two legacy rows rewritten.
    first = MagicMock()
    first.modified_count = 2
    # Second call: nothing to do.
    second = MagicMock()
    second.modified_count = 0
    mock_col.update_many = AsyncMock(side_effect=[first, second])

    store = PlatformStore(mock_col)
    assert await store.backfill_legacy_owners() == 2
    assert await store.backfill_legacy_owners() == 0

    # Each call targets the SAME "legacy rows" filter and sets the sentinel.
    for call in mock_col.update_many.await_args_list:
        filt, update = call.args[0], call.args[1]
        assert filt == {
            "$or": [
                {"owner_principal_id": {"$exists": False}},
                {"owner_principal_id": None},
            ]
        }
        assert update == {"$set": {"owner_principal_id": "legacy:shared"}}


@pytest.mark.asyncio
async def test_backfill_legacy_owners_swallows_collection_errors():
    """Fresh installs may not have the collection yet; backfill must be safe."""
    mock_col = MagicMock()
    mock_col.update_many = AsyncMock(side_effect=RuntimeError("no collection"))
    store = PlatformStore(mock_col)
    assert await store.backfill_legacy_owners() == 0
