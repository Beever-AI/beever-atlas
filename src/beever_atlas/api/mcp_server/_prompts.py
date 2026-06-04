"""Atlas MCP prompts (Phase 4, tasks 4.6–4.8)."""

from __future__ import annotations

from fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    """Register static prompt templates for common Atlas workflows."""

    # 4.6 summarize_channel
    @mcp.prompt(name="summarize_channel")
    def summarize_channel(channel_id: str, since_days: int = 7) -> list[dict]:
        """Recap recent activity in one channel.

        Use this prompt when a user asks "what happened in #channel lately?" or
        wants a standup-style digest. It returns a single user-role message that
        instructs the agent to summarize decisions, open questions, and key
        participants over the look-back window, grounded with get_recent_activity
        and the activity wiki page. It performs no retrieval itself — the caller's
        agent runs the named tools.

        Parameters:
            channel_id: Channel to summarize. Get a valid id from list_channels
                (e.g. "C12345").
            since_days: Look-back window in days. Default 7; use a larger value
                for a broader recap.
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
        """Trace how a decision was made in one channel.

        Use this prompt when a user asks "why did we decide X?" or "what's the
        history behind X?". It returns a single user-role message that directs
        the agent to reconstruct the decision's SUPERSEDES chain with
        trace_decision_history, identify who drove it with find_experts, and
        ground each claim with search_channel_facts. The prompt itself does no
        retrieval — the caller's agent runs the named tools.

        Parameters:
            channel_id: Channel to investigate. Get a valid id from list_channels
                (e.g. "C12345").
            topic: The decision or subject to trace, in natural language
                (e.g. "database choice", "auth provider migration").
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
        """Orient someone joining a channel for the first time.

        Use this prompt when a user asks "get me up to speed on #channel" or
        "what is this channel about?". It returns a single user-role message that
        walks the agent through the overview, people, and topics wiki pages and
        asks it to summarize the channel's scope, key people, active topics, and
        recent decisions. The prompt itself does no retrieval — the caller's
        agent runs the named tools.

        Parameters:
            channel_id: Channel to onboard into. Get a valid id from
                list_channels (e.g. "C12345").
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
