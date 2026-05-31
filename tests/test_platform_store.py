"""Tests for PlatformStore serialization helpers and credential decryption.

mongomock is not installed, so full CRUD tests that require a live Motor collection
are omitted. These tests cover _to_doc, _from_doc, and decrypt_connection_credentials
using mock data and a valid encryption key.
"""

from __future__ import annotations

import secrets
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.models.platform_connection import PlatformConnection

_VALID_KEY_HEX = secrets.token_hex(32)


def _patch_key(monkeypatch) -> None:
    from beever_atlas.infra import config

    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", _VALID_KEY_HEX)
    config.get_settings.cache_clear()


def _make_store(monkeypatch) -> object:
    """Return a PlatformStore with a mock Motor collection."""
    from beever_atlas.stores.platform_store import PlatformStore

    mock_col = MagicMock()
    return PlatformStore(mock_col)


def _encrypted_conn(monkeypatch, **overrides) -> PlatformConnection:
    """Build a PlatformConnection with real encrypted credentials."""
    _patch_key(monkeypatch)
    from beever_atlas.infra.crypto import encrypt_credentials

    ciphertext, iv, tag = encrypt_credentials({"token": "xoxb-test"})
    defaults = dict(
        platform="slack",
        display_name="My Workspace",
        encrypted_credentials=ciphertext,
        credential_iv=iv,
        credential_tag=tag,
    )
    defaults.update(overrides)
    return PlatformConnection(**defaults)


