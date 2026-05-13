"""PR-G: ``atlas apply`` declarative + env modes."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.atlas_apply import (
    _interpolate,
    diff_and_apply,
    format_diff,
    load_config_from_env,
    load_config_from_yaml,
)


class _AsyncCursor:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "_AsyncCursor":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _Result:
    def __init__(self, matched: int = 0) -> None:
        self.matched_count = matched
        self.modified_count = matched


class _FakeCollection:
    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any], _proj: Any = None) -> _AsyncCursor:
        return _AsyncCursor([d for d in self._docs if self._matches(d, query)])

    async def find_one(self, query: dict[str, Any], _proj: Any = None) -> Any:
        for d in self._docs:
            if self._matches(d, query):
                return d
        return None

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self._docs.append(dict(doc))

    async def update_one(
        self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> _Result:
        for d in self._docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))
                return _Result(matched=1)
        if upsert:
            new = dict(update.get("$set", {}))
            new.update(query)
            self._docs.append(new)
        return _Result(matched=0)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        if "$or" in query:
            return any(_FakeCollection._matches(doc, q) for q in query["$or"])
        return all(doc.get(k) == v for k, v in query.items())


def _stores() -> Any:
    return SimpleNamespace(
        mongodb=SimpleNamespace(
            db={"endpoints": _FakeCollection(), "llm_assignments": _FakeCollection()}
        )
    )


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "ab" * 32)


# ── interpolation ────────────────────────────────────────────────────────


def test_interpolate_resolves_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "secret-XYZ")
    assert _interpolate("Bearer ${MY_TOKEN}") == "Bearer secret-XYZ"


def test_interpolate_unknown_var_becomes_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNSET_VAR", raising=False)
    assert _interpolate("prefix-${UNSET_VAR}-suffix") == "prefix--suffix"


def test_interpolate_no_braces_unchanged() -> None:
    assert _interpolate("plain string") == "plain string"


# ── Mode B: env JSON envelope ───────────────────────────────────────────


def test_env_envelope_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BEEVER_ENDPOINTS", raising=False)
    assert load_config_from_env() is None


def test_env_envelope_parses_single_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "BEEVER_ENDPOINTS",
        json.dumps(
            [
                {
                    "name": "openai",
                    "preset": "openai",
                    "api_key": "sk-xyz",
                }
            ]
        ),
    )
    config = load_config_from_env()
    assert config is not None
    assert len(config.endpoints) == 1
    assert config.endpoints[0].name == "openai"
    assert config.endpoints[0].api_key == "sk-xyz"


def test_env_envelope_invalid_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEVER_ENDPOINTS", "not-json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_config_from_env()


def test_env_envelope_non_array_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEVER_ENDPOINTS", json.dumps({"oops": "object"}))
    with pytest.raises(ValueError, match="must be a JSON array"):
        load_config_from_env()


def test_env_envelope_includes_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEEVER_ENDPOINTS", "[]")
    monkeypatch.setenv("BEEVER_PRESET", "openai-quality")
    config = load_config_from_env()
    assert config is not None
    assert config.preset == "openai-quality"


def test_env_envelope_interpolates_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_KEY", "AIza-from-env")
    monkeypatch.setenv(
        "BEEVER_ENDPOINTS",
        json.dumps([{"name": "g", "preset": "google_ai", "api_key": "${MY_KEY}"}]),
    )
    config = load_config_from_env()
    assert config is not None
    assert config.endpoints[0].api_key == "AIza-from-env"


# ── Mode C: YAML ────────────────────────────────────────────────────────


def test_yaml_loads_endpoints_and_assignments(tmp_path: Path) -> None:
    yaml_text = textwrap.dedent(
        """
        endpoints:
          - name: anthropic-prod
            preset: anthropic
            api_key: sk-ant-test
            rpm: 100
          - name: ollama-local
            preset: ollama
            auth_type: none

        assignments:
          qa_agent:
            endpoint: anthropic-prod
            model: claude-sonnet-4-6
            temperature: 0.2
          image_describer:
            endpoint: ollama-local
            model: gemma3:e4b

        preset: claude-quality-gemini-fast
        """
    )
    config_path = tmp_path / "atlas.yaml"
    config_path.write_text(yaml_text)
    config = load_config_from_yaml(config_path)

    assert len(config.endpoints) == 2
    assert config.endpoints[0].name == "anthropic-prod"
    assert config.endpoints[0].rpm == 100
    assert len(config.assignments) == 2
    assert config.assignments[0].consumer == "qa_agent"
    assert config.assignments[0].temperature == 0.2
    assert config.preset == "claude-quality-gemini-fast"


def test_yaml_interpolates_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_KEY", "sk-ant-from-env")
    yaml_text = textwrap.dedent(
        """
        endpoints:
          - name: anthropic
            preset: anthropic
            api_key: ${ANTHROPIC_KEY}
        """
    )
    config_path = tmp_path / "atlas.yaml"
    config_path.write_text(yaml_text)
    config = load_config_from_yaml(config_path)
    assert config.endpoints[0].api_key == "sk-ant-from-env"


# ── diff_and_apply ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_creates_endpoints_and_assignments() -> None:
    config = load_config_from_env()
    # Build manually so we don't need an env round-trip.
    from scripts.atlas_apply import _AssignmentSpec, _Config, _EndpointSpec

    config = _Config(
        endpoints=[
            _EndpointSpec(
                name="openai-prod",
                preset="openai",
                api_key="sk-prod",
                base_url="https://api.openai.com/v1",
                models=["gpt-4o-mini"],
            )
        ],
        assignments=[
            _AssignmentSpec(
                consumer="qa_agent",
                endpoint="openai-prod",
                model="gpt-4o-mini",
                temperature=0.1,
            )
        ],
    )
    stores = _stores()
    diff = await diff_and_apply(config, stores, apply=True)
    actions = [(e.action, e.kind, e.name) for e in diff]
    assert ("create", "endpoint", "openai-prod") in actions
    assert ("create", "assignment", "qa_agent") in actions


@pytest.mark.asyncio
async def test_apply_idempotent_second_run_zero_changes() -> None:
    from scripts.atlas_apply import _Config, _EndpointSpec

    config = _Config(
        endpoints=[
            _EndpointSpec(name="openai", preset="openai", api_key="sk", base_url="https://x")
        ],
        assignments=[],
    )
    stores = _stores()
    await diff_and_apply(config, stores, apply=True)
    # Second run — nothing changed.
    diff = await diff_and_apply(config, stores, apply=True)
    assert all(e.action == "unchanged" for e in diff)


@pytest.mark.asyncio
async def test_apply_assignment_with_unknown_endpoint_raises() -> None:
    from scripts.atlas_apply import _AssignmentSpec, _Config

    config = _Config(
        endpoints=[],
        assignments=[_AssignmentSpec(consumer="qa_agent", endpoint="missing-ep", model="x")],
    )
    stores = _stores()
    with pytest.raises(RuntimeError, match="unknown endpoint"):
        await diff_and_apply(config, stores, apply=True)


@pytest.mark.asyncio
async def test_apply_preset_with_missing_endpoint_raises() -> None:
    from scripts.atlas_apply import _Config

    config = _Config(
        endpoints=[],
        assignments=[],
        preset="claude-quality-gemini-fast",
    )
    stores = _stores()
    with pytest.raises(RuntimeError, match="requirements not met"):
        await diff_and_apply(config, stores, apply=True)


@pytest.mark.asyncio
async def test_plan_does_not_write() -> None:
    from scripts.atlas_apply import _Config, _EndpointSpec

    config = _Config(
        endpoints=[
            _EndpointSpec(name="openai", preset="openai", api_key="sk", base_url="https://x")
        ],
        assignments=[],
    )
    stores = _stores()
    diff = await diff_and_apply(config, stores, apply=False)
    assert any(e.action == "create" for e in diff)
    # No endpoint actually written.
    assert stores.mongodb.db["endpoints"]._docs == []


def test_format_diff_human_readable() -> None:
    from scripts.atlas_apply import _DiffEntry

    diff = [
        _DiffEntry(action="create", kind="endpoint", name="x"),
        _DiffEntry(action="update", kind="endpoint", name="y"),
        _DiffEntry(action="unchanged", kind="assignment", name="qa_agent"),
    ]
    text = format_diff(diff)
    assert "+ endpoint" in text
    assert "~ endpoint" in text
    assert "Plan:" in text


@pytest.mark.asyncio
async def test_apply_with_fallback_endpoint_resolves_id() -> None:
    """Assignment ``fallback_endpoint`` (name) is resolved to ``fallback_endpoint_id`` (uuid)."""
    from scripts.atlas_apply import _AssignmentSpec, _Config, _EndpointSpec

    config = _Config(
        endpoints=[
            _EndpointSpec(name="anthropic", preset="anthropic", api_key="sk-ant"),
            _EndpointSpec(name="gemini", preset="google_ai", api_key="AIza"),
        ],
        assignments=[
            _AssignmentSpec(
                consumer="qa_agent",
                endpoint="anthropic",
                model="claude-sonnet-4-6",
                fallback_endpoint="gemini",
            )
        ],
    )
    stores = _stores()
    await diff_and_apply(config, stores, apply=True)

    # Verify the persisted Assignment carries a fallback_endpoint_id (UUID,
    # not the YAML name).
    from beever_atlas.llm.assignments import AssignmentStore

    assignment = await AssignmentStore(stores.mongodb).get("qa_agent")
    assert assignment is not None
    assert assignment.fallback_endpoint_id is not None
    assert assignment.fallback_endpoint_id != "gemini"  # resolved to UUID
