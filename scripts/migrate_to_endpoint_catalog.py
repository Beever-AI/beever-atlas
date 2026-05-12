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

import logging
import os
from typing import Any

from beever_atlas.llm.assignments import (
    Assignment,
    AssignmentStore,
    DEFAULT_CONSUMERS,
)
from beever_atlas.llm.endpoints import AuthType, EndpointStore

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


# Single source of truth lives in ``llm/presets.py`` (derived from ENDPOINT_PRESETS).
from beever_atlas.llm.presets import BASE_URL_BY_PRESET as _BASE_URL_BY_PRESET


async def migrate_to_endpoint_catalog(stores: Any) -> dict[str, Any]:
    """Hydrate the new collections from legacy data + env. Idempotent.

    Returns a summary dict ``{endpoints_created, assignments_created, skipped}``
    for the caller to log. When ``endpoints`` is already populated, returns
    ``{skipped: "endpoints_already_populated"}`` without writes.
    """
    endpoint_store = EndpointStore(stores.mongodb)
    assignment_store = AssignmentStore(stores.mongodb)

    existing_endpoints = await endpoint_store.list()
    if existing_endpoints:
        logger.info(
            "migrate_to_endpoint_catalog: %d endpoints already present — skip",
            len(existing_endpoints),
        )
        return {"skipped": "endpoints_already_populated"}

    endpoints_created: dict[str, str] = {}  # preset → endpoint_id
    assignments_created = 0

    encryptor_unavailable = False

    # ── Step 1: synthesise Endpoints from env credentials ──────────────
    for preset, (env_var, _unused_preset_key, auth_type) in _ENV_VAR_BY_PROVIDER.items():
        env_value = os.environ.get(env_var, "").strip()
        if not env_value:
            continue
        try:
            endpoint = await endpoint_store.create(
                name=f"{preset} (from {env_var})",
                preset=preset,
                base_url=_BASE_URL_BY_PRESET.get(preset, ""),
                auth_type=auth_type,
                plaintext_credential=env_value,
                models=[],
                rpm=None,
                tags=["migrated-from-env"],
            )
            endpoints_created[preset] = endpoint.id
            logger.info(
                "migrate_to_endpoint_catalog: created Endpoint preset=%s id=%s from %s",
                preset,
                endpoint.id,
                env_var,
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

    # ── Step 3: migrate embedding_settings → embedding Assignment ──────
    try:
        embedding_doc = await stores.mongodb.db["embedding_settings"].find_one(
            {"_id": "embedding_settings"}
        )
    except Exception:  # noqa: BLE001
        embedding_doc = None

    if embedding_doc:
        provider = embedding_doc.get("provider") or "jina_ai"
        # Pre-shipped jina is under preset "jina_ai" — already covered if
        # JINA_API_KEY is set. If not, the embedding doc points at a provider
        # whose Endpoint we haven't created.
        target_ep_id = endpoints_created.get(provider)
        if target_ep_id is not None:
            assignment = Assignment(
                consumer="embedding",
                endpoint_id=target_ep_id,
                model=embedding_doc.get("model") or "jina-embeddings-v4",
                dimensions=embedding_doc.get("dimensions"),
                task=embedding_doc.get("task"),
            )
            await assignment_store.upsert(assignment)
            assignments_created += 1
            logger.info(
                "migrate_to_endpoint_catalog: migrated embedding_settings -> Assignment(embedding)"
            )

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

    if not endpoints_created and encryptor_unavailable:
        # We had env credentials to migrate but couldn't encrypt any of them,
        # and no no-auth endpoint was created either — report the skip so the
        # operator knows the legacy env path is still the only one wired.
        return {"skipped": "credential_encryptor_unavailable"}

    logger.info(
        "migrate_to_endpoint_catalog: created %d endpoints + %d assignments",
        len(endpoints_created),
        assignments_created,
    )
    return {
        "endpoints_created": len(endpoints_created),
        "assignments_created": assignments_created,
        "skipped": None,
    }


__all__ = ["migrate_to_endpoint_catalog"]
