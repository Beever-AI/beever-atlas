"""ExternalMCPRegistry: load and wrap outbound MCP server tools for the QA agent.

Reads EXTERNAL_MCP_SERVERS env var (JSON array of {name, url, auth_token})
at startup, connects to each server, and returns ADK-compatible tool functions.

Each unreachable server is logged as a warning and skipped — startup is
non-blocking and never raises.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _MCPServerConfig:
    name: str
    url: str
    auth_token: str = ""


@dataclass
class ExternalMCPRegistry:
    """Registry of outbound MCP server connections.

    Usage::

        registry = ExternalMCPRegistry.from_env()
        await registry.connect()
        extra_tools = registry.tools  # list of async callables
    """

    _configs: list[_MCPServerConfig] = field(default_factory=list)
    _tools: list[Any] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> ExternalMCPRegistry:
        """Parse EXTERNAL_MCP_SERVERS env var and return a registry instance.

        Env var format (JSON array)::

            [{"name": "myserver", "url": "http://host:port", "auth_token": "secret"}]

        An empty or missing env var returns a no-op registry.
        """
        import os

        raw = os.environ.get("EXTERNAL_MCP_SERVERS", "").strip()
        if not raw:
            return cls(_configs=[])

        try:
            entries = json.loads(raw)
            if not isinstance(entries, list):
                raise ValueError("EXTERNAL_MCP_SERVERS must be a JSON array")
            configs = [
                _MCPServerConfig(
                    name=e["name"],
                    url=e["url"],
                    auth_token=e.get("auth_token", ""),
                )
                for e in entries
            ]
            logger.info("ExternalMCPRegistry: parsed %d server configs", len(configs))
            return cls(_configs=configs)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning(
                "ExternalMCPRegistry: failed to parse EXTERNAL_MCP_SERVERS (%s) — no external MCP tools loaded",
                exc,
            )
            return cls(_configs=[])

    async def connect(self) -> None:
        """Connect to each configured MCP server and wrap its tools.

        Unreachable servers are skipped with a warning — never raises.
        """
        if not self._configs:
            logger.debug("ExternalMCPRegistry: no servers configured, skipping connect")
            return

        for cfg in self._configs:
            try:
                await self._connect_one(cfg)
            except Exception as exc:
                logger.warning(
                    "ExternalMCPRegistry: could not connect to %r at %s (%s) — skipping",
                    cfg.name,
                    cfg.url,
                    exc,
                )

    async def _connect_one(self, cfg: _MCPServerConfig) -> None:
        """Connect to a single MCP server and register its tools."""
        import httpx

        headers: dict[str, str] = {}
        if cfg.auth_token:
            headers["Authorization"] = f"Bearer {cfg.auth_token}"

        # Probe the server — fetch its tool list via MCP initialize handshake
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{cfg.url.rstrip('/')}/tools",
                headers=headers,
            )
            resp.raise_for_status()
            tools_data = resp.json()

        tool_names: list[str] = []
        for tool_def in tools_data.get("tools", []):
            tool_name = tool_def.get("name", "")
            if not tool_name:
                continue

            # Wrap each remote tool as an async callable that POSTs to the MCP server
            wrapped = _make_remote_tool(
                server_name=cfg.name,
                server_url=cfg.url,
                tool_name=tool_name,
                description=tool_def.get("description", ""),
                headers=headers,
            )
            self._tools.append(wrapped)
            tool_names.append(tool_name)

        logger.info(
            "ExternalMCPRegistry: connected to %r, registered tools: %s",
            cfg.name,
            tool_names,
        )

    @property
    def tools(self) -> list[Any]:
        """Return all successfully loaded external tool callables."""
        return list(self._tools)

    @property
    def is_empty(self) -> bool:
        return len(self._tools) == 0


def _make_remote_tool(
    server_name: str,
    server_url: str,
    tool_name: str,
    description: str,
    headers: dict[str, str],
) -> Any:
    """Return an async function that calls a remote MCP tool via HTTP POST.

    The returned function is named after the remote tool and includes the
    description in its docstring so ADK can use it for routing.
    """
    import httpx

    url = f"{server_url.rstrip('/')}/tools/{tool_name}"

    async def remote_tool(**kwargs: Any) -> Any:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers=headers, json=kwargs)
            resp.raise_for_status()
            return resp.json()

    remote_tool.__name__ = f"{server_name}__{tool_name}"
    remote_tool.__doc__ = f"[External MCP: {server_name}] {description}"
    return remote_tool


# Module-level singleton — populated during app startup
_registry: ExternalMCPRegistry | None = None


async def init_mcp_registry() -> ExternalMCPRegistry:
    """Initialize the module-level registry from env at app startup."""
    global _registry
    _registry = ExternalMCPRegistry.from_env()
    await _registry.connect()
    return _registry


def get_mcp_registry() -> ExternalMCPRegistry:
    """Return the initialized registry, or an empty no-op instance."""
    if _registry is None:
        return ExternalMCPRegistry()
    return _registry
