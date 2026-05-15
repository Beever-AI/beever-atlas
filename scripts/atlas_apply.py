"""Apply a declarative Endpoint + Assignment configuration.

Supports two install modes from the proposal:

* **Mode B — env JSON envelope**: ``BEEVER_ENDPOINTS='[...]'`` +
  ``BEEVER_PRESET=<key>`` for Docker/CI/Helm.
* **Mode C — declarative YAML**: ``atlas.yaml`` (or ``--config <path>``)
  with ``endpoints[]`` + ``assignments{}`` + optional ``preset``.

Both produce the same writes against the ``endpoints`` and
``llm_assignments`` collections. Subcommands:

* ``plan`` — print the diff without writing.
* ``apply`` — write atomically; idempotent (re-run with no changes → no writes).

Usage::

    python -m scripts.atlas_apply plan
    python -m scripts.atlas_apply apply --config atlas.yaml
    BEEVER_ENDPOINTS='[{"name":"oai","preset":"openai","api_key":"sk-..."}]' \\
        BEEVER_PRESET=openai-quality python -m scripts.atlas_apply apply

See ``openspec/changes/agent-llm-provider-pluggable/specs/ai-installer/spec.md``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ─── Config schema ────────────────────────────────────────────────────────


@dataclass
class _EndpointSpec:
    name: str
    preset: str
    api_key: str | None = None
    base_url: str = ""
    auth_type: str = "api_key"
    models: list[str] | None = None
    rpm: int | None = None
    headers: dict[str, str] | None = None
    tags: list[str] | None = None
    # AWS IAM
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str | None = None
    # Vertex SA
    google_sa_json: str | None = None


@dataclass
class _AssignmentSpec:
    consumer: str
    endpoint: str  # endpoint name (resolved to id at apply time)
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: Literal["text", "json"] | None = None
    fallback_endpoint: str | None = None
    extra_headers: dict[str, str] | None = None
    dimensions: int | None = None
    task: str | None = None


@dataclass
class _Config:
    endpoints: list[_EndpointSpec]
    assignments: list[_AssignmentSpec]
    preset: str | None = None


# ─── Loaders ─────────────────────────────────────────────────────────────


_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _interpolate(value: str) -> str:
    """Replace ``${VAR}`` references with the env value, or empty string."""

    def replace(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), "")

    return _VAR_PATTERN.sub(replace, value)


def _coerce_endpoint(raw: dict[str, Any]) -> _EndpointSpec:
    interp = {
        k: (_interpolate(v) if isinstance(v, str) else v) for k, v in raw.items()
    }
    return _EndpointSpec(
        name=interp.get("name") or interp.get("preset") or "",
        preset=interp.get("preset") or "custom",
        api_key=interp.get("api_key"),
        base_url=interp.get("base_url", ""),
        auth_type=interp.get("auth_type", "api_key"),
        models=list(interp.get("models") or []),
        rpm=interp.get("rpm"),
        headers=dict(interp.get("headers") or {}),
        tags=list(interp.get("tags") or []),
        aws_access_key_id=interp.get("aws_access_key_id"),
        aws_secret_access_key=interp.get("aws_secret_access_key"),
        aws_region=interp.get("aws_region"),
        google_sa_json=interp.get("google_sa_json"),
    )


def _coerce_assignment(consumer: str, raw: dict[str, Any]) -> _AssignmentSpec:
    interp = {
        k: (_interpolate(v) if isinstance(v, str) else v) for k, v in raw.items()
    }
    return _AssignmentSpec(
        consumer=consumer,
        endpoint=interp.get("endpoint") or "",
        model=interp.get("model") or "",
        temperature=interp.get("temperature"),
        max_tokens=interp.get("max_tokens"),
        response_format=interp.get("response_format"),
        fallback_endpoint=interp.get("fallback_endpoint"),
        extra_headers=dict(interp.get("extra_headers") or {}),
        dimensions=interp.get("dimensions"),
        task=interp.get("task"),
    )


def load_config_from_env() -> _Config | None:
    """Build a ``_Config`` from ``BEEVER_ENDPOINTS`` + ``BEEVER_PRESET`` envs.

    Returns ``None`` when ``BEEVER_ENDPOINTS`` is unset (caller falls
    through to YAML).
    """
    raw = os.environ.get("BEEVER_ENDPOINTS")
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"BEEVER_ENDPOINTS is not valid JSON: {exc}") from exc
    if not isinstance(items, list):
        raise ValueError("BEEVER_ENDPOINTS must be a JSON array")
    return _Config(
        endpoints=[_coerce_endpoint(item) for item in items],
        assignments=[],
        preset=os.environ.get("BEEVER_PRESET"),
    )


def load_config_from_yaml(path: Path) -> _Config:
    """Parse an ``atlas.yaml`` file. Supports ``${VAR}`` interpolation."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML not available — install with 'uv pip install pyyaml' or use BEEVER_ENDPOINTS env mode"
        ) from exc

    text = path.read_text()
    data = yaml.safe_load(text) or {}
    endpoints_raw = data.get("endpoints") or []
    assignments_raw = data.get("assignments") or {}
    endpoints = [_coerce_endpoint(item) for item in endpoints_raw]
    assignments = [
        _coerce_assignment(consumer, raw or {})
        for consumer, raw in assignments_raw.items()
    ]
    return _Config(
        endpoints=endpoints,
        assignments=assignments,
        preset=data.get("preset"),
    )


