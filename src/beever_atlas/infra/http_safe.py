"""SSRF-safe HTTP helpers.

Resolve the hostname up front, reject any DNS result that maps to a private /
link-local / loopback / cloud-metadata range, and pin the request to the
resolved IP while preserving the original Host header. Follow-redirects is
disabled so an attacker-controlled origin cannot re-target the request at a
private address on a second hop.

`validate_proxy_url` layers a second defence for the file-proxy paths
(security findings H2, H3): any user-supplied URL must land on a known
platform host (suffix-match supported for tenant subdomains like
``*.sharepoint.com``), and the URL is percent-encoded before being
concatenated into the internal bridge URL.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Iterable
from urllib.parse import quote, urlparse, urlunparse

import httpx

_PRIVATE_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "0.0.0.0/8",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "169.254.169.254/32",
    )
)


# Default allowlist of platform file hosts that `validate_proxy_url`
# accepts. Entries prefixed with ``suffix:`` match any hostname that
# ends with the suffix (used for tenant-specific subdomains such as
# ``<tenant>.sharepoint.com``). The set is overridable at runtime via
# the ``FILE_PROXY_HOST_ALLOWLIST`` environment variable
# (comma-separated full replacement).
PLATFORM_HOST_ALLOWLIST: frozenset[str] = frozenset(
    {
        "files.slack.com",
        "cdn.discordapp.com",
        "api.telegram.org",
        "files.mattermost.com",
        "graph.microsoft.com",
        "suffix:.sharepoint.com",
        "suffix:.slack-edge.com",
    }
)


def _parse_env_allowlist(raw: str) -> frozenset[str]:
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _host_matches(host: str, entry: str) -> bool:
    if entry.startswith("suffix:"):
        suffix = entry[len("suffix:") :]
        # Reject bare suffix match (e.g. "sharepoint.com" against
        # "suffix:.sharepoint.com") — the suffix MUST include the
        # leading dot so an attacker cannot register ``attackersharepoint.com``.
        return host.endswith(suffix) and host != suffix.lstrip(".")
    return host == entry


def _active_allowlist(
    override: Iterable[str] | None,
) -> frozenset[str]:
    if override is not None:
        return frozenset(override)
    env_value = os.environ.get("FILE_PROXY_HOST_ALLOWLIST", "").strip()
    if env_value:
        return _parse_env_allowlist(env_value)
    return PLATFORM_HOST_ALLOWLIST


def _is_private(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return any(addr in net for net in _PRIVATE_NETS)


def resolve_and_validate(url: str, allowlist: Iterable[str] | None = None) -> tuple[str, str]:
    """Resolve `url` and return (pinned_url, original_host).

    Raises ValueError for a malformed URL or missing DNS result, and
    PermissionError when the host is not on the allowlist or resolves
    to a private address.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("missing host")
    if allowlist is not None:
        allow_set = set(allowlist)
        if not any(_host_matches(host, entry) for entry in allow_set):
            raise PermissionError(f"host {host} not in allowlist")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        # Honour the docstring contract: NXDOMAIN / DNS errors surface as
        # ValueError("no DNS result"), not as a leaked socket.gaierror /
        # OSError. Callers (proxy_media, validate_proxy_url) already handle
        # ValueError; wrapping here keeps the helper's exception surface
        # consistent across all call sites.
        raise ValueError(f"DNS resolution failed: {exc}") from exc
    ips: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if isinstance(addr, str):
            ips.add(addr)
    if not ips:
        raise ValueError("no DNS result")
    for ip in ips:
        if _is_private(ip):
            raise PermissionError(f"resolved IP {ip} is private")

    pinned_ip: str = next(iter(ips))
    if ":" in pinned_ip:
        new_netloc = f"[{pinned_ip}]:{port}"
    else:
        new_netloc = f"{pinned_ip}:{port}"
    pinned_url = urlunparse(parsed._replace(netloc=new_netloc))
    return pinned_url, host


def _merge_host_header(headers: dict[str, str] | None, host: str) -> dict[str, str]:
    merged = dict(headers or {})
    merged["Host"] = host
    return merged


async def safe_get(
    url: str,
    *,
    allowlist: Iterable[str] | None = None,
    timeout: float = 30.0,
    **kw,
) -> httpx.Response:
    pinned, host = resolve_and_validate(url, allowlist)
    headers = _merge_host_header(kw.pop("headers", None), host)
    async with httpx.AsyncClient(verify=True, follow_redirects=False, timeout=timeout) as client:
        return await client.get(pinned, headers=headers, **kw)


async def safe_post(
    url: str,
    *,
    allowlist: Iterable[str] | None = None,
    timeout: float = 30.0,
    **kw,
) -> httpx.Response:
    pinned, host = resolve_and_validate(url, allowlist)
    headers = _merge_host_header(kw.pop("headers", None), host)
    async with httpx.AsyncClient(verify=True, follow_redirects=False, timeout=timeout) as client:
        return await client.post(pinned, headers=headers, **kw)


def validate_proxy_url(url: str, allowlist: Iterable[str] | None = None) -> str:
    """Validate a user-supplied file URL before forwarding to the bridge.

    Confirms the host is on the platform allowlist AND does not resolve
    to a private / link-local / cloud-metadata IP, then percent-encodes
    the original URL so it can be safely f-string concatenated into the
    internal bridge proxy URL without letting an attacker inject
    ``&connection_id=…`` or ``#fragment`` parameters.

    Raises ``ValueError`` for a malformed URL and ``PermissionError``
    when the host is off-allowlist or resolves privately.

    This is the mitigation for security findings H2 (``/api/files/proxy``)
    and H3 (``media_processor._download_file``). Callers MUST use the
    returned percent-encoded string, never the raw input.
    """
    active = _active_allowlist(allowlist)
    # ``resolve_and_validate`` does DNS + IP-class rejection; our allowlist
    # handling here is layered on top so suffix-match entries work.
    resolve_and_validate(url, active)
    return quote(url, safe="")
