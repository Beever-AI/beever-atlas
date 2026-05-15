"""REST endpoints for the Endpoint catalog (``/api/settings/endpoints/*``).

Mirrors the shape of ``api/embedding_settings.py`` (masked-only on GET,
encrypted-on-PUT, never-persists-on-test). See
``openspec/changes/agent-llm-provider-pluggable/specs/endpoint-catalog/spec.md``
for the full contract.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

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
from beever_atlas.llm.model_classifier import classify_model
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
    # PR-β: soft role hint for the Test Connection probe + model picker.
    # ``None`` ⇒ derive a sensible default from the preset (embedding-only
    # presets get ``"embedding"``; everything else gets ``"auto"``).
    role: EndpointRole | None = None


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
    # PR-β: ``None`` ⇒ leave the field unchanged. Operators can flip the
    # role + curate ``manually_kept`` directly via PUT.
    role: EndpointRole | None = None
    manually_kept: list[str] | None = None


class TestConnectionResponse(BaseModel):
    ok: bool
    latency_ms: int | None = None
    error: str | None = None
    # PR-β: surface the probed model + kind so operators can see what was
    # actually hit. Response-only — not persisted on the Endpoint document.
    probed_model: str | None = None
    probed_kind: Literal["chat", "embedding"] | None = None


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

    # PR-β: derive a sensible role default from the preset when the caller
    # didn't supply one. Embedding-only presets get ``"embedding"`` so the
    # Test probe + picker default the right way; everything else gets
    # ``"auto"`` (model_kinds inference + preset's natural side).
    if req.role is not None:
        role: EndpointRole = req.role
    elif req.preset in _EMBEDDING_ONLY_PRESETS:
        role = "embedding"
    else:
        role = "auto"

    # PR-ε: seed ``models`` + ``model_kinds`` from Atlas's curated catalog
    # for commercial presets when the operator didn't supply any models.
    # Goal: the endpoint lands usable without requiring a Discover click,
    # AND with zero risk of pulling an upstream-only / experimental model
    # that breaks Test or dispatch downstream.
    from beever_atlas.llm.endpoints import (
        _CATALOG_DISCOVERY_PRESETS,
        catalog_models_for_preset,
    )

    seed_models = list(req.models)
    seed_kinds: dict[str, str] = {}
    if not seed_models and req.preset in _CATALOG_DISCOVERY_PRESETS:
        chat_ids, embedding_ids = catalog_models_for_preset(req.preset)
        seed_models = sorted(set(chat_ids + embedding_ids))
        seed_kinds = {mid: "chat" for mid in chat_ids}
        # ``both``-kind models appear in both buckets; the embedding tag wins
        # for the picker because operators usually want embedding-side probing
        # for dual-purpose models on a dedicated embedding endpoint.
        for mid in embedding_ids:
            seed_kinds[mid] = "embedding"

    store = _store()
    try:
        endpoint = await store.create(
            name=req.name,
            preset=req.preset,
            base_url=req.base_url,
            auth_type=req.auth_type,
            plaintext_credential=plaintext,
            models=seed_models,
            rpm=req.rpm,
            headers=req.headers,
            tags=req.tags,
        )
        # Persist the role + seeded ``model_kinds`` via update() — the store's
        # ``create()`` doesn't accept those. Skip the round-trip when the
        # defaults already match.
        update_kwargs: dict[str, Any] = {}
        if role != "auto":
            update_kwargs["role"] = role
        if seed_kinds:
            update_kwargs["model_kinds"] = seed_kinds  # type: ignore[assignment]
        if update_kwargs:
            updated = await store.update(endpoint.id, **update_kwargs)
            if updated is not None:
                endpoint = updated
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
            role=req.role,
            manually_kept=req.manually_kept,
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


def _build_probe_model(endpoint: Endpoint, model_id: str) -> tuple[str, str, bool]:
    """Return ``(litellm_provider, model_id, drop_base_url)`` for a probe.

    Thin wrapper around :func:`route_for_endpoint` — the single source of truth
    for ``(preset, base_url, model)`` → ``(provider, litellm_model)`` resolution
    shared between the Test Connection probe and ``LLMProvider.resolve_for_call``.
    Both paths must agree or operators get "Test passes / dispatch 404s".
    """
    from beever_atlas.services.llm_dispatch import route_for_endpoint

    return route_for_endpoint(endpoint.preset, endpoint.base_url, model_id)


# Presets whose natural probe side is embedding even when role="both"/"auto".
# Mirrors ``_EMBEDDING_ONLY_PRESETS`` above plus ``model_classifier``'s own
# embedding-only set. ``cohere`` lives in the embedding-only UX bucket per
# PR-α but the provider serves both — operators who flip the role to ``chat``
# get the chat probe regardless.
_EMBEDDING_NATURAL_PRESETS: frozenset[str] = frozenset({"jina_ai", "voyage"})


# Preferred probe model order, per preset, per kind. The Test probe walks
# these first — falling back to the first matching ``endpoint.models`` entry
# only when none of the preferred ids are configured on the Endpoint. This
# guards against ``endpoint.models[0]`` landing on a model the provider's
# OpenAI-compat shim rejects (e.g. ``models/gemini-2.5-flash-image-preview``
# returning ``INVALID_ARGUMENT: 'This model only supports Interactions API'``).
# Order matters: cheaper / faster / more-stable variants come first.
_PROBE_PREFERRED: dict[str, dict[str, tuple[str, ...]]] = {
    "openai": {
        "chat": ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"),
        "embedding": ("text-embedding-3-small", "text-embedding-3-large"),
    },
    "google_ai": {
        "chat": (
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-2.5-pro",
            "gemini-1.5-pro",
        ),
        "embedding": ("text-embedding-004", "gemini-embedding-001"),
    },
    "anthropic": {
        "chat": ("claude-haiku-4-5", "claude-3-5-haiku-latest", "claude-3-haiku-20240307"),
    },
    "mistral": {
        "chat": ("mistral-small-latest", "mistral-large-latest"),
        "embedding": ("mistral-embed",),
    },
    "groq": {
        "chat": ("llama-3.1-8b-instant", "llama-3.3-70b-versatile"),
    },
    "deepseek": {"chat": ("deepseek-chat",)},
    "xai": {"chat": ("grok-2-mini", "grok-2")},
    "cohere": {"embedding": ("embed-english-v3.0", "embed-multilingual-v3.0")},
    "voyage": {"embedding": ("voyage-3", "voyage-3-lite", "voyage-large-2")},
    "jina_ai": {"embedding": ("jina-embeddings-v3", "jina-embeddings-v2-base-en")},
}


# Substrings in upstream 4xx error messages that indicate the *picked model
# id* is the problem — not the credential, not the endpoint. When the probe
# trips one of these we retry against the next preferred candidate before
# returning a failure, so operators with 40+ chat-tagged Gemini models don't
# have to manually shuffle ``endpoint.models`` to get a green Test.
_MODEL_REJECT_PHRASES: tuple[str, ...] = (
    "invalid_argument",
    "only supports interactions api",
    "model not found",
    "unsupported model",
    "model_not_found",
    "no such model",
    "is not supported",
    "does not support",
    "incompatible model",
)


def _looks_like_model_reject(exc: BaseException) -> bool:
    """Return True iff the exception message points at the picked model id."""
    msg = str(exc).lower()
    return any(p in msg for p in _MODEL_REJECT_PHRASES)


def _probe_candidates(endpoint: Endpoint, desired: Literal["chat", "embedding"]) -> list[str]:
    """Return the ordered list of model ids to try for a ``desired`` kind probe.

    Resolution:
      1. Preset-preferred ids (``_PROBE_PREFERRED``) that ARE in
         ``endpoint.models`` AND match the desired kind (per
         :func:`_kind_matches`).
      2. Remaining ``endpoint.models`` entries that match the desired kind.
      3. Pre-α fallback: ``endpoint.models[0]`` so Test still tries SOMETHING
         on a freshly-imported endpoint that hasn't been re-Discovered.

    De-duplicated while preserving order.
    """
    preferred = _PROBE_PREFERRED.get(endpoint.preset, {}).get(desired, ())
    seen: set[str] = set()
    ordered: list[str] = []

    has_kinds = bool(endpoint.model_kinds)

    def _kind_matches(mid: str) -> bool:
        """Decide whether ``mid`` is eligible for a ``desired``-kind probe.

        * If ``model_kinds`` is non-empty (post-α doc): trust the persisted
          map — only accept ids whose kind == desired. Missing-from-map ids
          were classifier-dropped (reranker/VLM/image-gen/etc.) and would
          400 the probe.
        * If ``model_kinds`` is empty (pre-α doc): trust the live classifier.
          Accept when the classifier returns ``desired``; tolerate ``None``
          (unknown) only as a last resort via the fallback below.
        """
        if has_kinds:
            return endpoint.model_kinds.get(mid) == desired
        inferred = classify_model(endpoint.preset, mid)
        return inferred == desired

    def _accept(mid: str) -> None:
        if mid in seen:
            return
        if not _kind_matches(mid):
            return
        seen.add(mid)
        ordered.append(mid)

    # Preset-preferred first — match against bare ids AND ``models/``-prefixed
    # ids (Gemini discovery returns ``models/gemini-2.5-flash`` shape).
    for pref in preferred:
        for mid in endpoint.models:
            if mid == pref or mid == f"models/{pref}":
                _accept(mid)
                break

    # Then the rest in ``endpoint.models`` order.
    for mid in endpoint.models:
        _accept(mid)

    # Pre-α fallback — at least try ``models[0]`` so we don't 422 on a
    # freshly-imported endpoint that has not been re-Discovered yet.
    if not ordered and endpoint.models:
        ordered.append(endpoint.models[0])

    return ordered


def pick_probe_model(
    endpoint: Endpoint,
    intent: Literal["auto", "chat", "embedding"] = "auto",
) -> tuple[str, Literal["chat", "embedding"]]:
    """Pick the model + kind to probe for Test Connection.

    Resolution:
      * If ``intent`` is "chat" or "embedding" → first preferred-or-matching
        entry in ``endpoint.models`` (see :func:`_probe_candidates`).
      * If "auto" → resolve from ``endpoint.role``:
          - role="embedding"           → kind="embedding"
          - role="chat"                → kind="chat"
          - role="both" | role="auto"  → prefer the preset's natural side:
              embedding-only presets (jina_ai, voyage) → "embedding";
              others → "chat".
      * Fall back to ``endpoint.models[0]`` (with kind inferred via the
        classifier) when no model of the desired kind exists — preserves
        the old behaviour rather than 422-ing the operator.
    """
    if not endpoint.models:
        # Caller should short-circuit before us, but guard anyway.
        raise ValueError("endpoint has no models")

    if intent == "auto":
        role = endpoint.role
        if role == "embedding":
            desired: Literal["chat", "embedding"] = "embedding"
        elif role == "chat":
            desired = "chat"
        else:
            # role == "both" | "auto" — preset's natural side wins.
            desired = "embedding" if endpoint.preset in _EMBEDDING_NATURAL_PRESETS else "chat"
    else:
        desired = intent

    candidates = _probe_candidates(endpoint, desired)
    if candidates:
        # First candidate — preset-preferred ids come first when configured.
        head = candidates[0]
        # When the head's classifier kind disagrees with ``desired`` and the
        # endpoint's stored ``model_kinds`` is empty (pre-α doc), trust the
        # classifier — it's a better guide than ``desired`` for legacy docs.
        if not endpoint.model_kinds:
            inferred = classify_model(endpoint.preset, head)
            if inferred in ("chat", "embedding"):
                return head, inferred
        return head, desired

    # Defensive fallback (should not reach — _probe_candidates already
    # appends models[0] when nothing else matches).
    fallback_id = endpoint.models[0]
    inferred = classify_model(endpoint.preset, fallback_id)
    if inferred in ("chat", "embedding"):
        return fallback_id, inferred
    return fallback_id, desired


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

    # PR-β: pick model + kind based on role + per-model classification.
    probed_model, probed_kind = pick_probe_model(endpoint, "auto")
    # Force the embedding path for embedding-only presets even when the kind
    # came back "chat" (e.g. legacy doc with no model_kinds + classifier
    # fallback). ``route_for_endpoint`` raises for embedding-only presets, so
    # those must route through the embedding probe regardless.
    is_embedding_only = endpoint.preset in _EMBEDDING_ONLY_PRESETS
    use_embedding_path = is_embedding_only or probed_kind == "embedding"

    def _resolve_probe(model_id: str) -> tuple[str, str, bool]:
        """Pick LiteLLM ``(provider, full_model, drop_base_url)`` for ``model_id``.

        For the embedding path, defers to
        :func:`beever_atlas.llm.embeddings._route_embedding_for_dispatch` so
        Test and the production embedding dispatch share one decision tree
        — closes the "Test passes / dispatch 404s" failure mode the
        architecture review explicitly flagged.
        """
        if use_embedding_path:
            from beever_atlas.llm.embeddings import _route_embedding_for_dispatch

            # Translate the Endpoint preset to LiteLLM's provider prefix
            # (``google_ai`` → ``gemini``, OpenAI-compat presets → ``openai``,
            # etc.) and the bare model id LiteLLM expects.
            provider = preset_to_provider(endpoint.preset)
            bare = model_id.removeprefix("models/")
            litellm_model = model_id if "/" in model_id else f"{provider}/{bare}"
            routed_provider, routed_model, drop_api_base = _route_embedding_for_dispatch(
                provider, litellm_model, endpoint.base_url
            )
            return routed_provider, routed_model, drop_api_base
        return _build_probe_model(endpoint, model_id)

    provider, full_model, drop_base_url = _resolve_probe(probed_model)

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
        extra_kwargs["api_base"] = _rewrite_ollama_localhost(endpoint.preset, endpoint.base_url)
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

    # Build the ordered candidate list — preset-preferred first, then the
    # rest of ``endpoint.models``. The first candidate is what we already
    # routed above; only re-resolve provider/full_model on retry.
    desired_kind: Literal["chat", "embedding"] = "embedding" if use_embedding_path else "chat"
    candidates = _probe_candidates(endpoint, desired_kind)
    if probed_model in candidates:
        # Make sure ``probed_model`` is at the head so the first attempt
        # matches what we resolved above.
        candidates = [probed_model] + [c for c in candidates if c != probed_model]
    elif not candidates:
        candidates = [probed_model]
    # Bound retries — at most 3 attempts including the first.
    candidates = candidates[:3]

    started = time.monotonic()
    last_exc: BaseException | None = None
    last_attempt_model = probed_model
    for attempt_idx, candidate in enumerate(candidates):
        if attempt_idx == 0:
            attempt_provider, attempt_model, attempt_drop_base = (
                provider,
                full_model,
                drop_base_url,
            )
        else:
            attempt_provider, attempt_model, attempt_drop_base = _resolve_probe(candidate)
            # Re-honour drop_base_url on retry (native Gemini path).
            if attempt_drop_base:
                extra_kwargs.pop("api_base", None)
            elif endpoint.base_url:
                extra_kwargs["api_base"] = _rewrite_ollama_localhost(
                    endpoint.preset, endpoint.base_url
                )
        last_attempt_model = candidate
        try:
            if use_embedding_path:
                # Embedding probe — route through ``litellm.aembedding``.
                # Required for embedding-only providers (Jina, Voyage, Cohere)
                # AND for chat-capable providers whose role + model_kinds say
                # "probe an embedding model".
                await dispatch_embedding(
                    provider=attempt_provider,
                    model=attempt_model,
                    input=["test"],
                    timeout=_PROBE_TIMEOUT_SECONDS,
                    **extra_kwargs,
                )
            else:
                await dispatch_completion(
                    provider=attempt_provider,
                    model=attempt_model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=1,
                    timeout=_PROBE_TIMEOUT_SECONDS,
                    **extra_kwargs,
                )
            # Success — the candidate that worked is our probed model.
            latency_ms = int((time.monotonic() - started) * 1000)
            await _store().record_test_result(endpoint_id, ok=True)
            return TestConnectionResponse(
                ok=True,
                latency_ms=latency_ms,
                probed_model=candidate,
                probed_kind=desired_kind,
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            # Only retry on model-rejection signals — credential / network
            # failures aren't going to be fixed by picking a different model.
            if _looks_like_model_reject(exc) and attempt_idx + 1 < len(candidates):
                continue
            break

    # All attempts failed (or the first failure was not a model-rejection).
    assert last_exc is not None
    api_base = extra_kwargs.get("api_base")
    friendly = _friendly_probe_error(last_exc, api_base if isinstance(api_base, str) else None)
    if friendly is not None:
        error_msg = friendly
    else:
        # Sanitise the error before returning it — some LiteLLM SDK
        # exceptions embed the full request kwargs (including ``api_key``)
        # in their repr. When the message looks like it could carry
        # credential fragments, drop the body and return only the
        # exception class + a generic note.
        raw = _redact_credential_fragments(str(last_exc)[:200])
        error_msg = f"{type(last_exc).__name__}: {raw}"
    # Surface which model was probed so operators know which entry to move
    # to advanced — invaluable when ``endpoint.models`` has 40+ chat entries.
    error_msg = f"[probed {last_attempt_model}] {error_msg}"
    await _store().record_test_result(endpoint_id, ok=False, error=error_msg)
    return TestConnectionResponse(
        ok=False,
        error=error_msg,
        probed_model=last_attempt_model,
        probed_kind=desired_kind,
    )


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
