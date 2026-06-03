"""Standalone stdio entrypoint for the Beever Atlas MCP server.

``python -m beever_atlas.api.mcp_server`` serves the curated MCP surface over
stdio (JSON-RPC on stdin/stdout) with no HTTP server and no backing stores.

Why this exists:

- MCP registries (Glama.ai) and local launchers (Claude Desktop, ``mcp-proxy``)
  introspect a server by spawning its process and driving the MCP handshake
  (``initialize`` -> ``tools/list``). The production deployment serves MCP over
  streamable-HTTP at ``/mcp`` behind Bearer auth (``server/app.py``), which an
  anonymous registry sandbox can never reach — and the full server requires
  MongoDB/Weaviate/Neo4j/Redis at startup besides.
- ``build_mcp()`` registers tools lazily: no store is touched until a tool is
  *invoked*. The catalog (tools, prompts, resources) is therefore fully
  introspectable with zero external dependencies, which is exactly what
  registry checks need.

Tool calls that require backing stores return structured errors in this mode;
point clients at a full deployment's ``/mcp`` mount for real data.

Auth note: stdio is a local, single-principal transport — the process inherits
the invoker's privileges and exposes no network surface, so the ASGI
``MCPAuthMiddleware`` (HTTP-only by construction) is intentionally not
involved. With no stores connected there is also no data to protect.
"""

from __future__ import annotations

from beever_atlas.api.mcp_server import build_mcp


def main() -> None:
    """Run the MCP server on stdio.

    ``show_banner=False`` keeps the process output strictly JSON-RPC; the
    FastMCP banner targets stderr but registry sandboxes are conservative
    about any non-protocol output.
    """
    mcp = build_mcp()
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
