"""Tests for `_validate_production` rejection of the well-known
`CREDENTIAL_MASTER_KEY` placeholder (issue #41).

The placeholder `00...deadbeef` is shipped publicly in `.env.example`
and is valid 64-char hex, so the bare `hex_ok` check accepts it. Using
it as the AES-256-GCM master key makes "encrypted" credentials
effectively plaintext to anyone who reads the repo.

The validator now rejects it explicitly: hard error in production,
loud warning (problem string contains INSECURE / PLAINTEXT) in dev.
A regression test asserts the constant matches `.env.example` so the
two sources cannot drift.
"""

from __future__ import annotations

import pathlib
import re
import secrets

import pytest

from beever_atlas.infra import config as config_mod
from beever_atlas.infra.config import Settings, _INSECURE_PLACEHOLDER_KEY


# ── Fixtures ────────────────────────────────────────────────────────────


def _prod_kwargs(**overrides) -> dict:
    """Return a kwargs dict that satisfies every other production check
    so we isolate the placeholder check under test."""
    base = dict(
        beever_env="production",
        credential_master_key=secrets.token_hex(32),
        neo4j_auth="neo4j/this-is-a-strong-password-not-the-dev-default",
        nebula_password="nebula-strong-pw",
        bridge_api_key="bridge-real-key-aaaaaaaa",
        api_keys="user-real-key-bbbbbbbb",
        admin_token="admin-real-token-cccccccc",
    )
    base.update(overrides)
    return base


# ── Tests ───────────────────────────────────────────────────────────────


def test_placeholder_rejected_in_production() -> None:
    """Production raises ValueError when the master key is the public placeholder."""
    with pytest.raises(ValueError) as exc_info:
        Settings(**_prod_kwargs(credential_master_key=_INSECURE_PLACEHOLDER_KEY))
    msg = str(exc_info.value)
    assert "INSECURE" in msg, f"expected INSECURE in error; got: {msg}"
    assert "PLAINTEXT" in msg, f"expected PLAINTEXT in error; got: {msg}"


def test_placeholder_warns_loud_in_dev(monkeypatch) -> None:
    """Dev/test mode emits a WARNING containing INSECURE / PLAINTEXT.

    The autouse `_auth_bypass` fixture imports `beever_atlas.server.app`
    which sets `propagate=False` on the `beever_atlas` logger, so
    caplog (root) doesn't see records from `beever_atlas.infra.config`.
    Capture via direct monkeypatch on the module logger instead.
    """
    captured: list[str] = []
    monkeypatch.setattr(
        config_mod.logger,
        "warning",
        lambda msg, *a, **kw: captured.append(msg % a if a else msg),
    )
    Settings(
        beever_env="development",
        credential_master_key=_INSECURE_PLACEHOLDER_KEY,
    )
    matched = [m for m in captured if "INSECURE" in m and "PLAINTEXT" in m]
    assert matched, f"expected a WARNING containing INSECURE/PLAINTEXT keywords; got: {captured}"


def test_valid_random_key_passes_in_production() -> None:
    """A freshly-generated 64-char hex key satisfies the validator."""
    fresh = secrets.token_hex(32)
    assert fresh != _INSECURE_PLACEHOLDER_KEY
    # No exception raised.
    Settings(**_prod_kwargs(credential_master_key=fresh))


def test_placeholder_matches_env_example() -> None:
    """Drift guard: the constant in config.py and `.env.example` MUST agree.
    Use grep-based extraction (NOT line-number indexing) so cosmetic edits
    to `.env.example` don't break this test."""
    # Two parents up from this test file (tests/infra/) → repo root.
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    env_example = (repo_root / ".env.example").read_text()
    match = re.search(
        r"^CREDENTIAL_MASTER_KEY=(\S+)$",
        env_example,
        re.MULTILINE,
    )
    assert match, ".env.example must contain CREDENTIAL_MASTER_KEY=..."
    assert match.group(1) == _INSECURE_PLACEHOLDER_KEY, (
        "drift between .env.example and config._INSECURE_PLACEHOLDER_KEY — "
        "either both updated together or neither"
    )
