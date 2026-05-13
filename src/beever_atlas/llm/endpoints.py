"""Endpoint catalog — first-class LLM endpoint objects with UUID identity.

The data model that supersedes the parallel ``embedding_settings`` +
``agent_model_config`` + ``secrets.embedding_api_key`` collections. Each
Endpoint represents one LLM endpoint Atlas can talk to (a preset like OpenAI
or Anthropic, an Ollama instance, a custom OpenAI-compatible URL, etc.) and
carries its own encrypted credential, RPM budget, and curated model list.

See ``openspec/changes/agent-llm-provider-pluggable/design.md`` D1 + D3 + D7
for the rationale. The hydration shim that migrates legacy collections into
this catalog at boot lives in ``scripts/migrate_to_endpoint_catalog.py``
(PR-G); for PR-B.1 we only ship the data model + store.
"""

from __future__ import annotations

import base64
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)


AuthType = Literal["api_key", "aws_iam", "google_sa", "none", "oauth"]


# Endpoint preset key → LiteLLM provider prefix. Most are 1:1; a few diverge:
# ``google_ai`` is the operator-facing label but LiteLLM uses ``gemini/``;
# the OpenAI-compatible proxies (vLLM, LM Studio, OpenRouter, LiteLLM Proxy,
# Custom) all implement the OpenAI request shape. SINGLE SOURCE OF TRUTH —
# imported by ``llm/provider.py`` (resolve_for_call) and ``api/assignments.py``
# (capability validation) so dispatch + validation never disagree.
_PRESET_TO_PROVIDER: dict[str, str] = {
    "google_ai": "gemini",
    "ollama": "ollama",
    "vllm": "openai",
    "lmstudio": "openai",
    "openrouter": "openai",
    "litellm_proxy": "openai",
    "custom": "openai",
}


def preset_to_provider(preset: str) -> str:
    """Translate an Endpoint preset key to its LiteLLM provider prefix."""
    return _PRESET_TO_PROVIDER.get(preset, preset)


# Commercial presets whose Discover/auto-populate should read from Atlas's
# curated ``KNOWN_MODELS`` catalog instead of hitting the provider's
# ``/models`` API.
#
# Why: provider catalogs are noisy — OpenAI returns hundreds of internal
# variants ("o4-mini-deep-research-alpha-2025-..."), Gemini returns Live /
# Realtime / TTS models that 400 on the OpenAI-compat shim, Jina returns
# ``jina-ai/jina-code-embeddings-0.5b`` entries that 422 the inference API.
# Walking those produces models that look valid but break Test / dispatch
# in non-obvious ways. The curated catalog is small (~3-5 per provider),
# every entry is one we've actually validated, and the catalog version
# travels with the Atlas release.
#
# Operator-deployed presets (ollama, vllm, lmstudio, openrouter,
# litellm_proxy, custom) keep the upstream-``/models``-API path — the
# model list there IS the source of truth (operator chose what to host).
_CATALOG_DISCOVERY_PRESETS: frozenset[str] = frozenset(
    {
        "openai",
        "google_ai",
        "anthropic",
        "mistral",
        "deepseek",
        "groq",
        "together_ai",
        "xai",
        "minimax",
        "cohere",
        "voyage",
        "jina_ai",
    }
)


def catalog_models_for_preset(preset: str) -> tuple[list[str], list[str]]:
    """Return ``(chat_ids, embedding_ids)`` curated for ``preset``.

    Reads from ``llm/known_models.py``'s ``KNOWN_MODELS`` dict — the
    canonical source of truth for models Atlas has tested end-to-end.

    Returns ids in their *bare* shape (no ``provider/`` prefix), the same
    way an operator would type them and the same way ``endpoint.models``
    is stored. For ``google_ai``, the catalog keys use ``gemini/`` (the
    LiteLLM provider prefix); we strip that here.

    Returns ``([], [])`` for presets not present in the catalog.
    """
    from beever_atlas.llm.known_models import KNOWN_MODELS

    provider_prefix = preset_to_provider(preset)
    chat: list[str] = []
    embedding: list[str] = []
    for catalog_key, spec in KNOWN_MODELS.items():
        if "/" not in catalog_key:
            continue
        key_prefix, bare_id = catalog_key.split("/", 1)
        if key_prefix != provider_prefix:
            continue
        kind = spec.get("kind", "chat")
        if kind == "embedding":
            embedding.append(bare_id)
        elif kind == "both":
            chat.append(bare_id)
            embedding.append(bare_id)
        else:
            chat.append(bare_id)
    return sorted(set(chat)), sorted(set(embedding))


