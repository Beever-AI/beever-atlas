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
    EndpointRole,
    EndpointStore,
    PersistedModelKind,
    _redact_credential_fragments,
    decrypt_endpoint_credential,
    discover_models,
    preset_to_provider,
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
    # PR-α: per-model classification surface.
    model_kinds: dict[str, PersistedModelKind] = Field(default_factory=dict)
    advanced_models: list[str] = Field(default_factory=list)
    manually_kept: list[str] = Field(default_factory=list)
    role: EndpointRole = "auto"


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
    # PR-α: per-kind buckets + dropped-category counts. ``by_kind`` is the
    # kept ids split into chat / embedding; ``dropped_breakdown`` is the
    # count of ids per classifier-dropped category (reranker, image_gen, …)
    # so the UI can render a "filtered N (Y rerankers, Z image-gen…)" hint.
    # Full dropped-id lists live in ``Endpoint.advanced_models`` after the
    # response persists.
    by_kind: dict[str, list[str]] = Field(default_factory=dict)
    dropped_breakdown: dict[str, int] = Field(default_factory=dict)


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
        model_kinds=endpoint.model_kinds,
        advanced_models=endpoint.advanced_models,
        manually_kept=endpoint.manually_kept,
        role=endpoint.role,
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
        not in (
            "google_ai",
            "ollama",
            "vllm",
            "lmstudio",
            "openrouter",
            "litellm_proxy",
            # Embedding-only providers — LiteLLM exposes them under aembedding,
            # not acompletion, so they aren't in SUPPORTED_PROVIDERS (the chat-
            # completion allowlist) but the UI legitimately offers them as
            # "+ Add embedding endpoint" options.
            "jina_ai",
            "voyage",
        )
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


# Presets whose providers expose ONLY an embeddings endpoint (no chat completion).
# Probing these via ``litellm.acompletion`` always 400s — route to
# ``litellm.aembedding`` instead. Mirrors ``presets.py`` ``embedding_only=True``
# and the frontend's ``EMBEDDING_CAPABLE_PRESETS`` (which is the union;
# embedding-ONLY is a strict subset).
_EMBEDDING_ONLY_PRESETS: frozenset[str] = frozenset({"jina_ai", "voyage", "cohere"})


# Probe timeout for Test Connection (seconds). LiteLLM's openai SDK defaults
# to ~600s for connect+read — far too long for a UI-triggered probe. 15s is
# enough for any healthy endpoint to round-trip a 1-token completion AND for
# Ollama to surface a connect-refused / cold-load failure quickly.
_PROBE_TIMEOUT_SECONDS: float = 15.0

# Ollama presets whose ``localhost`` base_url must be rewritten to ``127.0.0.1``
# at probe time. See :func:`_rewrite_ollama_localhost`.
_OLLAMA_PRESETS: frozenset[str] = frozenset({"ollama", "ollama_chat"})


def _rewrite_ollama_localhost(preset: str, base_url: str) -> str:
    """Rewrite ``localhost`` → ``127.0.0.1`` for Ollama-preset probe URLs.

    macOS resolves ``localhost`` to ``::1`` (IPv6) first; Ollama binds to
    IPv4 ``127.0.0.1`` by default, so the first connect attempt waits the
    kernel's IPv6 TCP timeout (~75s on macOS) before falling back. The
    pragmatic fix is to rewrite the host at probe-build time for Ollama
    endpoints only — it's the user's loopback either way. This is a
    documented Ollama+macOS pitfall (cloud providers like ``api.openai.com``
    handle IPv6 fine, so the rewrite stays scoped to Ollama). The stored
    Endpoint document is NOT mutated; only the value forwarded to LiteLLM.
    """
    if preset not in _OLLAMA_PRESETS or not base_url:
        return base_url
    # Match http(s)://localhost[:port] or http(s)://localhost/path forms.
    # Use urlparse for correctness; avoid a naive string replace which would
    # also corrupt e.g. ``/path/localhost-thing``.
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(base_url)
    if (parsed.hostname or "").lower() != "localhost":
        return base_url
    new_netloc = "127.0.0.1"
    if parsed.port is not None:
        new_netloc = f"127.0.0.1:{parsed.port}"
    return urlunparse(parsed._replace(netloc=new_netloc))


