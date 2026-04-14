"""SSRF-safe HTTP helpers.

Resolve the hostname up front, reject any DNS result that maps to a private /
link-local / loopback / cloud-metadata range, and pin the request to the
resolved IP while preserving the original Host header. Follow-redirects is
disabled so an attacker-controlled origin cannot re-target the request at a
private address on a second hop.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Iterable
from urllib.parse import urlparse, urlunparse

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


def _is_private(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return any(addr in net for net in _PRIVATE_NETS)


def resolve_and_validate(
    url: str, allowlist: Iterable[str] | None = None
) -> tuple[str, str]:
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
        if host not in allow_set:
            raise PermissionError(f"host {host} not in allowlist")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
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
    async with httpx.AsyncClient(
        verify=True, follow_redirects=False, timeout=timeout
    ) as client:
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
    async with httpx.AsyncClient(
        verify=True, follow_redirects=False, timeout=timeout
    ) as client:
        return await client.post(pinned, headers=headers, **kw)