class TestToDoc:
    def test_to_doc_returns_dict(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        conn = _encrypted_conn(monkeypatch)

        doc = store._to_doc(conn)

        assert isinstance(doc, dict)

    def test_to_doc_includes_all_model_fields(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        conn = _encrypted_conn(monkeypatch)

        doc = store._to_doc(conn)

        assert "id" in doc
        assert "platform" in doc
        assert "display_name" in doc
        assert "encrypted_credentials" in doc
        assert "credential_iv" in doc
        assert "credential_tag" in doc
        assert "selected_channels" in doc
        assert "status" in doc
        assert "source" in doc
        assert "created_at" in doc
        assert "updated_at" in doc

    def test_to_doc_preserves_field_values(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        conn = _encrypted_conn(monkeypatch)

        doc = store._to_doc(conn)

        assert doc["id"] == conn.id
        assert doc["platform"] == "slack"
        assert doc["display_name"] == "My Workspace"
        assert doc["status"] == "connected"
        assert doc["source"] == "ui"
        assert doc["selected_channels"] == []


class TestFromDoc:
    def test_from_doc_returns_platform_connection(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        conn = _encrypted_conn(monkeypatch)
        doc = store._to_doc(conn)

        result = store._from_doc(doc)

        assert isinstance(result, PlatformConnection)

    def test_from_doc_strips_mongodb_id_field(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        conn = _encrypted_conn(monkeypatch)
        doc = store._to_doc(conn)
        doc["_id"] = "some-mongo-object-id"

        result = store._from_doc(doc)

        assert not hasattr(result, "_id")

    def test_from_doc_preserves_all_field_values(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        conn = _encrypted_conn(monkeypatch, selected_channels=["C001", "C002"])
        doc = store._to_doc(conn)

        result = store._from_doc(doc)

        assert result.id == conn.id
        assert result.platform == conn.platform
        assert result.display_name == conn.display_name
        assert result.status == conn.status
        assert result.source == conn.source
        assert result.selected_channels == ["C001", "C002"]

    def test_to_doc_then_from_doc_round_trip_preserves_bytes(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        conn = _encrypted_conn(monkeypatch)
        doc = store._to_doc(conn)

        result = store._from_doc(doc)

        assert result.encrypted_credentials == conn.encrypted_credentials
        assert result.credential_iv == conn.credential_iv
        assert result.credential_tag == conn.credential_tag


class TestDecryptConnectionCredentials:
    def test_returns_original_credentials_dict(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        original = {"token": "xoxb-test", "team_id": "T999"}
        from beever_atlas.infra.crypto import encrypt_credentials

        ciphertext, iv, tag = encrypt_credentials(original)
        conn = PlatformConnection(
            platform="slack",
            display_name="Test",
            encrypted_credentials=ciphertext,
            credential_iv=iv,
            credential_tag=tag,
        )

        result = store.decrypt_connection_credentials(conn)

        assert result == original

    def test_returns_empty_dict_when_encrypted_empty(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        from beever_atlas.infra.crypto import encrypt_credentials

        ciphertext, iv, tag = encrypt_credentials({})
        conn = PlatformConnection(
            platform="discord",
            display_name="Test",
            encrypted_credentials=ciphertext,
            credential_iv=iv,
            credential_tag=tag,
        )

        result = store.decrypt_connection_credentials(conn)

        assert result == {}

    def test_tampered_credentials_raise_invalid_tag(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        from cryptography.exceptions import InvalidTag

        from beever_atlas.infra.crypto import encrypt_credentials

        ciphertext, iv, tag = encrypt_credentials({"token": "xoxb-test"})
        tampered = bytes([ciphertext[0] ^ 0xFF]) + ciphertext[1:]
        conn = PlatformConnection(
            platform="slack",
            display_name="Test",
            encrypted_credentials=tampered,
            credential_iv=iv,
            credential_tag=tag,
        )

        with pytest.raises(InvalidTag):
            store.decrypt_connection_credentials(conn)


class TestTeamsKnownTeamIdsField:
    """Coverage for the persistent ``teams_known_team_ids`` field added so
    Teams connections can bootstrap their team list from Mongo (parity with
    Slack/Discord/Mattermost bootstrapping from tokens) instead of relying
    on the chat-adapter's Redis cache surviving every container restart."""

    def test_defaults_to_empty_list(self, monkeypatch):
        _patch_key(monkeypatch)
        conn = _encrypted_conn(monkeypatch)
        assert conn.teams_known_team_ids == []

    def test_round_trip_preserves_team_ids(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        ids = [
            "85e9fb0c-6cf9-4e94-9cc4-eb81ea6cd9de",
            "11111111-2222-3333-4444-555555555555",
        ]
        conn = _encrypted_conn(monkeypatch, platform="teams", teams_known_team_ids=ids)

        doc = store._to_doc(conn)
        assert doc["teams_known_team_ids"] == ids

        # Pre-migration Mongo docs are missing the field entirely; the
        # Pydantic default must fill in cleanly so they decode without error.
        legacy_doc = dict(doc)
        legacy_doc.pop("teams_known_team_ids")
        legacy_conn = store._from_doc(legacy_doc)
        assert legacy_conn.teams_known_team_ids == []

    @pytest.mark.asyncio
    async def test_add_teams_known_team_id_uses_addToSet_and_dedups(self, monkeypatch):
        """The store helper must use ``$addToSet`` so concurrent writes from
        multiple webhook deliveries don't double-insert. We mock the Motor
        call and assert the operator + payload shape so a future refactor
        can't silently regress to ``$push``."""
        _patch_key(monkeypatch)
        from beever_atlas.stores.platform_store import PlatformStore

        existing = _encrypted_conn(
            monkeypatch,
            platform="teams",
            teams_known_team_ids=["85e9fb0c-6cf9-4e94-9cc4-eb81ea6cd9de"],
        )

        mock_col = MagicMock()
        # Motor returns the updated doc (we asked for return_document=True).
        mock_col.find_one_and_update = AsyncMock(
            return_value=PlatformStore(mock_col)._to_doc(existing),
        )
        store = PlatformStore(mock_col)

        result = await store.add_teams_known_team_id(
            existing.id,
            "85e9fb0c-6cf9-4e94-9cc4-eb81ea6cd9de",
        )

        # Inspect the call shape — filter by `id`, $addToSet operator.
        mock_col.find_one_and_update.assert_awaited_once()
        call_args, call_kwargs = mock_col.find_one_and_update.call_args
        # First positional: filter; second: update document.
        filter_doc, update_doc = call_args[0], call_args[1]
        assert filter_doc == {"id": existing.id}
        assert "$addToSet" in update_doc
        assert update_doc["$addToSet"] == {
            "teams_known_team_ids": "85e9fb0c-6cf9-4e94-9cc4-eb81ea6cd9de",
        }
        # `updated_at` must be touched so callers see a fresh timestamp.
        assert "updated_at" in update_doc["$set"]
        # Hands back the deserialised connection.
        assert isinstance(result, PlatformConnection)
        assert result.id == existing.id

    @pytest.mark.asyncio
    async def test_add_teams_known_team_id_returns_none_when_missing(self, monkeypatch):
        _patch_key(monkeypatch)
        from beever_atlas.stores.platform_store import PlatformStore

        mock_col = MagicMock()
        mock_col.find_one_and_update = AsyncMock(return_value=None)

        result = await PlatformStore(mock_col).add_teams_known_team_id(
            "missing-conn",
            "85e9fb0c-6cf9-4e94-9cc4-eb81ea6cd9de",
        )
        assert result is None