def _friendly_probe_error(exc: BaseException, api_base: str | None) -> str | None:
    """Return a friendly error message for known probe failure modes.

    Returns ``None`` for unrecognised exceptions — caller falls back to the
    generic ``<ExceptionType>: <redacted str>`` shape. Returned strings are
    crafted to NOT contain any ``_CREDENTIAL_MARKERS`` substring so the
    redactor leaves them intact.
    """
    import asyncio

    try:
        import litellm  # type: ignore[import-untyped]
    except Exception:  # noqa: BLE001
        litellm = None  # type: ignore[assignment]

    try:
        import httpx
    except Exception:  # noqa: BLE001
        httpx = None  # type: ignore[assignment]

    # Timeout first — ``litellm.Timeout`` subclasses ``litellm.APIConnectionError``.
    timeout_types: list[type[BaseException]] = [asyncio.TimeoutError, TimeoutError]
    if litellm is not None:
        timeout_types.append(litellm.Timeout)
    if httpx is not None:
        timeout_types.append(httpx.TimeoutException)
    if isinstance(exc, tuple(timeout_types)):
        return f"Request timed out after {int(_PROBE_TIMEOUT_SECONDS)}s"

    # Connection refused / unreachable — LiteLLM wraps these in APIConnectionError;
    # httpx's raw form is ConnectError. The message is intentionally framed
    # around the service rather than the URL to avoid touching marker words.
    connect_types: list[type[BaseException]] = []
    if litellm is not None:
        connect_types.append(litellm.APIConnectionError)
    if httpx is not None:
        connect_types.extend([httpx.ConnectError, httpx.ConnectTimeout])
    if connect_types and isinstance(exc, tuple(connect_types)):
        where = f" at {api_base}" if api_base else ""
        return f"Connection refused{where} - is the service running?"
    return None


def _build_probe_model(endpoint: Endpoint) -> tuple[str, str, bool]:
    """Return ``(litellm_provider, model_id, drop_base_url)`` for a probe.

    Thin wrapper around :func:`route_for_endpoint` — the single source of truth
    for ``(preset, base_url, model)`` → ``(provider, litellm_model)`` resolution
    shared between the Test Connection probe and ``LLMProvider.resolve_for_call``.
    Both paths must agree or operators get "Test passes / dispatch 404s".
    """
    from beever_atlas.services.llm_dispatch import route_for_endpoint

    return route_for_endpoint(endpoint.preset, endpoint.base_url, endpoint.models[0])


@router.post("/{endpoint_id}/test", response_model=TestConnectionResponse)
async def test_endpoint(endpoint_id: str) -> TestConnectionResponse:
    """Probe the Endpoint with its persisted credentials.

    Issues a 1-token completion (or single embedding for embedding-only presets)
    against the first model in ``endpoint.models``. Never persists anything
    except the ``last_test_*`` stamps on the Endpoint document.
    """
    import time

    from beever_atlas.llm.agent_credentials import get_runtime_credential
    from beever_atlas.services.llm_dispatch import (
        dispatch_completion,
        dispatch_embedding,
    )

    endpoint = await _store().get(endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail={"error": "endpoint_not_found"})

    if not endpoint.models:
        return TestConnectionResponse(
            ok=False, error="endpoint_has_no_models: configure at least one model before testing"
        )

    is_embedding_only = endpoint.preset in _EMBEDDING_ONLY_PRESETS
    if is_embedding_only:
        # Embedding-only presets aren't routed through ``route_for_endpoint``
        # (it's chat-only — would raise ValueError). Probe-side they still use
        # the legacy provider prefix + ``preset/<model>`` shape that LiteLLM's
        # ``aembedding`` accepts.
        raw_model = endpoint.models[0]
        provider = preset_to_provider(endpoint.preset)
        full_model = (
            raw_model if "/" in raw_model else f"{provider}/{raw_model.removeprefix('models/')}"
        )
        drop_base_url = False
    else:
        provider, full_model, drop_base_url = _build_probe_model(endpoint)

    # Pull credentials from runtime cache (populated at boot or via PUT hot-reload).
    credential = get_runtime_credential(endpoint_id)
    extra_kwargs: dict[str, Any] = {}
    if isinstance(credential, str):
        extra_kwargs["api_key"] = credential
    elif endpoint.auth_type == "none" and provider == "openai":
        # LiteLLM's ``openai`` provider rejects a missing api_key client-side
        # even when the upstream server doesn't validate it (local Ollama /
        # vLLM / LM Studio). Pass a harmless placeholder — the server ignores
        # it. Without this, ``auth_type=none`` + OpenAI-compat routing 400s
        # at the SDK boundary before the request leaves the process.
        extra_kwargs["api_key"] = "placeholder-no-auth"
    if endpoint.base_url and not drop_base_url:
        # Rewrite ``localhost`` → ``127.0.0.1`` for Ollama presets to dodge the
        # macOS IPv6-first / Ollama-binds-IPv4 ~75s connect stall. Scoped to
        # Ollama only — cloud providers handle IPv6 fine. Stored Endpoint is
        # not mutated; only the value forwarded to LiteLLM.
        extra_kwargs["api_base"] = _rewrite_ollama_localhost(
            endpoint.preset, endpoint.base_url
        )
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
        if is_embedding_only:
            # Embedding-only providers (Jina, Voyage, Cohere) have no chat
            # completion route — LiteLLM 400s with ``LiteLLMUnknownProvider``
            # the moment we try ``acompletion`` against them. Probe via the
            # embedding path instead.
            await dispatch_embedding(
                provider=provider,
                model=full_model,
                input=["test"],
                timeout=_PROBE_TIMEOUT_SECONDS,
                **extra_kwargs,
            )
        else:
            await dispatch_completion(
                provider=provider,
                model=full_model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
                timeout=_PROBE_TIMEOUT_SECONDS,
                **extra_kwargs,
            )
    except Exception as exc:  # noqa: BLE001
        # Friendly path first — turn known transport failures (timeout,
        # connection refused) into operator-readable messages. These strings
        # are crafted to avoid any ``_CREDENTIAL_MARKERS`` substring so the
        # redactor below leaves them intact.
        api_base = extra_kwargs.get("api_base")
        friendly = _friendly_probe_error(exc, api_base if isinstance(api_base, str) else None)
        if friendly is not None:
            error_msg = friendly
        else:
            # Sanitise the error before returning it — some LiteLLM SDK
            # exceptions embed the full request kwargs (including ``api_key``)
            # in their repr. When the message looks like it could carry
            # credential fragments, drop the body and return only the
            # exception class + a generic note.
            raw = _redact_credential_fragments(str(exc)[:200])
            error_msg = f"{type(exc).__name__}: {raw}"
        await _store().record_test_result(endpoint_id, ok=False, error=error_msg)
        return TestConnectionResponse(ok=False, error=error_msg)

    latency_ms = int((time.monotonic() - started) * 1000)
    await _store().record_test_result(endpoint_id, ok=True)
    return TestConnectionResponse(ok=True, latency_ms=latency_ms)


