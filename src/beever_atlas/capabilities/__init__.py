"""Framework-neutral capability layer for Beever Atlas.

Each module in this package provides business logic that is independent of
the HTTP/ADK/MCP transport layer:

- ``connections`` — list platform connections and their selected channels
- ``memory``      — Q&A history search, channel facts, media refs, activity
- ``wiki``        — wiki-page retrieval and refresh
- ``graph``       — entity relationships, expert ranking, decision history
- ``sync``        — trigger channel syncs (with cooldown enforcement)
- ``jobs``        — job-status lookup with ownership check
- ``errors``      — typed domain exceptions (ChannelAccessDenied, etc.)
"""
