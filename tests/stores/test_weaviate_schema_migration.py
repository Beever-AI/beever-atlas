"""Tests for `WeaviateStore` schema-migration symmetry (issue #38).

Covers `_apply_schema_migration` helper + symmetric behavior between
`ensure_schema` (async) and `_ensure_schema_sync` (sync) when called
against an existing collection. All tests use `unittest.mock` — no
live Weaviate.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

from weaviate.classes.config import Property

from beever_atlas.stores.weaviate_store import COLLECTION_NAME, WeaviateStore


def _store_with_mock_client(mock_client) -> WeaviateStore:
    """Construct a WeaviateStore without going through the real connect path."""
    store = WeaviateStore.__new__(WeaviateStore)
    store._client = mock_client  # bypass async connect
    store._url = "http://test"
    store._api_key = ""
    return store


def _mock_collection_with_existing(prop_names: list[str]) -> MagicMock:
    """Build a mock collection whose `config.get().properties` returns
    SimpleNamespace objects matching the given names."""
    collection = MagicMock()
    config = MagicMock()
    config.get.return_value = SimpleNamespace(
        properties=[SimpleNamespace(name=n) for n in prop_names],
    )
    collection.config = config
    return collection


# ── Helper unit tests ───────────────────────────────────────────────────


def test_apply_schema_migration_adds_missing_properties() -> None:
    """The helper adds each missing property with explicit `name=` and
    `data_type=` kwargs (Architect Patch 1 — assert kwargs, not just
    that the call happened, so a regression that swaps name/type is
    caught)."""
    store = _store_with_mock_client(MagicMock())
    # Existing collection has only the FIRST property of _EXPECTED_PROPERTIES.
    first_name, _first_dtype = store._EXPECTED_PROPERTIES[0]
    collection = _mock_collection_with_existing([first_name])

    store._apply_schema_migration(collection)

    # Every property AFTER the first should have been added with kwargs.
    expected_calls = [
        call(Property(name=name, data_type=dtype)) for name, dtype in store._EXPECTED_PROPERTIES[1:]
    ]
    assert collection.config.add_property.call_args_list == expected_calls
    # First property was already present — must NOT have been re-added.
    assert all(
        c != call(Property(name=first_name, data_type=_first_dtype))
        for c in collection.config.add_property.call_args_list
    )


def test_apply_schema_migration_noop_when_up_to_date() -> None:
    store = _store_with_mock_client(MagicMock())
    all_names = [n for n, _ in store._EXPECTED_PROPERTIES]
    collection = _mock_collection_with_existing(all_names)

    store._apply_schema_migration(collection)

    collection.config.add_property.assert_not_called()


# ── Sync path tests ─────────────────────────────────────────────────────


def test_ensure_schema_sync_creates_collection_when_missing() -> None:
    client = MagicMock()
    client.collections.exists.return_value = False
    store = _store_with_mock_client(client)

    store._ensure_schema_sync()

    client.collections.create.assert_called_once()
    # Verify the create call sets the full property list (defense-in-depth).
    create_kwargs = client.collections.create.call_args.kwargs
    assert create_kwargs["name"] == COLLECTION_NAME
    assert len(create_kwargs["properties"]) == len(store._EXPECTED_PROPERTIES)


def test_ensure_schema_sync_migrates_existing_collection() -> None:
    """Sync path now migrates when collection already exists (issue #38).
    Verify each `add_property` call uses explicit `name=` AND `data_type=`
    kwargs (Architect Patch 1)."""
    client = MagicMock()
    client.collections.exists.return_value = True
    # Existing collection has half the expected properties.
    half_idx = len(WeaviateStore._EXPECTED_PROPERTIES) // 2
    existing_names = [n for n, _ in WeaviateStore._EXPECTED_PROPERTIES[:half_idx]]
    collection = _mock_collection_with_existing(existing_names)
    client.collections.get.return_value = collection
    store = _store_with_mock_client(client)

    store._ensure_schema_sync()

    # Migration helper must have been called via the new branch.
    expected_calls = [
        call(Property(name=name, data_type=dtype))
        for name, dtype in WeaviateStore._EXPECTED_PROPERTIES[half_idx:]
    ]
    assert collection.config.add_property.call_args_list == expected_calls
    # Must NOT have called `collections.create` (collection already exists).
    client.collections.create.assert_not_called()


def test_ensure_schema_sync_noop_when_collection_up_to_date() -> None:
    client = MagicMock()
    client.collections.exists.return_value = True
    all_names = [n for n, _ in WeaviateStore._EXPECTED_PROPERTIES]
    collection = _mock_collection_with_existing(all_names)
    client.collections.get.return_value = collection
    store = _store_with_mock_client(client)

    store._ensure_schema_sync()

    collection.config.add_property.assert_not_called()
    client.collections.create.assert_not_called()


# ── Async path delegates to helper ──────────────────────────────────────


async def test_ensure_schema_delegates_to_helper_when_collection_exists() -> None:
    """After the refactor, `ensure_schema()` calls the same helper as
    the sync path — so future schema additions update both paths."""
    client = MagicMock()
    client.collections.exists.return_value = True
    half_idx = len(WeaviateStore._EXPECTED_PROPERTIES) // 2
    existing_names = [n for n, _ in WeaviateStore._EXPECTED_PROPERTIES[:half_idx]]
    collection = _mock_collection_with_existing(existing_names)
    client.collections.get.return_value = collection
    store = _store_with_mock_client(client)

    await store.ensure_schema()

    expected_calls = [
        call(Property(name=name, data_type=dtype))
        for name, dtype in WeaviateStore._EXPECTED_PROPERTIES[half_idx:]
    ]
    assert collection.config.add_property.call_args_list == expected_calls
    client.collections.create.assert_not_called()
