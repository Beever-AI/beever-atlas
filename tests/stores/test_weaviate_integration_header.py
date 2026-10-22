"""The Weaviate clients report Beever Atlas via ``X-Weaviate-Client-Integration``."""

from __future__ import annotations

import importlib.metadata
from unittest.mock import MagicMock

import weaviate
from weaviate.classes.init import AdditionalConfig

from beever_atlas.stores import weaviate_store
from beever_atlas.stores.weaviate_store import register_integration

HEADER = "X-Weaviate-Client-Integration"


def _offline_client() -> weaviate.WeaviateClient:
    return weaviate.WeaviateClient(
        connection_params=weaviate.connect.ConnectionParams.from_url(
            "http://localhost:8080", grpc_port=50051
        ),
        additional_config=AdditionalConfig(),
        skip_init_checks=True,
    )


def test_header_set_on_rest_and_grpc() -> None:
    client = _offline_client()
    register_integration(client)

    expected = f"beever-atlas/{importlib.metadata.version('beever-atlas')}"
    conn = client._connection
    assert conn.additional_headers[HEADER] == expected
    assert (HEADER.lower(), expected) in (conn.grpc_headers() or ())


def test_unresolvable_version_reports_unknown(monkeypatch) -> None:
    def _missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(weaviate_store.importlib.metadata, "version", _missing)
    client = _offline_client()
    register_integration(client)
    assert client._connection.additional_headers[HEADER] == "beever-atlas/unknown"


def test_registration_failure_is_swallowed() -> None:
    client = MagicMock()
    client.integrations.configure.side_effect = RuntimeError("boom")
    register_integration(client)  # must not raise
