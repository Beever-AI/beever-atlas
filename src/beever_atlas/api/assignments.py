"""REST endpoints for per-consumer LLM assignments.

Routes:
* ``GET  /api/settings/assignments`` — list every Assignment
* ``GET  /api/settings/assignments/{consumer}`` — fetch one
* ``PUT  /api/settings/assignments/{consumer}`` — upsert with capability validation
* ``DELETE /api/settings/assignments/{consumer}`` — clear
* ``POST /api/settings/assignments/preset`` — preview/apply preset (with diff)

See ``openspec/changes/agent-llm-provider-pluggable/specs/assignment-overrides/spec.md``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from beever_atlas.llm.assignments import (
    Assignment,
    AssignmentStore,
    DEFAULT_CONSUMERS,
    ResponseFormat,
)
from beever_atlas.llm.endpoints import EndpointStore, preset_to_provider
from beever_atlas.llm.model_resolver import (
    AGENT_CAPABILITIES,
    suggest_compatible_assignments,
    validate_assignment_compatibility,
)
from beever_atlas.llm.presets import (
    APPLY_PRESETS,
    PresetRequirementsNotMet,
    apply_preset,
)
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/assignments", tags=["assignments"])


# ─── Request / response models ─────────────────────────────────────────────


class AssignmentResponse(BaseModel):
    consumer: str
    endpoint_id: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: ResponseFormat | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    fallback_endpoint_id: str | None = None
    dimensions: int | None = None
    task: str | None = None
    updated_at: str = ""


class AssignmentListResponse(BaseModel):
    assignments: list[AssignmentResponse]
    default_consumers: list[str]
    capabilities: dict[str, list[str]]  # per-consumer required capabilities


class UpdateAssignmentRequest(BaseModel):
    endpoint_id: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=200_000)
    response_format: ResponseFormat | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    fallback_endpoint_id: str | None = None
    dimensions: int | None = None
    task: str | None = None
    # When true, an incompatible model is accepted with a warning rather than 422.
    force: bool = False


class PresetRequest(BaseModel):
    preset: str
    confirm: bool = False  # apply when true; preview when false
    force_overwrite_custom: bool = False  # overwrite Assignments with per-call params


class PresetDiffEntry(BaseModel):
    consumer: str
    before: AssignmentResponse | None
    after: AssignmentResponse


class PresetResponse(BaseModel):
    action: str  # "preview" | "applied"
    diff: list[PresetDiffEntry]
    preserved: list[str] = Field(default_factory=list)


# ─── Helpers ────────────────────────────────────────────────────────────────


def _assignment_to_response(a: Assignment) -> AssignmentResponse:
    return AssignmentResponse(
        consumer=a.consumer,
        endpoint_id=a.endpoint_id,
        model=a.model,
        temperature=a.temperature,
        max_tokens=a.max_tokens,
        response_format=a.response_format,
        extra_headers=a.extra_headers,
        fallback_endpoint_id=a.fallback_endpoint_id,
        dimensions=a.dimensions,
        task=a.task,
        updated_at=a.updated_at,
    )


def _store() -> AssignmentStore:
    return AssignmentStore(get_stores().mongodb)


def _endpoint_store() -> EndpointStore:
    return EndpointStore(get_stores().mongodb)


def _capabilities_payload() -> dict[str, list[str]]:
    """Convert ``AGENT_CAPABILITIES[consumer]`` set → sorted list for JSON."""
    return {consumer: sorted(caps) for consumer, caps in AGENT_CAPABILITIES.items()}


async def _has_custom_params(consumer: str) -> bool:
    """True when the operator has set ANY per-call override on this Assignment.

    Used by the preset-apply path to decide whether to preserve the entry.
    """
    existing = await _store().get(consumer)
    if existing is None:
        return False
    return (
        existing.temperature is not None
        or existing.max_tokens is not None
        or existing.response_format is not None
        or bool(existing.extra_headers)
        or existing.fallback_endpoint_id is not None
    )


# ─── Endpoints ──────────────────────────────────────────────────────────────


@router.get("", response_model=AssignmentListResponse)
async def list_assignments() -> AssignmentListResponse:
    assignments = await _store().list()
    return AssignmentListResponse(
        assignments=[_assignment_to_response(a) for a in assignments],
        default_consumers=list(DEFAULT_CONSUMERS),
        capabilities=_capabilities_payload(),
    )


@router.get("/{consumer}", response_model=AssignmentResponse)
async def get_assignment(consumer: str) -> AssignmentResponse:
    assignment = await _store().get(consumer)
    if assignment is None:
        raise HTTPException(
            status_code=404, detail={"error": "assignment_not_found", "consumer": consumer}
        )
    return _assignment_to_response(assignment)


@router.put("/{consumer}", response_model=AssignmentResponse)
async def upsert_assignment(consumer: str, req: UpdateAssignmentRequest) -> AssignmentResponse:
    """Insert-or-update an Assignment. Validates the consumer name is one of the
    known consumers, the Endpoint exists, the model is in the Endpoint's curated
    list, and the (consumer, model) pair satisfies every required capability."""
    # Reject arbitrary consumer names — only the 16 agents + ``embedding`` are valid.
    if consumer not in DEFAULT_CONSUMERS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unknown_consumer",
                "consumer": consumer,
                "valid": list(DEFAULT_CONSUMERS),
            },
        )
    # Verify Endpoint exists.
    endpoint = await _endpoint_store().get(req.endpoint_id)
    if endpoint is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "endpoint_not_found",
                "endpoint_id": req.endpoint_id,
            },
        )

    # The persisted ``model`` is the bare name (e.g. ``claude-sonnet-4-6``);
    # validation needs the fully-qualified ``provider/model`` id. The
    # Endpoint's preset doubles as the LiteLLM provider for known presets.
    full_model_id = (
        req.model if "/" in req.model else f"{_preset_to_provider(endpoint.preset)}/{req.model}"
    )

    # Capability validation.
    missing = validate_assignment_compatibility(consumer, full_model_id)
    if missing and not req.force:
        # Build suggestions across every existing Endpoint × its curated models.
        all_endpoints = await _endpoint_store().list()
        candidates = [
            (e.id, f"{_preset_to_provider(e.preset)}/{m}") for e in all_endpoints for m in e.models
        ]
        suggested = suggest_compatible_assignments(consumer, candidates, n=3)
        raise HTTPException(
            status_code=422,
            detail={
                "error": "incompatible_assignment",
                "consumer": consumer,
                "model": full_model_id,
                "missing_capabilities": missing,
                "suggested": [{"endpoint_id": ep, "model": m} for ep, m in suggested],
            },
        )

    # Fallback validation.
    if req.fallback_endpoint_id:
        if req.fallback_endpoint_id == req.endpoint_id:
            raise HTTPException(
                status_code=422,
                detail={"error": "fallback_must_differ_from_primary"},
            )
        fallback_ep = await _endpoint_store().get(req.fallback_endpoint_id)
        if fallback_ep is None:
            raise HTTPException(
                status_code=422,
                detail={"error": "fallback_endpoint_not_found"},
            )

    assignment = Assignment(
        consumer=consumer,
        endpoint_id=req.endpoint_id,
        model=req.model,
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        response_format=req.response_format,
        extra_headers=req.extra_headers,
        fallback_endpoint_id=req.fallback_endpoint_id,
        dimensions=req.dimensions,
        task=req.task,
    )
    saved = await _store().upsert(assignment)
    # PR-ν: hot-reload LLMProvider so the new model takes effect on the
    # next agent call — without this, agent code keeps dispatching the
    # previous model until uvicorn restart.
    await _refresh_llm_provider()
    return _assignment_to_response(saved)


@router.delete("/{consumer}", status_code=204)
async def delete_assignment(consumer: str) -> None:
    deleted = await _store().delete(consumer)
    if not deleted:
        raise HTTPException(
            status_code=404, detail={"error": "assignment_not_found", "consumer": consumer}
        )
    # PR-ν: hot-reload so the consumer falls back to the default map
    # immediately, not after the next restart.
    await _refresh_llm_provider()


async def _refresh_llm_provider() -> None:
    """Refresh ``LLMProvider`` agent overrides from the persistent state.

    Pulls from ``llm_assignments`` + legacy ``agent_model_config`` so a
    UI-saved Assignment takes effect on the very next agent call. Also
    invalidates the qa_agent ``_agents`` cache — without that, the cached
    LlmAgent keeps using the previously-resolved model object (built on
    its first request after boot) and an Assignment switch silently has
    no effect at runtime.

    Order matters: clear the agent cache FIRST, then reload provider
    overrides. The reverse order has a brief window where a concurrent
    request can hit ``get_agent_for_mode`` and read the stale cached
    agent built with the previous model. Clearing first guarantees the
    next request rebuilds — at worst it momentarily uses the OLD
    overrides (next reload fixes it), never a stale cached agent.

    Best-effort — never let a hydration failure fail the save.
    """
    try:
        from beever_atlas.agents.query.qa_agent import reset_agent_cache

        reset_agent_cache()
    except Exception:  # noqa: BLE001
        logger.warning(
            "assignments: qa_agent.reset_agent_cache failed (non-fatal)",
            exc_info=True,
        )

    try:
        from beever_atlas.llm.provider import get_llm_provider

        await get_llm_provider().reload_from_db()
    except Exception:  # noqa: BLE001
        logger.warning(
            "assignments: LLMProvider.reload_from_db failed (non-fatal)",
            exc_info=True,
        )


@router.post("/preset", response_model=PresetResponse)
async def apply_preset_handler(req: PresetRequest) -> PresetResponse:
    """Preview or apply a full preset Assignment seed.

    ``confirm: false`` returns a diff without writing; ``confirm: true`` writes
    atomically. Assignments with custom per-call params are preserved unless
    ``force_overwrite_custom`` is true.
    """
    if req.preset not in APPLY_PRESETS:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_preset", "valid": list(APPLY_PRESETS)},
        )

    # Load endpoints to feed apply_preset.
    endpoints = await _endpoint_store().list()
    try:
        new_assignments = apply_preset(req.preset, endpoints)
    except PresetRequirementsNotMet as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "preset_requirements_not_met",
                "required": exc.required,
                "present": exc.present,
            },
        ) from exc

    # Compute the diff against existing Assignments.
    store = _store()
    existing_by_consumer = {a.consumer: a for a in await store.list()}
    diff: list[PresetDiffEntry] = []
    preserved: list[str] = []
    to_write: list[Assignment] = []

    for consumer, proposed in new_assignments.items():
        existing = existing_by_consumer.get(consumer)
        if existing is not None and not req.force_overwrite_custom:
            if await _has_custom_params(consumer):
                preserved.append(consumer)
                continue
        diff.append(
            PresetDiffEntry(
                consumer=consumer,
                before=_assignment_to_response(existing) if existing else None,
                after=_assignment_to_response(proposed),
            )
        )
        to_write.append(proposed)

    if not req.confirm:
        return PresetResponse(action="preview", diff=diff, preserved=preserved)

    # Apply atomically (one upsert per Assignment).
    for assignment in to_write:
        await store.upsert(assignment)

    return PresetResponse(action="applied", diff=diff, preserved=preserved)


# ─── Helpers (private) ────────────────────────────────────────────────────


# Single source of truth for "Endpoint preset key → LiteLLM provider prefix"
# lives in ``llm/endpoints.py``. Re-bound here under the historical private
# name so the existing call sites in this module keep working.
_preset_to_provider = preset_to_provider