# Substrings that, if present in an upstream error body or an SDK exception
# repr, mean the text may carry a credential fragment (Bearer token, OpenAI
# ``sk-`` key, Google ``AIzaSy`` key, AWS ``AKIA`` id / secret, a service-
# account ``private_key``/``client_email``, etc.). Such text is replaced with
# a generic note before it crosses an API boundary or hits a log line. Match
# against the LOWER-cased haystack.
_CREDENTIAL_MARKERS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "authorization",
    "bearer ",
    "sk-",
    "aizasy",
    "akia",
    "aws_secret",
    "access_key",
    "secret",
    "token=",
    "private_key",
    "client_email",
    "password",
    "credential",
)


def _redact_credential_fragments(text: str) -> str:
    """Return ``text`` unchanged unless it looks like it carries a secret."""
    if any(m in text.lower() for m in _CREDENTIAL_MARKERS):
        return "(redacted — upstream text may contain credential fragments)"
    return text


# Default per-preset RPM budgets — conservative free-tier-safe values.
# Operators raise via env (``LLM_PROVIDER_RPM_<PROVIDER>``) or via the UI on
# the Endpoint document. See design D7. PR-B.2 wires these into ``LLMThrottle``;
# for PR-B.1 they live here as the source of truth.
DEFAULT_PROVIDER_RPM: dict[str, int] = {
    "gemini": 1000,
    "openai": 500,
    "anthropic": 100,
    "mistral": 500,
    "deepseek": 500,
    "groq": 30,
    "together_ai": 500,
    "xai": 100,
    "minimax": 200,
    "cohere": 500,
    "ollama_chat": 1000,
    "ollama": 1000,
    "vertex_ai": 1000,
    "bedrock": 1000,
    "jina_ai": 500,
    "voyage": 500,
    "custom": 500,
}


# A persisted per-model kind label. Only chat / embedding land on the
# Endpoint document — finer categories (reranker, image_gen, …) returned
# by the classifier surface in the *dropped* breakdown for UI, never in
# the kept-models map. See ``llm/model_classifier.py``.
PersistedModelKind = Literal["chat", "embedding"]


# Role hint per endpoint. ``auto`` means "infer from model_kinds"; the
# remaining values are soft hints the UI uses to default the Test Connection
# probe + the model picker, NOT a hard gate on Assignments. See design D7.
EndpointRole = Literal["chat", "embedding", "both", "auto"]