# ─── Diff + apply ────────────────────────────────────────────────────────


@dataclass
class _DiffEntry:
    action: Literal["create", "update", "unchanged"]
    kind: Literal["endpoint", "assignment"]
    name: str


async def diff_and_apply(
    config: _Config,
    stores: Any,
    *,
    apply: bool,
) -> list[_DiffEntry]:
    """Compute the diff between ``config`` and the current state. Apply when
    ``apply=True``; preview-only otherwise.
    """
    from beever_atlas.llm.assignments import Assignment, AssignmentStore
    from beever_atlas.llm.endpoints import AuthType, EndpointStore
    from beever_atlas.llm.presets import (
        PresetRequirementsNotMet,
        apply_preset as build_preset_assignments,
    )

    ep_store = EndpointStore(stores.mongodb)
    asn_store = AssignmentStore(stores.mongodb)
    diff: list[_DiffEntry] = []

    # ── Endpoints by name (name is the natural key in YAML) ─────────
    existing_by_name = {e.name: e for e in await ep_store.list()}
    for spec in config.endpoints:
        existing = existing_by_name.get(spec.name)
        if existing is None:
            diff.append(_DiffEntry(action="create", kind="endpoint", name=spec.name))
            if apply:
                plaintext: Any = spec.api_key
                if spec.auth_type == "aws_iam":
                    plaintext = {
                        "access_key_id": spec.aws_access_key_id or "",
                        "secret_access_key": spec.aws_secret_access_key or "",
                        "region": spec.aws_region or "",
                    }
                elif spec.auth_type == "google_sa":
                    plaintext = {"sa_json": spec.google_sa_json or ""}
                elif spec.auth_type == "none":
                    plaintext = None
                await ep_store.create(
                    name=spec.name,
                    preset=spec.preset,
                    base_url=spec.base_url or _BASE_URL_BY_PRESET.get(spec.preset, ""),
                    auth_type=spec.auth_type,  # type: ignore[arg-type]
                    plaintext_credential=plaintext,
                    models=spec.models or [],
                    rpm=spec.rpm,
                    headers=spec.headers or {},
                    tags=spec.tags or ["atlas-yaml"],
                )
        else:
            # Update — check if rpm / models / headers changed.
            need_update = (
                (spec.rpm is not None and spec.rpm != existing.rpm)
                or (spec.models and set(spec.models) != set(existing.models))
                or (spec.headers and spec.headers != existing.headers)
            )
            if need_update:
                diff.append(_DiffEntry(action="update", kind="endpoint", name=spec.name))
                if apply:
                    await ep_store.update(
                        existing.id,
                        rpm=spec.rpm,
                        models=spec.models,
                        headers=spec.headers,
                    )
            else:
                diff.append(_DiffEntry(action="unchanged", kind="endpoint", name=spec.name))

    # ── Preset application (after endpoints exist) ─────────────────
    if config.preset:
        endpoints_now = await ep_store.list()
        try:
            preset_assignments = build_preset_assignments(config.preset, endpoints_now)
        except PresetRequirementsNotMet as exc:
            raise RuntimeError(
                f"preset {config.preset!r} requirements not met: required={exc.required}, "
                f"present={exc.present}"
            ) from exc
        # Preset Assignments are applied UNLESS the YAML explicitly assigns the
        # same consumer (explicit > preset).
        explicit_consumers = {a.consumer for a in config.assignments}
        for consumer, proposed in preset_assignments.items():
            if consumer in explicit_consumers:
                continue
            existing = await asn_store.get(consumer)
            if existing is None or existing.endpoint_id != proposed.endpoint_id:
                diff.append(
                    _DiffEntry(action="create", kind="assignment", name=consumer)
                )
                if apply:
                    await asn_store.upsert(proposed)
            else:
                diff.append(
                    _DiffEntry(action="unchanged", kind="assignment", name=consumer)
                )

    # ── Explicit Assignments (YAML-declared) ───────────────────────
    endpoints_by_name = {e.name: e for e in await ep_store.list()}
    for spec in config.assignments:
        target_endpoint = endpoints_by_name.get(spec.endpoint)
        if target_endpoint is None:
            raise RuntimeError(
                f"assignment for consumer {spec.consumer!r} references unknown endpoint "
                f"{spec.endpoint!r}"
            )
        fallback_id: str | None = None
        if spec.fallback_endpoint:
            fb = endpoints_by_name.get(spec.fallback_endpoint)
            if fb is None:
                raise RuntimeError(
                    f"assignment {spec.consumer!r} fallback_endpoint "
                    f"{spec.fallback_endpoint!r} not found"
                )
            fallback_id = fb.id

        existing = await asn_store.get(spec.consumer)
        proposed = Assignment(
            consumer=spec.consumer,
            endpoint_id=target_endpoint.id,
            model=spec.model,
            temperature=spec.temperature,
            max_tokens=spec.max_tokens,
            response_format=spec.response_format,
            fallback_endpoint_id=fallback_id,
            extra_headers=spec.extra_headers or {},
            dimensions=spec.dimensions,
            task=spec.task,
        )
        if existing is None:
            diff.append(_DiffEntry(action="create", kind="assignment", name=spec.consumer))
            if apply:
                await asn_store.upsert(proposed)
        else:
            same = (
                existing.endpoint_id == proposed.endpoint_id
                and existing.model == proposed.model
                and existing.temperature == proposed.temperature
                and existing.max_tokens == proposed.max_tokens
                and existing.response_format == proposed.response_format
                and existing.fallback_endpoint_id == proposed.fallback_endpoint_id
            )
            if same:
                diff.append(
                    _DiffEntry(action="unchanged", kind="assignment", name=spec.consumer)
                )
            else:
                diff.append(
                    _DiffEntry(action="update", kind="assignment", name=spec.consumer)
                )
                if apply:
                    await asn_store.upsert(proposed)

    return diff


