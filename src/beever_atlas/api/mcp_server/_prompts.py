"""Atlas MCP prompts (Phase 4, tasks 4.6–4.8)."""

from __future__ import annotations

from fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """Register static prompt templates for common Atlas workflows."""

    # 4.6 summarize_channel
    @mcp.prompt(name="summarize_channel")
    def summarize_channel(channel_id: str, since_days: int = 7) -> list[dict]:
        """Produce a user-role instruction to summarize recent channel discussion.

        Parameters:
            channel_id: The channel id to summarize (from list_channels).
            since_days: Look-back window in days (default 7).
        """
        return [
            {
                "role": "user",
                "content": (
                    f"Summarize the last {since_days} days of discussion in #{channel_id}. "
                    "Focus on decisions made, open questions, and key participants. "
                    "Use get_wiki_page(page_type='activity') and get_recent_activity "
                    "to ground your answer."
                ),
            }
        ]

    # 4.7 investigate_decision
    @mcp.prompt(name="investigate_decision")
    def investigate_decision(channel_id: str, topic: str) -> list[dict]:
        """Produce a decision-trace-style instruction for investigating a topic.

        Parameters:
            channel_id: The channel id to investigate (from list_channels).
            topic: The decision or topic to trace (e.g. 'database choice').
        """
        return [
            {
                "role": "user",
                "content": (
                    f"Trace the decision history for '{topic}' in channel {channel_id}. "
                    "Use trace_decision_history for the SUPERSEDES chain, find_experts "
                    "to identify who drove the decision, and search_channel_facts to "
                    "ground individual claims."
                ),
            }
        ]

    # 4.8 onboard_new_channel
    @mcp.prompt(name="onboard_new_channel")
    def onboard_new_channel(channel_id: str) -> list[dict]:
        """Produce an onboarding-overview instruction for a new channel.

        Parameters:
            channel_id: The channel id to onboard (from list_channels).
        """
        return [
            {
                "role": "user",
                "content": (
                    f"Give me an onboarding overview of channel {channel_id}. "
                    "Call get_wiki_page(page_type='overview') first, then "
                    "get_wiki_page(page_type='people') and (page_type='topics'). "
                    "Summarize scope, key people, active topics, and recent decisions."
                ),
            }
        ]