@dataclass
class Endpoint:
    """A single LLM endpoint Atlas can route consumers to.

    Identity is the UUIDv4 ``id`` — operators can have multiple Endpoints
    of the same preset (e.g. OpenAI prod + OpenAI staging) without naming
    collisions. The ``name`` field is a free-form operator label.
    """

    id: str
    name: str
    preset: str
    base_url: str
    auth_type: AuthType
    # Encrypted credential payload. For ``auth_type=api_key`` the decrypted
    # plaintext is the raw API key string. For ``aws_iam`` it is a JSON blob
    # ``{access_key_id, secret_access_key, region}``. For ``google_sa`` it is
    # the service-account JSON. For ``none`` and ``oauth`` it is ``None``.
    encrypted_key: dict[str, str] | None
    models: list[str]
    rpm: int
    headers: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    last_test_at: str | None = None
    last_test_ok: bool | None = None
    last_test_error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    # ── Model classification (PR-α) ──────────────────────────────────────
    # Per-model kind for every id in ``models``. Only ``chat`` and
    # ``embedding`` are persisted here — the rest (rerankers, image-gen,
    # TTS, …) are routed into ``advanced_models`` and surface in a separate
    # "advanced models" UI bucket. Old documents without this field hydrate
    # to ``{}`` (lazy backfill on next Re-Discover).
    model_kinds: dict[str, PersistedModelKind] = field(default_factory=dict)
    # Discovered ids the classifier dropped (rerankers, image-gen, TTS, …).
    # The full per-category breakdown lives in the discover response; the
    # persisted form is just a flat list so the UI can offer
    # "Promote to active" later.
    advanced_models: list[str] = field(default_factory=list)
    # Ids the operator manually promoted out of ``advanced_models`` (or
    # added by hand). These survive a Re-Discover even when the classifier
    # would otherwise drop them.
    manually_kept: list[str] = field(default_factory=list)
    # Soft hint — ``auto`` means "infer from model_kinds"; the remaining
    # values bias the default Test Connection probe + picker. Not a hard
    # gate on Assignments.
    role: EndpointRole = "auto"

    @classmethod
    def from_document(cls, doc: dict[str, Any]) -> "Endpoint":
        """Hydrate from a MongoDB document; tolerant of legacy/missing fields."""
        # Defensive defaults — old documents (pre-PR-α) lack the new fields.
        raw_kinds = doc.get("model_kinds") or {}
        model_kinds: dict[str, PersistedModelKind] = {
            str(k): cast(PersistedModelKind, v)
            for k, v in raw_kinds.items()
            if v in ("chat", "embedding")
        }
        return cls(
            id=cast(str, doc["id"]),
            name=cast(str, doc.get("name") or ""),
            preset=cast(str, doc.get("preset") or "custom"),
            base_url=cast(str, doc.get("base_url") or ""),
            auth_type=cast(AuthType, doc.get("auth_type") or "api_key"),
            encrypted_key=doc.get("encrypted_key"),
            models=list(doc.get("models") or []),
            rpm=int(doc.get("rpm") or DEFAULT_PROVIDER_RPM.get(doc.get("preset") or "", 500)),
            headers=dict(doc.get("headers") or {}),
            tags=list(doc.get("tags") or []),
            last_test_at=doc.get("last_test_at"),
            last_test_ok=doc.get("last_test_ok"),
            last_test_error=doc.get("last_test_error"),
            created_at=cast(str, doc.get("created_at") or ""),
            updated_at=cast(str, doc.get("updated_at") or ""),
            model_kinds=model_kinds,
            advanced_models=list(doc.get("advanced_models") or []),
            manually_kept=list(doc.get("manually_kept") or []),
            role=cast(EndpointRole, doc.get("role") or "auto"),
        )

    def to_document(self) -> dict[str, Any]:
        """Serialise to a MongoDB-shaped document (no ``_id`` — the store sets it)."""
        return asdict(self)


def encrypt_endpoint_credential(plaintext: dict[str, str] | str) -> dict[str, str]:
    """Encrypt a credential payload for storage on an Endpoint document.

    ``plaintext`` is either a single key string (api_key path) or a dict
    (aws_iam / google_sa). Returns the ``{ciphertext_b64, iv_b64, tag_b64}``
    envelope shape used by the existing ``CredentialEncryptor``. Raises
    ``RuntimeError`` when ``CREDENTIAL_MASTER_KEY`` is unconfigured.
    """
    from beever_atlas.infra.crypto import encrypt_credentials

    payload = {"value": plaintext} if isinstance(plaintext, str) else dict(plaintext)
    ciphertext, iv, tag = encrypt_credentials(payload)
    return {
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
        "iv_b64": base64.b64encode(iv).decode("ascii"),
        "tag_b64": base64.b64encode(tag).decode("ascii"),
    }


def decrypt_endpoint_credential(envelope: dict[str, str]) -> dict[str, str] | str | None:
    """Decrypt an Endpoint's ``encrypted_key`` envelope.

    Returns the same shape that was encrypted: a string when the original
    plaintext was an api_key, otherwise a dict (aws_iam / google_sa). Returns
    ``None`` when the envelope is empty / malformed (logs at WARNING).
    """
    from beever_atlas.infra.crypto import decrypt_credentials

    if not envelope:
        return None
    try:
        ciphertext = base64.b64decode(envelope["ciphertext_b64"])
        iv = base64.b64decode(envelope["iv_b64"])
        tag = base64.b64decode(envelope["tag_b64"])
    except (KeyError, ValueError) as exc:
        logger.warning("endpoint credential envelope malformed: %s", exc)
        return None
    payload = decrypt_credentials(ciphertext, iv, tag)
    # api_key path stored under the single key "value"; return string directly
    if len(payload) == 1 and "value" in payload:
        return cast(str, payload["value"])
    return cast(dict[str, str], payload)


