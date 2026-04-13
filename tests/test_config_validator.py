"""Tests for production fail-fast config validation."""

from __future__ import annotations

import pytest

from beever_atlas.infra.config import Settings


GOOD_KEY = "a" * 64
GOOD_ARGS = dict(
    beever_env="production",
    credential_master_key=GOOD_KEY,
    neo4j_auth="neo4j/a-real-prod-password",
    nebula_password="a-real-nebula-pw",
    bridge_api_key="bridge-secret",
    api_keys="prod-key",
    admin_token="prod-admin",
)


def test_production_accepts_valid():
    Settings(**GOOD_ARGS)


def test_production_rejects_short_master_key():
    args = dict(GOOD_ARGS, credential_master_key="abc123")
    with pytest.raises(ValueError, match="CREDENTIAL_MASTER_KEY"):
        Settings(**args)


def test_production_rejects_non_hex_master_key():
    args = dict(GOOD_ARGS, credential_master_key="z" * 64)
    with pytest.raises(ValueError, match="CREDENTIAL_MASTER_KEY"):
        Settings(**args)


def test_production_rejects_default_neo4j_password():
    args = dict(GOOD_ARGS, neo4j_auth="neo4j/beever_atlas_dev")
    with pytest.raises(ValueError, match="NEO4J"):
        Settings(**args)


def test_production_rejects_default_nebula_password():
    args = dict(GOOD_ARGS, nebula_password="nebula")
    with pytest.raises(ValueError, match="NEBULA"):
        Settings(**args)


def test_production_rejects_empty_bridge_key():
    args = dict(GOOD_ARGS, bridge_api_key="")
    with pytest.raises(ValueError, match="BRIDGE_API_KEY"):
        Settings(**args)


def test_production_rejects_empty_api_keys():
    args = dict(GOOD_ARGS, api_keys="")
    with pytest.raises(ValueError, match="BEEVER_API_KEYS"):
        Settings(**args)


def test_production_rejects_empty_admin_token():
    args = dict(GOOD_ARGS, admin_token="")
    with pytest.raises(ValueError, match="BEEVER_ADMIN_TOKEN"):
        Settings(**args)


def test_development_allows_dev_defaults(caplog):
    dev_args = dict(
        beever_env="development",
        credential_master_key="",
        neo4j_auth="neo4j/beever_atlas_dev",
        nebula_password="nebula",
        bridge_api_key="",
        api_keys="",
        admin_token="",
    )
    s = Settings(**dev_args)
    assert s.beever_env == "development"
