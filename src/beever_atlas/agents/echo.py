"""Echo agent for validating the ADK pipeline without memory stores.

This is a minimal LlmAgent that receives a question and echoes it back
with mock metadata. It validates the full ADK Runner → SSE streaming path.
Replace with the real query_router_agent in M3/M4.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent

_DEFAULT_FAST_MODEL = "gemini/gemini-2.0-flash-lite"
_DEFAULT_QUALITY_MODEL = "gemini/gemini-2.0-flash"


def _get_model(env_var: str, default: str) -> str:
    return os.environ.get(env_var, default)


echo_agent = LlmAgent(
    name="query_router_agent",
    description="Echo agent that validates the ADK pipeline. Returns the user's question with mock metadata.",
    model=_get_model("LLM_FAST_MODEL", _DEFAULT_FAST_MODEL),
    instruction="""You are a pipeline validation echo agent for Beever Atlas.

Your job is simple: echo the user's question back in a structured format.
This validates that the full ADK Runner → SSE streaming → response path works.

When you receive a question, respond with EXACTLY this format:

**Echo Response**

> [repeat the user's question here]

**Metadata**
- Route: echo
- Confidence: 1.0
- Cost: $0.00

This is a test response from the echo agent. In future milestones, this agent
will be replaced by the real query router that searches semantic and graph memory.
""",
)
