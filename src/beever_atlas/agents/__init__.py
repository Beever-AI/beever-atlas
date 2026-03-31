"""Agent catalog for Beever Atlas."""
from __future__ import annotations

_root_agent = None


def get_root_agent():
    """Lazy-create the root agent (defers LLM provider access to runtime)."""
    global _root_agent
    if _root_agent is None:
        from beever_atlas.agents.query.echo import create_echo_agent
        _root_agent = create_echo_agent()
    return _root_agent
