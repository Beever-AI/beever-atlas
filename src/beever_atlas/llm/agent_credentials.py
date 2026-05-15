"""Process-local credential cache, keyed on Endpoint UUID.

Boot decrypts each Endpoint document once via
:func:`beever_atlas.llm.endpoints.decrypt_endpoint_credential` and stores the
plaintext in this module's ``_runtime`` dict. Dispatch reads from here per
call — never decrypts in the hot path.

Design D3 + D6: the dict lives only in process memory; no serialisation to
disk, response bodies, or log lines. ``set_runtime_credential`` is the hot-
reload path called by the PUT endpoint after a write.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Module-private. Keyed by Endpoint UUID; value is whatever
# ``decrypt_endpoint_credential`` returns (str for api_key, dict for IAM/SA,
# None for ``auth_type=none``).
_runtime: dict[str, Any] = {}


def set_runtime_credential(endpoint_id: str, value: Any) -> None:
    """Set or replace the cached plaintext credential for an Endpoint.

    Called from the PUT endpoint after a write so the next dispatch call
    uses the new credential without a restart. Passing ``None`` clears the
    entry (mirrors the ``auth_type=none`` semantics).
    """
    if value is None:
        _runtime.pop(endpoint_id, None)
    else:
        _runtime[endpoint_id] = value


def get_runtime_credential(endpoint_id: str) -> Any:
    """Return the cached plaintext credential, or ``None`` if absent.

    Absence means either the Endpoint has ``auth_type=none`` or boot
    hydration hasn't been run for this process yet. Callers MUST treat
    ``None`` as "no credential available" and fall back to LiteLLM's env-
    based defaults (which on a vanilla install with ``OPENAI_API_KEY`` etc.
    set in env will Just Work).
    """
    return _runtime.get(endpoint_id)


def clear_all_runtime_credentials() -> None:
    """Reset the cache. Used by tests and by lifespan teardown."""
    _runtime.clear()


def runtime_credential_count() -> int:
    """Number of endpoints with a cached plaintext credential. Test helper."""
    return len(_runtime)


async def hydrate_runtime_credentials(stores: Any) -> int:
    """Load every Endpoint document, decrypt the credential, populate cache.

    Idempotent — clears the cache before repopulating. Safe to call multiple
    times. Returns the number of endpoints loaded.

    Defensive: per-Endpoint decryption failures log at WARNING and skip; one
    bad envelope does not block server boot.
    """
    from beever_atlas.llm.endpoints import EndpointStore, decrypt_endpoint_credential

    clear_all_runtime_credentials()
    store = EndpointStore(stores.mongodb)
    endpoints = await store.list()
    loaded = 0
    for endpoint in endpoints:
        if endpoint.encrypted_key is None:
            # auth_type=none — nothing to cache.
            continue
        try:
            plaintext = decrypt_endpoint_credential(endpoint.encrypted_key)
        except Exception as exc:  # noqa: BLE001 — never crash boot on a bad envelope
            # Log the exception class + message only, NOT a full traceback —
            # frames from the decrypt path hold ciphertext (and potentially
            # partially-decrypted plaintext) in locals.
            logger.warning(
                "agent_credentials: failed to decrypt endpoint id=%s name=%s — skipping (%s: %s)",
                endpoint.id,
                endpoint.name,
                type(exc).__name__,
                exc,
            )
            continue
        if plaintext is not None:
            _runtime[endpoint.id] = plaintext
            loaded += 1
    logger.info(
        "agent_credentials: hydrated %d/%d endpoint credentials at boot",
        loaded,
        len(endpoints),
    )
    return loaded


__all__ = [
    "set_runtime_credential",
    "get_runtime_credential",
    "clear_all_runtime_credentials",
    "runtime_credential_count",
    "hydrate_runtime_credentials",
]
