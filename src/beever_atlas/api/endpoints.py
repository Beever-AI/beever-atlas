"""REST endpoints for the Endpoint catalog (``/api/settings/endpoints/*``).

Mirrors the shape of ``api/embedding_settings.py`` (masked-only on GET,
encrypted-on-PUT, never-persists-on-test). See
``openspec/changes/agent-llm-provider-pluggable/specs/endpoint-catalog/spec.md``
for the full contract.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from beever_atlas.llm.agent_credentials import set_runtime_credential
from beever_atlas.llm.endpoints import (
    AuthType,
    Endpoint,
    EndpointStore,
    _redact_credential_fragments,
    decrypt_endpoint_credential,
    discover_models,
)
from beever_atlas.llm.model_resolver import SUPPORTED_PROVIDERS
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/endpoints", tags=["endpoints"])


# ─── Request / response models ─────────────────────────────────────────────


class EndpointResponse(BaseModel):
    id: str
    name: str
    preset: str
    base_url: str
    auth_type: AuthType
    has_credential: bool
    credential_masked: str
    models: list[str]
    rpm: int
    headers: dict[str, str]
    tags: list[str]
    last_test_at: str | None = None
    last_test_ok: bool | None = None
    last_test_error: str | None = None
    created_at: str
    updated_at: str


class EndpointListResponse(BaseModel):
    endpoints: list[EndpointResponse]


class CreateEndpointRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    preset: str
    base_url: str = ""
    auth_type: AuthType = "api_key"
    api_key: str | None = None
    # AWS IAM payload — only honoured when auth_type=aws_iam.
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str | None = None
    # Google SA JSON blob — only honoured when auth_type=google_sa.
    google_sa_json: str | None = None
    models: list[str] = Field(default_factory=list)
    rpm: int | None = Field(default=None, ge=1, le=10000)
    headers: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class UpdateEndpointRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    auth_type: AuthType | None = None
    api_key: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str | None = None
    google_sa_json: str | None = None
    models: list[str] | None = None
    rpm: int | None = Field(default=None, ge=1, le=10000)
    headers: dict[str, str] | None = None
    tags: list[str] | None = None


class TestConnectionResponse(BaseModel):
    ok: bool
    latency_ms: int | None = None
    error: str | None = None


class DiscoverModelsResponse(BaseModel):
    ok: bool
    models: list[str] = Field(default_factory=list)
    error: str | None = None


# ─── Helpers ────────────────────────────────────────────────────────────────


def _mask_credential(envelope: dict[str, str] | None) -> tuple[bool, str]:
    """Decrypt the envelope just to mask — never return plaintext."""
    if not envelope:
        return False, ""
    try:
        decrypted = decrypt_endpoint_credential(envelope)
    except Exception:  # noqa: BLE001
        return True, "***"
    if isinstance(decrypted, str):
        # 16 chars is the shortest real API-key format across supported
        # providers; below that, showing 8 of N characters leaks too much.
        if len(decrypted) < 16:
            return True, "***"
        return True, f"{decrypted[:4]}...{decrypted[-4:]}"
    if isinstance(decrypted, dict):
        # IAM / SA — surface a generic hint.
        return True, "***"
    return False, ""


def _endpoint_to_response(endpoint: Endpoint) -> EndpointResponse:
    has_cred, masked = _mask_credential(endpoint.encrypted_key)
    return EndpointResponse(
        id=endpoint.id,
        name=endpoint.name,
        preset=endpoint.preset,
        base_url=endpoint.base_url,
        auth_type=endpoint.auth_type,
        has_credential=has_cred,
        credential_masked=masked,
        models=endpoint.models,
        rpm=endpoint.rpm,
        headers=endpoint.headers,
        tags=endpoint.tags,
        last_test_at=endpoint.last_test_at,
        last_test_ok=endpoint.last_test_ok,
        last_test_error=endpoint.last_test_error,
        created_at=endpoint.created_at,
        updated_at=endpoint.updated_at,
    )


def _build_plaintext_credential(
    auth_type: AuthType,
    api_key: str | None,
    aws_access_key_id: str | None,
    aws_secret_access_key: str | None,
    aws_region: str | None,
    google_sa_json: str | None,
) -> dict[str, str] | str | None:
    """Pack form fields into the credential plaintext the store will encrypt."""
    if auth_type == "none":
        return None
    if auth_type == "api_key":
        return api_key  # may be None — caller decides whether to error
    if auth_type == "aws_iam":
        if not (aws_access_key_id and aws_secret_access_key and aws_region):
            return None
        return {
            "access_key_id": aws_access_key_id,
            "secret_access_key": aws_secret_access_key,
            "region": aws_region,
        }
    if auth_type == "google_sa":
        if not google_sa_json:
            return None
        return {"sa_json": google_sa_json}
    return None


def _store() -> EndpointStore:
    return EndpointStore(get_stores().mongodb)


# ─── Endpoints ──────────────────────────────────────────────────────────────


@router.get("", response_model=EndpointListResponse)
async def list_endpoints() -> EndpointListResponse:
    """Enumerate every Endpoint. Plaintext credentials NEVER appear."""
    endpoints = await _store().list()
    return EndpointListResponse(endpoints=[_endpoint_to_response(e) for e in endpoints])


@router.get("/{endpoint_id}", response_model=EndpointResponse)
async def get_endpoint(endpoint_id: str) -> EndpointResponse:
    endpoint = await _store().get(endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail={"error": "endpoint_not_found"})
    return _endpoint_to_response(endpoint)


@router.post("", response_model=EndpointResponse, status_code=201)
async def create_endpoint(req: CreateEndpointRequest) -> EndpointResponse:
    """Insert a new Endpoint document. Validates provider against ``SUPPORTED_PROVIDERS``."""
    if (
        req.preset != "custom"
        and req.preset not in SUPPORTED_PROVIDERS
        and req.preset
        not in ("google_ai", "ollama", "vllm", "lmstudio", "openrouter", "litellm_proxy")
    ):
        # Allow the established preset keys (which differ from LiteLLM prefixes
        # in a few places — ``google_ai`` resolves to ``gemini/`` at dispatch
        # time, ``ollama`` covers both embedding + chat).
        raise HTTPException(
            status_code=422,
            detail={"error": "unsupported_preset", "supported": list(SUPPORTED_PROVIDERS)},
        )
    if req.auth_type == "oauth":
        raise HTTPException(
            status_code=501,
            detail={
                "error": "oauth_not_yet_supported",
                "message": (
                    "OAuth-based endpoints are reserved for a future release. Use api_key for now."
                ),
            },
        )

    plaintext = _build_plaintext_credential(
        req.auth_type,
        req.api_key,
        req.aws_access_key_id,
        req.aws_secret_access_key,
        req.aws_region,
        req.google_sa_json,
    )

    store = _store()
    try:
        endpoint = await store.create(
            name=req.name,
            preset=req.preset,
            base_url=req.base_url,
            auth_type=req.auth_type,
            plaintext_credential=plaintext,
            models=req.models,
            rpm=req.rpm,
            headers=req.headers,
            tags=req.tags,
        )
    except RuntimeError as exc:
        # ``CredentialEncryptor`` raises when ``CREDENTIAL_MASTER_KEY`` is unset.
        # Don't echo the internal error string (it names the env var + the
        # generation command) — log it server-side, return a generic note.
        logger.error("endpoint create: credential encryptor unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "credential_encryptor_unavailable",
                "message": "Credential encryption is not configured — contact your administrator.",
            },
        ) from exc

    # Hot-reload the runtime cache so dispatch sees the new credential.
    if plaintext is not None:
        decrypted = decrypt_endpoint_credential(endpoint.encrypted_key or {})
        set_runtime_credential(endpoint.id, decrypted)

    return _endpoint_to_response(endpoint)


@router.put("/{endpoint_id}", response_model=EndpointResponse)
async def update_endpoint(endpoint_id: str, req: UpdateEndpointRequest) -> EndpointResponse:
    existing = await _store().get(endpoint_id)
    if existing is None:
        raise HTTPException(status_code=404, detail={"error": "endpoint_not_found"})

    # Compute the credential update — only when any credential field is supplied.
    plaintext: dict[str, str] | str | None | object = ...
    if req.auth_type is not None or any(
        v is not None
        for v in (
            req.api_key,
            req.aws_access_key_id,
            req.aws_secret_access_key,
            req.aws_region,
            req.google_sa_json,
        )
    ):
        target_auth = req.auth_type or existing.auth_type
        plaintext = _build_plaintext_credential(
            target_auth,
            req.api_key,
            req.aws_access_key_id,
            req.aws_secret_access_key,
            req.aws_region,
            req.google_sa_json,
        )

    try:
        updated = await _store().update(
            endpoint_id,
            name=req.name,
            base_url=req.base_url,
            auth_type=req.auth_type,
            plaintext_credential=plaintext,
            models=req.models,
            rpm=req.rpm,
            headers=req.headers,
            tags=req.tags,
        )
    except RuntimeError as exc:
        logger.error("endpoint update: credential encryptor unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "credential_encryptor_unavailable",
                "message": "Credential encryption is not configured — contact your administrator.",
            },
        ) from exc

    if updated is None:
        raise HTTPException(status_code=404, detail={"error": "endpoint_not_found"})

    # Hot-reload the runtime cache.
    if plaintext is not ...:
        if plaintext is None:
            set_runtime_credential(endpoint_id, None)
        elif updated.encrypted_key is not None:
            set_runtime_credential(endpoint_id, decrypt_endpoint_credential(updated.encrypted_key))

    return _endpoint_to_response(updated)


@router.delete("/{endpoint_id}", status_code=204)
async def delete_endpoint(endpoint_id: str) -> None:
    """Delete an Endpoint. Returns 409 if any Assignment references it
    (as primary OR fallback)."""
    from beever_atlas.llm.assignments import AssignmentStore

    asn_store = AssignmentStore(get_stores().mongodb)
    references = await asn_store.list_referencing_endpoint(endpoint_id)
    if references:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "endpoint_in_use_as_primary_or_fallback",
                "consumers": [a.consumer for a in references],
            },
        )

    deleted = await _store().delete(endpoint_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"error": "endpoint_not_found"})

    set_runtime_credential(endpoint_id, None)


@router.post("/{endpoint_id}/test", response_model=TestConnectionResponse)
async def test_endpoint(endpoint_id: str) -> TestConnectionResponse:
    """Probe-completion using the Endpoint's persisted credentials.

    Issues a 1-token completion against the first model in ``endpoint.models``
    via ``dispatch_completion``. Never persists anything except the
    ``last_test_*`` stamps on the Endpoint document.
    """
    import time

    from beever_atlas.llm.agent_credentials import get_runtime_credential
    from beever_atlas.services.llm_dispatch import (
        dispatch_completion,
        normalize_litellm_model,
        sniff_provider,
    )

    endpoint = await _store().get(endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail={"error": "endpoint_not_found"})

    if not endpoint.models:
        return TestConnectionResponse(
            ok=False, error="endpoint_has_no_models: configure at least one model before testing"
        )

    test_model = endpoint.models[0]
    provider = sniff_provider(test_model)
    full_model = normalize_litellm_model(
        test_model if "/" in test_model else f"{endpoint.preset}/{test_model}"
    )

    # Pull credentials from runtime cache (populated at boot or via PUT hot-reload).
    credential = get_runtime_credential(endpoint_id)
    extra_kwargs: dict[str, Any] = {}
    if isinstance(credential, str):
        extra_kwargs["api_key"] = credential
    if endpoint.base_url:
        extra_kwargs["api_base"] = endpoint.base_url
        # Opt-in SSRF guard — refuse private/link-local/metadata targets before
        # we attach the credential and probe. Off by default (local presets
        # legitimately point at localhost); see ``llm_endpoint_ssrf_guard``.
        from beever_atlas.infra.config import get_settings

        if get_settings().llm_endpoint_ssrf_guard:
            from beever_atlas.infra.http_safe import resolve_and_validate

            try:
                resolve_and_validate(endpoint.base_url)
            except (ValueError, PermissionError) as exc:
                msg = f"base_url_blocked: {exc}"
                await _store().record_test_result(endpoint_id, ok=False, error=msg)
                return TestConnectionResponse(ok=False, error=msg)

    started = time.monotonic()
    try:
        await dispatch_completion(
            provider=provider,
            model=full_model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
            **extra_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        # Sanitise the error before returning it — some LiteLLM SDK exceptions
        # embed the full request kwargs (including ``api_key``) in their repr.
        # When the message looks like it could carry credential fragments, drop
        # the body and return only the exception class + a generic note.
        raw = _redact_credential_fragments(str(exc)[:200])
        error_msg = f"{type(exc).__name__}: {raw}"
        await _store().record_test_result(endpoint_id, ok=False, error=error_msg)
        return TestConnectionResponse(ok=False, error=error_msg)

    latency_ms = int((time.monotonic() - started) * 1000)
    await _store().record_test_result(endpoint_id, ok=True)
    return TestConnectionResponse(ok=True, latency_ms=latency_ms)


@router.post("/{endpoint_id}/discover", response_model=DiscoverModelsResponse)
async def discover_endpoint_models(endpoint_id: str) -> DiscoverModelsResponse:
    """Issue the preset-specific discovery request and return the model list."""
    from beever_atlas.llm.agent_credentials import get_runtime_credential

    endpoint = await _store().get(endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail={"error": "endpoint_not_found"})

    credential = get_runtime_credential(endpoint_id)
    plaintext = credential if isinstance(credential, str) else None

    result = await discover_models(endpoint, plaintext_credential=plaintext)
    return DiscoverModelsResponse(
        ok=bool(result.get("ok")),
        models=list(result.get("models") or []),
        error=result.get("error"),
    )