@router.post("/{endpoint_id}/discover", response_model=DiscoverModelsResponse)
async def discover_endpoint_models(endpoint_id: str) -> DiscoverModelsResponse:
    """Issue the preset-specific discovery request, classify each id, and
    persist the bucketed result back onto the Endpoint document.

    Discovery dumps every model the provider serves — including rerankers,
    image-gen, TTS / STT and fine-tunes that Atlas doesn't consume. The
    classifier (see ``llm/model_classifier.py``) splits those into:

      * kept ``models`` = chat + embedding, persisted to ``Endpoint.models``
      * ``advanced_models`` = everything else, surfaced separately in the UI
      * ``model_kinds[id]`` = ``"chat" | "embedding"`` for each kept id

    Models the operator has manually promoted (``Endpoint.manually_kept``)
    always stay in the kept list even when re-Discover would otherwise drop
    them.
    """
    from itertools import chain

    from beever_atlas.llm.agent_credentials import get_runtime_credential

    endpoint = await _store().get(endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail={"error": "endpoint_not_found"})

    credential = get_runtime_credential(endpoint_id)
    plaintext = credential if isinstance(credential, str) else None

    result = await discover_models(endpoint, plaintext_credential=plaintext)
    ok = bool(result.get("ok"))
    error = result.get("error")
    by_kind = dict(result.get("models_by_kind") or {"chat": [], "embedding": []})
    dropped = dict(result.get("dropped") or {})

    if ok:
        chat_ids = list(by_kind.get("chat") or [])
        embedding_ids = list(by_kind.get("embedding") or [])
        kept_set = set(chat_ids) | set(embedding_ids)

        # Pre-existing operator-promoted ids survive re-Discover even when
        # the classifier would drop them. They keep their previous kind if
        # known, otherwise default to "chat".
        manually_kept = list(endpoint.manually_kept)
        prev_kinds = endpoint.model_kinds
        for mid in manually_kept:
            if mid in kept_set:
                continue
            kept_set.add(mid)
            kind = prev_kinds.get(mid)
            if kind == "embedding":
                embedding_ids.append(mid)
            else:
                # No prior kind, or it was a non-chat/embedding category —
                # treat operator's promotion as a chat default. We do NOT
                # re-classify; the operator's intent wins.
                if kind != "chat":
                    chat_ids.append(mid)
                else:
                    chat_ids.append(mid)

        new_model_kinds: dict[str, PersistedModelKind] = {}
        for mid in chat_ids:
            new_model_kinds[mid] = "chat"
        for mid in embedding_ids:
            # Embedding wins if a model somehow ended up in both buckets.
            new_model_kinds[mid] = "embedding"

        kept_sorted = sorted(kept_set)
        manually_kept_set = set(manually_kept)
        advanced = sorted(
            {mid for mid in chain.from_iterable(dropped.values()) if mid not in manually_kept_set}
        )

        await _store().update(
            endpoint_id,
            models=kept_sorted,
            model_kinds=new_model_kinds,
            advanced_models=advanced,
        )

        return DiscoverModelsResponse(
            ok=True,
            models=kept_sorted,
            error=None,
            by_kind={
                "chat": sorted(set(chat_ids)),
                "embedding": sorted(set(embedding_ids)),
            },
            dropped_breakdown={k: len(v) for k, v in dropped.items() if v},
        )

    # Failure path — preserve legacy ``models`` shape (empty list).
    return DiscoverModelsResponse(
        ok=False,
        models=list(result.get("models") or []),
        error=error,
        by_kind={"chat": [], "embedding": []},
        dropped_breakdown={},
    )