# Single source of truth lives in ``llm/presets.py`` (derived from ENDPOINT_PRESETS).
from beever_atlas.llm.presets import BASE_URL_BY_PRESET as _BASE_URL_BY_PRESET


def format_diff(diff: list[_DiffEntry]) -> str:
    """Human-readable diff summary."""
    lines: list[str] = []
    for entry in diff:
        symbol = {"create": "+", "update": "~", "unchanged": " "}[entry.action]
        lines.append(f"  {symbol} {entry.kind:11} {entry.name}")
    counts: dict[str, int] = {}
    for entry in diff:
        counts[entry.action] = counts.get(entry.action, 0) + 1
    summary = ", ".join(f"{c} {k}" for k, c in sorted(counts.items()))
    lines.append("")
    lines.append(f"Plan: {summary or 'no changes'}")
    return "\n".join(lines)


# ─── CLI entry point ─────────────────────────────────────────────────────


async def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Apply Endpoint + Assignment config")
    parser.add_argument("command", choices=["plan", "apply"])
    parser.add_argument("--config", default="atlas.yaml", help="Path to atlas.yaml")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    # Mode B: env JSON envelope wins when set; Mode C: fall back to YAML.
    config = load_config_from_env()
    if config is None:
        config_path = Path(args.config)
        if not config_path.exists():
            print(
                f"error: neither BEEVER_ENDPOINTS env nor {config_path} found",
                flush=True,
            )
            return 2
        config = load_config_from_yaml(config_path)

    from beever_atlas.infra.config import get_settings
    from beever_atlas.stores import StoreClients

    settings = get_settings()
    stores = StoreClients.from_settings(settings)
    await stores.startup()
    try:
        diff = await diff_and_apply(config, stores, apply=(args.command == "apply"))
        if not args.quiet:
            print(format_diff(diff))
            if args.command == "apply":
                print()
                print(f"Applied {args.command} successfully.")
    finally:
        await stores.shutdown()
    return 0


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(asyncio.run(main()))


__all__ = [
    "diff_and_apply",
    "format_diff",
    "load_config_from_env",
    "load_config_from_yaml",
    "main",
]
