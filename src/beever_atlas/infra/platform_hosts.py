"""Derive the file-proxy host allowlist from active platform connections.

The connection wizard already collects each platform's server URL
(``base_url`` for Mattermost, ``server_url`` for self-hosted variants).
This module reads those URLs back out at server startup — and after
any connection CRUD — and registers the hostnames into:

  * ``infra.http_safe._runtime_hosts`` — for the wiki-side
    ``/api/files/proxy`` route.
  * ``api.media._RUNTIME_HOSTS`` — for the entity-graph
    ``/api/media/proxy`` route.

This removes the need for operators to also set
``FILE_PROXY_HOST_ALLOWLIST_EXTRA``: the source of truth is the existing
connection config. Adding a self-hosted Mattermost via the UI now
automatically grants its host file-proxy coverage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    # Avoid a hard runtime dep on the stores package — the helper is
    # called with the already-initialised `Stores` instance from the
    # FastAPI lifespan and connection CRUD paths.
    from beever_atlas.stores import StoreClients

logger = logging.getLogger(__name__)


# Credential keys that hold a server URL across all known platforms.
# The wizard uses ``base_url`` for Mattermost and self-hosted setups;
# other connectors may use ``server_url`` or ``instance_url`` so we
# accept any of the three.
_URL_CREDENTIAL_KEYS: tuple[str, ...] = ("base_url", "server_url", "instance_url")


def _hostname_of(value: object) -> str | None:
    """Return the lowercase hostname of ``value`` if it looks like an
    HTTP(S) URL, otherwise None. Skips empty strings, malformed URLs,
    and non-string types so credentials with mixed shapes (Slack tokens,
    bot IDs) are silently ignored."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlparse(value.strip())
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    return host or None


async def derive_proxy_hosts_from_connections(stores: "StoreClients") -> set[str]:
    """Iterate active connections, decrypt credentials, return the unique
    set of server hostnames.

    Disconnected / errored connections are skipped — operators who pause
    a connection should not retain its host in the allowlist. Decryption
    failures are logged but never raised so a single corrupt row cannot
    take down server startup.
    """
    hosts: set[str] = set()
    try:
        connections = await stores.platform.list_connections()
    except Exception:
        logger.exception("derive_proxy_hosts: failed to list connections")
        return hosts

    for conn in connections:
        if conn.status != "connected":
            continue
        try:
            creds = stores.platform.decrypt_connection_credentials(conn)
        except Exception:
            logger.warning(
                "derive_proxy_hosts: decrypt failed for conn=%s platform=%s",
                conn.id,
                conn.platform,
            )
            continue
        if not isinstance(creds, dict):
            continue
        for key in _URL_CREDENTIAL_KEYS:
            host = _hostname_of(creds.get(key))
            if host:
                hosts.add(host)
    return hosts


async def refresh_runtime_proxy_hosts(stores: "StoreClients") -> frozenset[str]:
    """Derive hosts from connections and register them into both proxy
    allowlists. Returns the registered set so callers can log / inspect.
    Safe to call repeatedly — registration is replace-not-union so a
    deleted connection's host falls out on the next refresh.
    """
    # Lazy imports — these modules pull FastAPI/httpx at import time and
    # the helper is occasionally called from contexts (tests, scripts)
    # that don't want those side effects up-front.
    from beever_atlas.api.media import register_runtime_media_hosts
    from beever_atlas.infra.http_safe import register_runtime_hosts

    hosts = await derive_proxy_hosts_from_connections(stores)
    register_runtime_hosts(hosts)
    register_runtime_media_hosts(hosts)
    logger.info(
        "proxy allowlist: registered %d runtime host(s) from connections: %s",
        len(hosts),
        sorted(hosts),
    )
    return frozenset(hosts)
