"""ADK agent definitions for Beever Atlas.

Exports `root_agent` which is the entry point for the ADK Runner.
Currently uses the echo agent for pipeline validation (M2).
Will be replaced by the real query_router_agent in M3/M4.
"""

from beever_atlas.agents.echo import echo_agent

# The root agent is what the ADK Runner invokes.
# Swap this import to switch from echo to real agent.
root_agent = echo_agent