class EndpointStore:
    """CRUD over the ``endpoints`` Mongo collection.

    Wraps the raw collection so callers don't have to know the document
    shape. Encryption is transparent — :meth:`create` and :meth:`update`
    accept ``plaintext_credential`` and persist the encrypted envelope.
    """

    def __init__(self, mongodb_store: Any) -> None:
        self._mongo = mongodb_store

    @property
    def _collection(self) -> Any:
        return self._mongo.db["endpoints"]

    async def list(self) -> list[Endpoint]:
        cursor = self._collection.find({}, {"_id": 0})
        return [Endpoint.from_document(doc) async for doc in cursor]

    async def get(self, endpoint_id: str) -> Endpoint | None:
        doc = await self._collection.find_one({"id": endpoint_id}, {"_id": 0})
        return Endpoint.from_document(doc) if doc else None

    async def get_by_name(self, name: str) -> Endpoint | None:
        doc = await self._collection.find_one({"name": name}, {"_id": 0})
        return Endpoint.from_document(doc) if doc else None

    async def create(
        self,
        *,
        name: str,
        preset: str,
        base_url: str,
        auth_type: AuthType,
        plaintext_credential: dict[str, str] | str | None = None,
        models: list[str] | None = None,
        rpm: int | None = None,
        headers: dict[str, str] | None = None,
        tags: list[str] | None = None,
    ) -> Endpoint:
        """Insert a new Endpoint document. Encrypts ``plaintext_credential``
        before persisting. ``None`` is allowed when ``auth_type`` is ``none``."""
        now = datetime.now(tz=UTC).isoformat()
        envelope = (
            encrypt_endpoint_credential(plaintext_credential)
            if plaintext_credential is not None
            else None
        )
        endpoint = Endpoint(
            id=str(uuid.uuid4()),
            name=name,
            preset=preset,
            base_url=base_url,
            auth_type=auth_type,
            encrypted_key=envelope,
            models=models or [],
            rpm=rpm if rpm is not None else DEFAULT_PROVIDER_RPM.get(preset, 500),
            headers=headers or {},
            tags=tags or [],
            created_at=now,
            updated_at=now,
        )
        await self._collection.insert_one(endpoint.to_document())
        return endpoint

    async def update(
        self,
        endpoint_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        auth_type: AuthType | None = None,
        plaintext_credential: dict[str, str] | str | None | object = ...,
        models: list[str] | None = None,
        rpm: int | None = None,
        headers: dict[str, str] | None = None,
        tags: list[str] | None = None,
        model_kinds: dict[str, PersistedModelKind] | None = None,
        advanced_models: list[str] | None = None,
        manually_kept: list[str] | None = None,
        role: EndpointRole | None = None,
    ) -> Endpoint | None:
        """Patch fields on an existing Endpoint.

        Pass ``plaintext_credential=None`` to clear the credential (auth_type
        flips to ``none`` semantically — caller updates that field too).
        Pass ``plaintext_credential=...`` (the default sentinel) to leave the
        credential unchanged.
        """
        updates: dict[str, Any] = {"updated_at": datetime.now(tz=UTC).isoformat()}
        if name is not None:
            updates["name"] = name
        if base_url is not None:
            updates["base_url"] = base_url
        if auth_type is not None:
            updates["auth_type"] = auth_type
        if models is not None:
            updates["models"] = models
        if rpm is not None:
            updates["rpm"] = rpm
        if headers is not None:
            updates["headers"] = headers
        if tags is not None:
            updates["tags"] = tags
        if model_kinds is not None:
            updates["model_kinds"] = dict(model_kinds)
        if advanced_models is not None:
            updates["advanced_models"] = list(advanced_models)
        if manually_kept is not None:
            updates["manually_kept"] = list(manually_kept)
        if role is not None:
            updates["role"] = role
        if plaintext_credential is not ...:
            cred = cast("dict[str, str] | str | None", plaintext_credential)
            updates["encrypted_key"] = (
                encrypt_endpoint_credential(cred) if cred is not None else None
            )

        result = await self._collection.update_one({"id": endpoint_id}, {"$set": updates})
        if result.matched_count == 0:
            return None
        return await self.get(endpoint_id)

    async def delete(self, endpoint_id: str) -> bool:
        result = await self._collection.delete_one({"id": endpoint_id})
        return result.deleted_count > 0

    async def record_test_result(
        self,
        endpoint_id: str,
        *,
        ok: bool,
        error: str | None = None,
    ) -> None:
        """Stamp ``last_test_*`` fields after a probe completes."""
        await self._collection.update_one(
            {"id": endpoint_id},
            {
                "$set": {
                    "last_test_at": datetime.now(tz=UTC).isoformat(),
                    "last_test_ok": ok,
                    "last_test_error": error,
                }
            },
        )


