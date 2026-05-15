"""Hydration shim — migrate legacy data into the Endpoint + Assignment catalog.

Runs once at boot when the new ``endpoints`` collection is empty AND any
legacy data source is present (env-set credentials, ``agent_model_config``,
``embedding_settings``, ``secrets.embedding_api_key``). Synthesises one
Endpoint per credentialed provider + one Assignment per agent + the embedding
Assignment from the legacy collections.

Idempotent — re-running with non-empty ``endpoints`` is a no-op. The legacy
collections are NOT deleted; they remain authoritative for old read paths
until Phase 5 cleanup.

See ``openspec/changes/agent-llm-provider-pluggable/design.md`` D10 +
``specs/ai-installer/spec.md`` "Hydration shim".
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any

from beever_atlas.llm.assignments import (
    Assignment,
    AssignmentStore,
    DEFAULT_CONSUMERS,
)
from beever_atlas.llm.endpoints import (
    AuthType,
    EndpointStore,
    PersistedModelKind,
    catalog_models_for_preset,
    preset_to_provider,
)

# Single source of truth lives in ``llm/presets.py`` (derived from ENDPOINT_PRESETS).
from beever_atlas.llm.presets import BASE_URL_BY_PRESET as _BASE_URL_BY_PRESET


def _seeded_models_for_preset(
    preset: str,
) -> tuple[list[str], dict[str, PersistedModelKind]]:
    """Return ``(models, model_kinds)`` to pre-populate on a freshly-synthesised
    Endpoint of ``preset`` so the operator-facing UI has a populated model
    dropdown out of the box (no "Re-Discover" click required).

    Reads from the curated ``KNOWN_MODELS`` catalog via
    :func:`catalog_models_for_preset`. Returns ``([], {})`` for presets not in
    the catalog (Ollama, vLLM, OpenRouter, custom) — those endpoints still
    require operator discovery because their model lists are operator-deployed,
    not centrally curated.

    Without this, the migration created Endpoints with ``models=[]`` and the
    Settings UI showed "no models available" on every chat-endpoint card after
    a fresh install — a dead-end the operator had no UI signal to escape from.
    """
    chat_ids, embedding_ids = catalog_models_for_preset(preset)
    empty_kinds: dict[str, PersistedModelKind] = {}
    if not chat_ids and not embedding_ids:
        return ([], empty_kinds)
    models = sorted(set(chat_ids + embedding_ids))
    model_kinds: dict[str, PersistedModelKind] = {}
    for m in chat_ids:
        model_kinds[m] = "chat"
    for m in embedding_ids:
        # If a model appears in both lists (e.g. ``gemini-embedding-001`` is
        # embedding-only in our catalog), the embedding label wins — matches
        # the order callers expect when reading ``endpoint.model_kinds``.
        model_kinds[m] = "embedding"
    return (models, model_kinds)

logger = logging.getLogger(__name__)


# Provider → env-var name map for the credentials sniff. Mirrors the
# resolution map in design D6 (``llm/agent_credentials.py``).
_ENV_VAR_BY_PROVIDER: dict[str, tuple[str, str, AuthType]] = {
    # provider_key → (env_var, preset, auth_type)
    "google_ai": ("GOOGLE_API_KEY", "google_ai", "api_key"),
    "openai": ("OPENAI_API_KEY", "openai", "api_key"),
    "anthropic": ("ANTHROPIC_API_KEY", "anthropic", "api_key"),
    "mistral": ("MISTRAL_API_KEY", "mistral", "api_key"),
    "deepseek": ("DEEPSEEK_API_KEY", "deepseek", "api_key"),
    "groq": ("GROQ_API_KEY", "groq", "api_key"),
    "xai": ("XAI_API_KEY", "xai", "api_key"),
    "together_ai": ("TOGETHER_API_KEY", "together_ai", "api_key"),
    "minimax": ("MINIMAX_API_KEY", "minimax", "api_key"),
    "cohere": ("COHERE_API_KEY", "cohere", "api_key"),
    "voyage": ("VOYAGE_API_KEY", "voyage", "api_key"),
    "jina_ai": ("JINA_API_KEY", "jina_ai", "api_key"),
}


# Legacy embedding-provider enum value → Endpoint preset key. The only
# divergence is ``gemini`` (the embedding-provider enum, mirroring LiteLLM)
# vs ``google_ai`` (the operator-facing Endpoint preset key); every other
# value is 1:1. Derived as the inverse of ``endpoints.preset_to_provider``
# restricted to the embedding-capable presets.
_EMBEDDING_PROVIDER_TO_PRESET: dict[str, str] = {
    "jina_ai": "jina_ai",
    "openai": "openai",
    "gemini": "google_ai",
    "voyage": "voyage",
    "cohere": "cohere",
    "mistral": "mistral",
    "ollama": "ollama",
    "bedrock": "bedrock",
    "vertex_ai": "vertex_ai",
}

_MIGRATED_EMBEDDING_TAG = "migrated-embedding-config"


@dataclass
class _LegacyEmbeddingConfig:
    """Resolved legacy embedding settings — DB doc overlaid on env defaults."""

    provider: str
    model: str
    dimensions: int | None
    api_base: str
    task: str | None
    api_key: str | None  # decrypted plaintext, or None
    preset: str  # mapped Endpoint preset key


_EMBEDDING_ENV_VARS: tuple[str, ...] = (
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_RPM",
    "EMBEDDING_API_BASE",
    "EMBEDDING_API_KEY",
    "EMBEDDING_TASK",
)


async def _resolve_legacy_embedding_config(stores: Any) -> _LegacyEmbeddingConfig | None:
    """Resolve the effective legacy embedding config the way the deprecated
    ``api/embedding_settings.py::_resolve_effective_settings`` does — env
    (Settings) baseline, ``embedding_settings`` DB doc overlay, plus the
    DB-stored encrypted API key (``secrets.embedding_api_key``) decrypted, or
    the ``EMBEDDING_API_KEY`` env / ``cfg.embedding_api_key`` fallback.

    Returns ``None`` when there is **no** explicit legacy embedding signal —
    i.e. no ``embedding_settings`` DB doc, no ``EMBEDDING_*`` env var, and no
    ``secrets.embedding_api_key`` blob. (A bare ``Settings`` default such as
    ``jina_ai`` is NOT a signal — synthesising an Endpoint from it would be a
    false positive for installs that never configured embedding.)
    """
    # ── embedding_settings DB doc ────────────────────────────────────────
    embedding_doc: dict[str, Any] | None
    try:
        embedding_doc = await stores.mongodb.db["embedding_settings"].find_one(
            {"_id": "embedding_settings"}
        )
    except Exception:  # noqa: BLE001
        embedding_doc = None

    # ── DB-stored embedding key (mirrors api/embedding_settings._decrypt_db_key) ─
    api_key: str | None = None
    secret: dict[str, Any] | None = None
    try:
        secret = await stores.mongodb.get_embedding_secret()
    except Exception:  # noqa: BLE001
        secret = None
    if secret:
        try:
            from beever_atlas.infra.crypto import decrypt_credentials

            ciphertext = base64.b64decode(secret["ciphertext_b64"])
            iv = base64.b64decode(secret["iv_b64"])
            tag = base64.b64decode(secret["tag_b64"])
            api_key = decrypt_credentials(ciphertext, iv, tag).get("api_key")
        except RuntimeError:
            # Master key missing — leave api_key None; the endpoint will be
            # created with auth_type=none (or skipped if a key was required).
            logger.debug(
                "migrate_to_endpoint_catalog: master key unavailable; "
                "cannot decrypt legacy embedding key"
            )
        except Exception as exc:  # noqa: BLE001
            # SECURITY: NEVER pass ``exc_info=True`` here — ``api_key``,
            # ``ciphertext``, ``iv``, and ``tag`` are live locals in this
            # frame (lines 134-137 above). A structured log handler (Sentry,
            # JSON formatter, Datadog) would serialise the traceback locals
            # and leak the decrypted plaintext credential. Log the exception
            # CLASS + message only — same pattern as ``provider.py:160-163``
            # and ``agent_credentials.py:84-86``.
            logger.warning(
                "migrate_to_endpoint_catalog: failed to decrypt legacy "
                "embedding key (%s: %s)",
                type(exc).__name__,
                exc,
            )

    env_signal = any(os.environ.get(v) for v in _EMBEDDING_ENV_VARS)
    if embedding_doc is None and secret is None and not env_signal:
        return None

    # ── env / Settings baseline (mirrors _resolve_effective_settings) ────
    # Read ``os.environ`` directly first — ``get_settings()`` is ``lru_cache``-d
    # and may be stale relative to a test/env mutation. Fall back to the
    # cached Settings for any var not present in the live environment.
    provider = "jina_ai"
    model = "jina-embeddings-v4"
    dimensions: int | None = 2048
    api_base = ""
    task: str | None = "text-matching"
    cfg_api_key: str | None = None
    try:
        from beever_atlas.infra.config import get_settings

        cfg = get_settings()
        provider = cfg.embedding_provider or provider
        model = cfg.embedding_model or model
        dimensions = cfg.embedding_dimensions if cfg.embedding_dimensions else dimensions
        api_base = cfg.embedding_api_base or ""
        task = cfg.embedding_task or task
        cfg_api_key = cfg.embedding_api_key or None
    except Exception:  # noqa: BLE001 — Settings unavailable: stay on hardcoded defaults.
        logger.debug("migrate_to_endpoint_catalog: get_settings() unavailable", exc_info=True)

    if os.environ.get("EMBEDDING_PROVIDER"):
        provider = os.environ["EMBEDDING_PROVIDER"].strip()
    if os.environ.get("EMBEDDING_MODEL"):
        model = os.environ["EMBEDDING_MODEL"].strip()
    if os.environ.get("EMBEDDING_DIMENSIONS"):
        try:
            dimensions = int(os.environ["EMBEDDING_DIMENSIONS"].strip())
        except ValueError:
            pass
    if os.environ.get("EMBEDDING_API_BASE"):
        api_base = os.environ["EMBEDDING_API_BASE"].strip()
    if os.environ.get("EMBEDDING_TASK"):
        task = os.environ["EMBEDDING_TASK"].strip()
    if os.environ.get("EMBEDDING_API_KEY"):
        cfg_api_key = os.environ["EMBEDDING_API_KEY"].strip()

    # ── embedding_settings DB doc overlay ────────────────────────────────
    if embedding_doc:
        if embedding_doc.get("provider"):
            provider = embedding_doc["provider"]
        if embedding_doc.get("model"):
            model = embedding_doc["model"]
        if embedding_doc.get("dimensions") is not None:
            dimensions = embedding_doc["dimensions"]
        if embedding_doc.get("api_base"):
            api_base = embedding_doc["api_base"]
        if embedding_doc.get("task") is not None:
            task = embedding_doc["task"]

    if api_key is None:
        api_key = cfg_api_key

    preset = _EMBEDDING_PROVIDER_TO_PRESET.get(provider, provider)
    return _LegacyEmbeddingConfig(
        provider=provider,
        model=model,
        dimensions=dimensions,
        api_base=api_base,
        task=task,
        api_key=api_key,
        preset=preset,
    )


async def _backfill_catalog_models(
    endpoint_store: EndpointStore,
    existing_endpoints: list,
) -> int:
    """Self-heal: populate ``models[]`` + ``model_kinds`` from the curated
    catalog for any endpoint that was created with an empty ``models`` list
    AND whose preset is in the catalog discovery set.

    Targets the pre-F9 install case: operators who already ran a previous
    boot got endpoints with ``models=[]`` and saw "no models available" in
    the Settings UI. On the next server start, this backfill runs once and
    the UI picker lights up — no operator action required.

    Idempotent: skips endpoints that already have a non-empty ``models``
    list (operator may have curated their own subset; never overwrite).
    Returns the number of endpoints backfilled.
    """
    backfilled = 0
    for endpoint in existing_endpoints:
        # Self-heal: pre-F11 google_ai endpoints were created with a base_url
        # pointing at the OpenAI-compat shim
        # (``https://generativelanguage.googleapis.com/v1beta/openai/``).
        # That URL forces ``route_for_endpoint`` to pick LiteLLM's ``openai``
        # provider, which does NOT honor Gemini's
        # ``response_mime_type="application/json"`` — extraction agents
        # silently return 0 facts. Clear the OpenAI-compat URL so dispatch
        # uses the native ``gemini`` provider path (the May-10 working
        # baseline). Never touch a manually-edited custom base_url.
        if (
            endpoint.preset == "google_ai"
            and endpoint.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"
        ):
            await endpoint_store.update(endpoint.id, base_url="")
            logger.info(
                "migrate_to_endpoint_catalog: cleared OpenAI-compat base_url on "
                "google_ai Endpoint id=%s (F11 self-heal)",
                endpoint.id,
            )
            endpoint.base_url = ""  # update local copy for subsequent passes

        if endpoint.models:
            # Operator-curated or previously backfilled — leave alone.
            continue
        seeded_models, seeded_kinds = _seeded_models_for_preset(endpoint.preset)
        if not seeded_models:
            # Non-catalog preset (Ollama, vLLM, OpenRouter, custom) — discovery
            # is still operator-driven for these because the model list is
            # operator-deployed, not centrally curated.
            continue
        await endpoint_store.update(
            endpoint.id,
            models=seeded_models,
            model_kinds=seeded_kinds if seeded_kinds else None,
        )
        backfilled += 1
        logger.info(
            "migrate_to_endpoint_catalog: backfilled %d catalog models for "
            "Endpoint preset=%s id=%s",
            len(seeded_models),
            endpoint.preset,
            endpoint.id,
        )
    if backfilled:
        logger.info(
            "migrate_to_endpoint_catalog: backfilled catalog models on %d endpoints",
            backfilled,
        )
    return backfilled


async def _repair_embedding_assignment(
    stores: Any,
    endpoint_store: EndpointStore,
    assignment_store: AssignmentStore,
    endpoints_by_preset: dict[str, str],
) -> tuple[bool, bool]:
    """Ensure the ``embedding`` Endpoint and Assignment match the legacy config.

    Always runs — both on a fresh migration (``endpoints_by_preset`` comes from
    the newly-created endpoints) and on the already-populated path (built from
    the existing catalog). Idempotent: skips creation/update when things are
    already correct.

    Args:
        stores: the store clients (used to call ``_resolve_legacy_embedding_config``).
        endpoint_store: for Endpoint CRUD.
        assignment_store: for Assignment CRUD.
        endpoints_by_preset: mapping of preset-key → endpoint_id, covering
            endpoints that are candidates to reuse for the embedding consumer.
            On a fresh-migration run this is ``endpoints_created``; on the
            already-populated run it is built from the existing catalog list.

    Returns:
        ``(endpoint_created, assignment_repaired)`` — both ``False`` when no
        legacy embedding signal exists or when things are already correct.
    """
    legacy = await _resolve_legacy_embedding_config(stores)
    if legacy is None:
        return False, False

    endpoint_created = False
    embedding_endpoint_id: str | None = endpoints_by_preset.get(legacy.preset)

    if embedding_endpoint_id is None:
        # No existing/newly-created Endpoint covers the legacy embedding preset.
        # Check whether a ``migrated-embedding-config``-tagged endpoint already
        # exists in the catalog (handles the re-run-on-populated path).
        all_endpoints = await endpoint_store.list()
        existing_migrated = next(
            (
                e
                for e in all_endpoints
                if e.preset == legacy.preset and _MIGRATED_EMBEDDING_TAG in e.tags
            ),
            None,
        )
        if existing_migrated is not None:
            embedding_endpoint_id = existing_migrated.id
        else:
            # Create a dedicated Endpoint from the legacy embedding config.
            # Ollama embedding needs no auth; otherwise auth depends on whether
            # we recovered a key.
            legacy_auth: AuthType = (
                "api_key" if (legacy.preset != "ollama" and legacy.api_key) else "none"
            )
            legacy_base_url = legacy.api_base or _BASE_URL_BY_PRESET.get(legacy.preset, "")
            try:
                embedding_ep = await endpoint_store.create(
                    name=f"{legacy.provider} (migrated embedding config)",
                    preset=legacy.preset,
                    base_url=legacy_base_url,
                    auth_type=legacy_auth,
                    plaintext_credential=(legacy.api_key if legacy_auth == "api_key" else None),
                    models=[legacy.model] if legacy.model else [],
                    rpm=None,
                    tags=[_MIGRATED_EMBEDDING_TAG],
                )
                embedding_endpoint_id = embedding_ep.id
                endpoint_created = True
                logger.info(
                    "migrate_to_endpoint_catalog: created embedding Endpoint preset=%s id=%s "
                    "from legacy embedding config",
                    legacy.preset,
                    embedding_ep.id,
                )
            except RuntimeError:
                # Master key unavailable AND the legacy embedding config has a
                # key we'd need to encrypt — skip-and-continue. (A no-auth
                # Ollama embedding endpoint never reaches this branch.)
                logger.debug(
                    "migrate_to_endpoint_catalog: master key unavailable; "
                    "cannot create embedding Endpoint for preset=%s",
                    legacy.preset,
                )

    if embedding_endpoint_id is None:
        return endpoint_created, False

    # Set / repair the ``embedding`` Assignment. Only write it when it's
    # missing OR currently mis-set (points at an Endpoint whose preset's
    # provider differs from the legacy embedding provider) — don't clobber
    # an operator's deliberate later change.
    legacy_litellm_provider = preset_to_provider(legacy.preset)
    existing_embedding = await assignment_store.get("embedding")
    should_write = True
    if existing_embedding is not None:
        current_ep = await endpoint_store.get(existing_embedding.endpoint_id)
        current_provider = preset_to_provider(current_ep.preset) if current_ep is not None else None
        if current_provider == legacy_litellm_provider:
            # Already points at the right provider — leave it.
            should_write = False

    if not should_write:
        return endpoint_created, False

    await assignment_store.upsert(
        Assignment(
            consumer="embedding",
            endpoint_id=embedding_endpoint_id,
            model=legacy.model or "jina-embeddings-v4",
            dimensions=legacy.dimensions,
            task=legacy.task,
        )
    )
    logger.info(
        "migrate_to_endpoint_catalog: set Assignment(embedding) -> preset=%s model=%s",
        legacy.preset,
        legacy.model,
    )
    return endpoint_created, True


async def migrate_to_endpoint_catalog(stores: Any) -> dict[str, Any]:
    """Hydrate the new collections from legacy data + env. Idempotent.

    Returns a summary dict for the caller to log. Two shapes:

    * Fresh migration (``endpoints`` was empty):
      ``{endpoints_created, assignments_created, skipped: None}``
    * Already populated (``endpoints`` was non-empty):
      ``{skipped: "endpoints_already_populated",
         embedding_endpoint_created: bool,
         embedding_assignment_repaired: bool}``
      Step 3 (legacy-embedding repair) always runs so a wrongly-migrated
      install self-heals on the next server boot.
    """
    endpoint_store = EndpointStore(stores.mongodb)
    assignment_store = AssignmentStore(stores.mongodb)

    existing_endpoints = await endpoint_store.list()
    if existing_endpoints:
        logger.info(
            "migrate_to_endpoint_catalog: %d endpoints already present — running embedding + catalog repair",
            len(existing_endpoints),
        )
        # Steps 1–2 (env-derived endpoint creation) and Step 4 (agent
        # Assignments) are skipped — they are idempotent only on a blank slate
        # and would produce duplicates on an already-populated catalog.
        # Step 3 (legacy-embedding repair) always runs: it is idempotent (checks
        # for a ``migrated-embedding-config`` endpoint before creating) and fixes
        # installs that previously had the ``embedding`` Assignment mis-pointed.
        # Self-heal: also backfill ``models[]`` for any catalog-preset endpoint
        # that was created with an empty list (pre-F9 installs) — this lets the
        # Settings UI populate its model dropdown without the operator clicking
        # "Re-Discover" on every endpoint.
        models_backfilled = await _backfill_catalog_models(endpoint_store, existing_endpoints)
        endpoints_by_preset = {e.preset: e.id for e in existing_endpoints}
        ep_created, assign_repaired = await _repair_embedding_assignment(
            stores, endpoint_store, assignment_store, endpoints_by_preset
        )
        return {
            "skipped": "endpoints_already_populated",
            "embedding_endpoint_created": ep_created,
            "embedding_assignment_repaired": assign_repaired,
            "models_backfilled": models_backfilled,
        }

    endpoints_created: dict[str, str] = {}  # preset → endpoint_id
    assignments_created = 0

    encryptor_unavailable = False

    # ── Step 1: synthesise Endpoints from env credentials ──────────────
    for preset, (env_var, _unused_preset_key, auth_type) in _ENV_VAR_BY_PROVIDER.items():
        env_value = os.environ.get(env_var, "").strip()
        if not env_value:
            continue
        # Pre-populate models[] + model_kinds from the curated catalog so the
        # Settings UI's "available models" dropdown isn't empty after a fresh
        # ./atlas install — the operator should see a working model picker
        # without having to click "Re-Discover" on every endpoint card.
        seeded_models, seeded_kinds = _seeded_models_for_preset(preset)
        try:
            endpoint = await endpoint_store.create(
                name=f"{preset} (from {env_var})",
                preset=preset,
                base_url=_BASE_URL_BY_PRESET.get(preset, ""),
                auth_type=auth_type,
                plaintext_credential=env_value,
                models=seeded_models,
                rpm=None,
                tags=["migrated-from-env"],
            )
            endpoints_created[preset] = endpoint.id
            # Persist model_kinds via the update path (create doesn't accept it)
            # so the per-model chat/embedding classification is available to the
            # UI immediately. Skipped when the catalog returned no entries (e.g.
            # Ollama, vLLM — operator-curated discovery still required).
            if seeded_kinds:
                await endpoint_store.update(endpoint.id, model_kinds=seeded_kinds)
            logger.info(
                "migrate_to_endpoint_catalog: created Endpoint preset=%s id=%s "
                "from %s with %d catalog models",
                preset,
                endpoint.id,
                env_var,
                len(seeded_models),
            )
        except RuntimeError:
            # CREDENTIAL_MASTER_KEY unconfigured — skip this provider but keep
            # going. A no-auth Ollama endpoint (Step 2) needs no encryption, so
            # it can still be created. We only report the "skipped" status at the
            # end if NOTHING was created.
            encryptor_unavailable = True
            logger.debug(
                "migrate_to_endpoint_catalog: master key unavailable, skipping %s",
                preset,
            )
            continue

    # ── Step 2: synthesise an Ollama Endpoint when enabled ──────────────
    if os.environ.get("OLLAMA_ENABLED", "").strip().lower() == "true":
        ollama_base = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
        if not ollama_base.endswith("/v1"):
            ollama_base = f"{ollama_base.rstrip('/')}/v1"
        ollama_ep = await endpoint_store.create(
            name="Ollama (local)",
            preset="ollama",
            base_url=ollama_base,
            auth_type="none",
            plaintext_credential=None,
            models=[],
            rpm=None,
            tags=["migrated-from-env", "local"],
        )
        endpoints_created["ollama"] = ollama_ep.id
        logger.info("migrate_to_endpoint_catalog: created Ollama Endpoint id=%s", ollama_ep.id)

    # ── Step 3: migrate the legacy embedding config → Endpoint + Assignment ─
    # The legacy embedding config can live in three places — the
    # ``embedding_settings`` DB doc, the ``secrets.embedding_api_key`` blob,
    # and/or the ``EMBEDDING_*`` env. None of those is an env-var credential
    # that Step 1 would have sniffed, so an install that configured embedding
    # via the legacy Settings UI (DB-stored key, not env) ends up with no
    # matching Endpoint here. Resolve it explicitly and ensure one exists.
    ep_created, assign_repaired = await _repair_embedding_assignment(
        stores, endpoint_store, assignment_store, endpoints_created
    )
    if ep_created:
        # The repair helper may have added a new endpoint — reflect that in the
        # endpoints_created count (preset already inserted into endpoints_by_preset
        # inside the helper, but endpoints_created here is a separate local dict
        # used only for the final summary count; increment via a sentinel key).
        endpoints_created.setdefault("__embedding_migrated__", "")
    if assign_repaired:
        assignments_created += 1

    # ── Step 4: migrate agent_model_config.models → 16 agent Assignments ──
    try:
        agent_doc = await stores.mongodb.db["agent_model_config"].find_one(
            {"_id": "agent_model_config"}
        )
    except Exception:  # noqa: BLE001
        agent_doc = None

    agent_overrides = (agent_doc or {}).get("models", {}) or {}
    for consumer in DEFAULT_CONSUMERS:
        if consumer == "embedding":
            continue  # handled above
        model_string = agent_overrides.get(consumer)
        if not model_string:
            # Fall back to env defaults for the legacy fast tier (gemini-2.5-flash).
            model_string = os.environ.get("LLM_FAST_MODEL") or "gemini-2.5-flash"

        # Figure out which Endpoint this model belongs to via prefix sniff.
        if model_string.startswith("ollama_chat/") or model_string.startswith("ollama/"):
            ep_preset = "ollama"
            model_bare = model_string.split("/", 1)[1] if "/" in model_string else model_string
        elif "/" in model_string:
            ep_preset_raw = model_string.split("/", 1)[0]
            # LiteLLM prefix → our Endpoint preset key.
            ep_preset = "google_ai" if ep_preset_raw == "gemini" else ep_preset_raw
            model_bare = model_string.split("/", 1)[1]
        else:
            # Bare "gemini-2.5-flash" → assume Google AI.
            ep_preset = "google_ai"
            model_bare = model_string

        target_ep_id = endpoints_created.get(ep_preset)
        if target_ep_id is None:
            # No matching endpoint synthesised — skip this consumer; operator
            # can configure manually in the UI.
            continue
        await assignment_store.upsert(
            Assignment(consumer=consumer, endpoint_id=target_ep_id, model=model_bare)
        )
        assignments_created += 1

    # Exclude the sentinel key from the count.
    real_endpoints_created = sum(1 for k in endpoints_created if not k.startswith("__"))
    if not endpoints_created and encryptor_unavailable:
        # We had env credentials to migrate but couldn't encrypt any of them,
        # and no no-auth endpoint was created either — report the skip so the
        # operator knows the legacy env path is still the only one wired.
        return {"skipped": "credential_encryptor_unavailable"}

    logger.info(
        "migrate_to_endpoint_catalog: created %d endpoints + %d assignments",
        real_endpoints_created,
        assignments_created,
    )
    return {
        "endpoints_created": real_endpoints_created,
        "assignments_created": assignments_created,
        "skipped": None,
    }


__all__ = ["migrate_to_endpoint_catalog"]
