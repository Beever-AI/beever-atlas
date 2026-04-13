"""ReAct-style QA agent for answering channel questions with grounded citations."""

from __future__ import annotations

import logging

from google.adk.agents import LlmAgent

from beever_atlas.agents.query.prompts import (
    build_qa_system_prompt,
    QA_QUICK_SUFFIX,
    QA_SUMMARIZE_SUFFIX,
)
from beever_atlas.agents.tools import QA_TOOLS

logger = logging.getLogger(__name__)

# Tool subsets for each answer mode
_WIKI_TOOLS_NAMES = {"get_wiki_page", "get_topic_overview"}
_SUMMARIZE_TOOLS_NAMES = {"get_wiki_page", "get_topic_overview", "search_channel_facts", "search_qa_history"}

# Cached agent instances keyed on (mode, citation_registry_enabled, qa_new_prompt) so
# flipping either flag at runtime produces a freshly-built agent.
_agents: dict[tuple[str, bool, bool], LlmAgent] = {}


def _current_registry_flag() -> bool:
    try:
        from beever_atlas.infra.config import get_settings
        return bool(get_settings().citation_registry_enabled)
    except Exception:
        return False


def _current_new_prompt_flag() -> bool:
    try:
        from beever_atlas.infra.config import get_settings
        return bool(get_settings().qa_new_prompt)
    except Exception:
        return False


def _get_tools_by_names(names: set[str]) -> list:
    """Filter QA_TOOLS to only those matching the given function names."""
    return [t for t in QA_TOOLS if getattr(t, '__name__', getattr(t, 'name', '')) in names or
            (hasattr(t, 'func') and getattr(t.func, '__name__', '') in names)]


def _maybe_add_follow_ups_tool(tools_list: list, include_follow_ups: bool) -> list:
    """Append the `suggest_follow_ups` ADK tool when the citation registry
    flag is on and the mode opts into follow-ups. Flag-off path is
    untouched and keeps using the legacy prose-JSON FOLLOW_UPS regex.
    """
    if not include_follow_ups:
        return tools_list
    try:
        from beever_atlas.infra.config import get_settings
        if not get_settings().citation_registry_enabled:
            return tools_list
    except Exception:
        return tools_list
    from beever_atlas.agents.query.follow_ups_tool import suggest_follow_ups
    return [*tools_list, suggest_follow_ups]


def create_qa_agent(mode: str = "deep") -> LlmAgent:
    """Create a QA LlmAgent for the specified answer mode.

    Args:
        mode: "quick", "deep", or "summarize"

    Returns:
        LlmAgent configured for the specified mode.
    """
    from beever_atlas.llm.provider import get_llm_provider
    from beever_atlas.agents.mcp_registry import get_mcp_registry

    provider = get_llm_provider()
    model = provider.resolve_model("qa_agent")
    registry = get_mcp_registry()

    if mode == "quick":
        # Quick: 2 tools, no thinking, concise prompt
        tools_list = [t for t in QA_TOOLS if getattr(t, '__name__', '') in _WIKI_TOOLS_NAMES]
        prompt = build_qa_system_prompt(max_tool_calls=2, include_follow_ups=False, mode="quick") + QA_QUICK_SUFFIX
        agent = LlmAgent(
            name="qa_agent_quick",
            model=model,
            instruction=prompt,
            tools=tools_list,
        )
    elif mode == "summarize":
        # Summarize: 4 tools, thinking, structured output
        tools_list = [t for t in QA_TOOLS if getattr(t, '__name__', '') in _SUMMARIZE_TOOLS_NAMES]
        tools_list = [*tools_list, *registry.tools]
        tools_list = _maybe_add_follow_ups_tool(tools_list, include_follow_ups=True)
        prompt = build_qa_system_prompt(max_tool_calls=4, include_follow_ups=True, mode="summarize") + QA_SUMMARIZE_SUFFIX
        planner = _create_thinking_planner()
        agent = LlmAgent(
            name="qa_agent_summarize",
            model=model,
            instruction=prompt,
            tools=tools_list,
            planner=planner,
        )
    else:
        # Deep (default): all tools, thinking, full pipeline
        all_tools = [*QA_TOOLS, *registry.tools]
        all_tools = _maybe_add_follow_ups_tool(all_tools, include_follow_ups=True)
        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=True)
        planner = _create_thinking_planner()
        agent = LlmAgent(
            name="qa_agent_deep",
            model=model,
            instruction=prompt,
            tools=all_tools,
            planner=planner,
        )

    logger.info(
        "QA agent created: mode=%s model=%s tools=%d",
        mode,
        model if isinstance(model, str) else type(model).__name__,
        len(agent.tools) if hasattr(agent, 'tools') and agent.tools else 0,
    )
    return agent


def _create_thinking_planner():
    """Create a BuiltInPlanner with ThinkingConfig for Gemini thinking support.

    Returns None if the required classes are not available (older ADK versions).
    """
    try:
        from google.adk.planners import BuiltInPlanner
        from google.genai import types
        return BuiltInPlanner(
            thinking_config=types.ThinkingConfig(
                include_thoughts=True,
                thinking_budget=8192,
            )
        )
    except (ImportError, AttributeError):
        logger.warning("BuiltInPlanner or ThinkingConfig not available — thinking disabled")
        return None


def get_agent_for_mode(mode: str = "deep") -> LlmAgent:
    """Get or create a cached QA agent for the specified mode.

    Cache key is `(mode, citation_registry_enabled)` so flipping the
    registry flag at runtime produces a new agent with the correct
    tool-set — avoids stale prompt/tool mismatch during rollout.
    """
    key = (mode, _current_registry_flag(), _current_new_prompt_flag())
    if key not in _agents:
        _agents[key] = create_qa_agent(mode)
    return _agents[key]


def get_root_agent() -> LlmAgent:
    """Get the default (deep) QA agent. Backward-compatible entry point."""
    return get_agent_for_mode("deep")