# ────────────────────────────────────────────────────────────────────────
# /v1/models discovery — design D4.
# ────────────────────────────────────────────────────────────────────────


class DiscoveryResult(dict[str, Any]):
    """Return shape from ``discover_models``.

    Keys (post PR-α):
      * ``ok`` (bool) — request succeeded.
      * ``models`` (list[str]) — kept ids = chat + embedding, sorted.
        Kept for backward compatibility with callers that don't yet read
        ``models_by_kind``.
      * ``models_by_kind`` (dict[str, list[str]]) —
        ``{"chat": [...], "embedding": [...]}``.
      * ``dropped`` (dict[str, list[str]]) — finer category → ids the
        classifier excluded (rerankers, image_gen, audio_*, fine_tune, …).
      * ``error`` (str | None) — populated when ``ok`` is False.
    """


async def discover_models(
    endpoint: Endpoint,
    *,
    plaintext_credential: str | None = None,
    timeout_seconds: float = 10.0,
) -> DiscoveryResult:
    """Issue a discovery request against ``endpoint`` and return the model list.

    Discovery shape depends on the preset:
      * Ollama: ``GET {base_url}/api/tags`` → ``models[].name``
        (Note: Ollama's OpenAI-compat shim doesn't expose tags, so we hit
        the native ``/api/tags`` instead — strip the ``/v1`` suffix.)
      * Default (OpenAI-compatible): ``GET {base_url}/models`` → ``data[].id``.
      * Bedrock / Vertex: not implemented (preset-specific SDK call). Surfaces
        a clear error so the UI can prompt the operator to enter models manually.

    Never raises on transport error — surfaces ``{ok: False, error: ...}``.
    """
    import httpx

    # Commercial providers — load Atlas's curated catalog instead of hitting
    # the upstream ``/models`` endpoint. Returns 3-5 known-good entries with
    # zero ``dropped`` bucket, so the UI never surfaces models that 400 / 422
    # at Test or dispatch time. See ``_CATALOG_DISCOVERY_PRESETS`` for why.
    if endpoint.preset in _CATALOG_DISCOVERY_PRESETS:
        chat_ids, embedding_ids = catalog_models_for_preset(endpoint.preset)
        kept = sorted(set(chat_ids + embedding_ids))
        if not kept:
            # Defensive — catalog drift would otherwise produce an empty
            # discovery silently. Operators see the underlying cause.
            return DiscoveryResult(
                ok=False,
                models=[],
                models_by_kind={"chat": [], "embedding": []},
                dropped={},
                error=(
                    f"catalog_empty_for_preset: {endpoint.preset}. "
                    "Update ``llm/known_models.py`` or enter models manually."
                ),
            )
        return DiscoveryResult(
            ok=True,
            models=kept,
            models_by_kind={
                "chat": sorted(set(chat_ids)),
                "embedding": sorted(set(embedding_ids)),
            },
            dropped={},
        )

    if endpoint.preset in ("bedrock", "vertex_ai"):
        return DiscoveryResult(
            ok=False,
            models=[],
            models_by_kind={"chat": [], "embedding": []},
            dropped={},
            error=(
                f"discovery_not_supported_for_preset: {endpoint.preset}. "
                "Enter model names manually."
            ),
        )

    if not endpoint.base_url:
        return DiscoveryResult(
            ok=False,
            models=[],
            models_by_kind={"chat": [], "embedding": []},
            dropped={},
            error="discovery_no_base_url: base_url is empty",
        )

    # Resolve auth header. Anthropic's /v1/models endpoint accepts ONLY
    # ``x-api-key`` + ``anthropic-version``; ``Authorization: Bearer`` 401s
    # silently. Every other supported preset uses the OpenAI bearer shape.
    headers = dict(endpoint.headers)
    if plaintext_credential and endpoint.auth_type == "api_key":
        if endpoint.preset == "anthropic":
            headers["x-api-key"] = plaintext_credential
            headers.setdefault("anthropic-version", "2023-06-01")
        else:
            headers["Authorization"] = f"Bearer {plaintext_credential}"

    # Pick the discovery URL per preset.
    if endpoint.preset in ("ollama", "ollama_chat"):
        # Strip /v1 suffix — Ollama's tag list lives under the native API.
        base = endpoint.base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        url = f"{base}/api/tags"
        list_key = "models"  # different shape: [{name, ...}]
    else:
        url = f"{endpoint.base_url.rstrip('/')}/models"
        list_key = "data"  # OpenAI shape: [{id, ...}]

    # Opt-in SSRF guard: refuse private/link-local/metadata targets before
    # we attach the credential and call out. Off by default so local presets
    # (Ollama/vLLM/LM Studio at localhost) keep working — see settings.
    from beever_atlas.infra.config import get_settings

    if get_settings().llm_endpoint_ssrf_guard:
        from beever_atlas.infra.http_safe import resolve_and_validate

        try:
            resolve_and_validate(url)
        except (ValueError, PermissionError) as exc:
            return DiscoveryResult(
                ok=False,
                models=[],
                models_by_kind={"chat": [], "embedding": []},
                dropped={},
                error=f"discovery_url_blocked: {exc}",
            )

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.get(url, headers=headers)
    except (httpx.TimeoutException,) as exc:
        return DiscoveryResult(
            ok=False,
            models=[],
            models_by_kind={"chat": [], "embedding": []},
            dropped={},
            error=f"discovery_timeout: {timeout_seconds}s ({exc})",
        )
    except httpx.HTTPError as exc:
        return DiscoveryResult(
            ok=False,
            models=[],
            models_by_kind={"chat": [], "embedding": []},
            dropped={},
            error=f"discovery_connect_error: {exc}",
        )

    if resp.status_code != 200:
        body = _redact_credential_fragments(resp.text[:200])
        return DiscoveryResult(
            ok=False,
            models=[],
            models_by_kind={"chat": [], "embedding": []},
            dropped={},
            error=f"discovery_http_{resp.status_code}: {body}",
        )

    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return DiscoveryResult(
            ok=False,
            models=[],
            models_by_kind={"chat": [], "embedding": []},
            dropped={},
            error="discovery_invalid_json_response",
        )

    entries = payload.get(list_key)
    if not isinstance(entries, list):
        return DiscoveryResult(
            ok=False,
            models=[],
            models_by_kind={"chat": [], "embedding": []},
            dropped={},
            error=f"discovery_invalid_response_shape: expected {{{list_key}: [...]}}",
        )

    if list_key == "models":
        # Ollama shape: extract "name" from each entry.
        model_ids = [
            str(item["name"]) for item in entries if isinstance(item, dict) and "name" in item
        ]
    else:
        # OpenAI shape: extract "id" from each entry.
        model_ids = [str(item["id"]) for item in entries if isinstance(item, dict) and "id" in item]

    # Bucket every discovered id by classifier kind. Lazy import keeps the
    # ``endpoints`` module free of a hard dep on ``model_classifier`` at
    # import time (matters for the credential-encryption tests that import
    # this module without the full LLM stack ready).
    from beever_atlas.llm.model_classifier import classify_model

    chat_ids: list[str] = []
    embedding_ids: list[str] = []
    dropped: dict[str, list[str]] = {}
    for mid in model_ids:
        kind = classify_model(endpoint.preset, mid)
        if kind == "chat":
            chat_ids.append(mid)
        elif kind == "embedding":
            embedding_ids.append(mid)
        else:
            dropped.setdefault(kind, []).append(mid)

    kept_sorted = sorted(set(chat_ids + embedding_ids))
    return DiscoveryResult(
        ok=True,
        models=kept_sorted,
        models_by_kind={
            "chat": sorted(set(chat_ids)),
            "embedding": sorted(set(embedding_ids)),
        },
        dropped={k: sorted(set(v)) for k, v in dropped.items()},
    )


__all__ = [
    "AuthType",
    "DEFAULT_PROVIDER_RPM",
    "DiscoveryResult",
    "Endpoint",
    "EndpointStore",
    "catalog_models_for_preset",
    "discover_models",
    "encrypt_endpoint_credential",
    "decrypt_endpoint_credential",
    "preset_to_provider",
    "_CATALOG_DISCOVERY_PRESETS",
]
