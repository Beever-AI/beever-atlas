"""ReAct-style QA agent for answering channel questions with grounded citations."""

from __future__ import annotations

import logging

from google.adk.agents import LlmAgent

from beever_atlas.agents.prompt_safety import UNTRUSTED_SYSTEM_NOTE
from beever_atlas.agents.query.prompts import (
    build_qa_system_prompt,
    QA_QUICK_SUFFIX,
    QA_SUMMARIZE_SUFFIX,
)
from beever_atlas.agents.tools import QA_TOOLS
from beever_atlas.infra.config import ConfigurationError


# Tool name fragments that indicate write or network-egress capability.
# When an agent context includes untrusted content, these tools are filtered
# out as an output-side defense against prompt-injection-driven exfiltration.
_UNTRUSTED_TOOL_DENYLIST_FRAGMENTS = (
    "tavily",
    "web_search",
    "write",
    "create",
    "update",
    "delete",
    "send",
    "post",
)


def _filter_tools_for_untrusted(tools: list) -> list:
    """Drop write/egress tools when the prompt contains untrusted content.

    Defense-in-depth: a prompt-injection payload in retrieved memory could
    instruct the model to exfiltrate data via web search or mutate state
    via MCP write tools. Restrict to read-only tools in that context.
    """
    kept: list = []
    for t in tools:
        name = (
            getattr(t, "__name__", None)
            or getattr(t, "name", None)
            or getattr(getattr(t, "func", None), "__name__", "")
            or ""
        ).lower()
        if any(frag in name for frag in _UNTRUSTED_TOOL_DENYLIST_FRAGMENTS):
            continue
        kept.append(t)
    return kept

logger = logging.getLogger(__name__)


def _tool_name(tool) -> str:
    """Best-effort extraction of a callable tool's __name__."""
    return (
        getattr(tool, "__name__", None)
        or getattr(tool, "name", None)
        or getattr(getattr(tool, "func", None), "__name__", "")
    )


def _maybe_wrap_with_skills(tools_list: list) -> list:
    """If `qa_skills_enabled` is on, wrap `tools_list` in a `SkillToolset`
    carrying only skills whose `allowed_tools` are a subset of the enabled
    tool names. Skills with no `allowed_tools` (pure-formatting) always pass.

    Precondition: `qa_skills_enabled` requires `qa_new_prompt=True`.
    Returns the original `tools_list` unchanged when the flag is off.
    """
    try:
        from beever_atlas.infra.config import get_settings
        settings = get_settings()
    except Exception:
        logger.exception("qa_agent: failed to load settings when wrapping skills")
        return tools_list

    if not getattr(settings, "qa_skills_enabled", False):
        return tools_list

    if not getattr(settings, "qa_new_prompt", False):
        raise ConfigurationError(
            "qa_skills_enabled requires qa_new_prompt=True"
        )

    from google.adk.tools.skill_toolset import SkillToolset

    from beever_atlas.agents.query.skills import build_qa_skill_pack

    enabled_tool_names = {n for n in (_tool_name(t) for t in tools_list) if n}
    overlap_skills = []
    for skill in build_qa_skill_pack():
        allowed = skill.frontmatter.allowed_tools
        if not allowed:
            overlap_skills.append(skill)
            continue
        required = set(allowed.split())
        if required.issubset(enabled_tool_names):
            overlap_skills.append(skill)

    toolset = SkillToolset(skills=overlap_skills)
    logger.info(
        "QA skills enabled: %d/%d skills survived tool-overlap filter",
        len(overlap_skills),
        len(build_qa_skill_pack()),
    )
    # Surface the QA tools as siblings of the SkillToolset so the LLM
    # can call them directly (not just via load_skill). `additional_tools`
    # on SkillToolset does not expose tools to the agent in current ADK.
    return [toolset, *tools_list]

# Tool subsets for each answer mode
_WIKI_TOOLS_NAMES = {"get_wiki_page", "get_topic_overview"}
_SUMMARIZE_TOOLS_NAMES = {"get_wiki_page", "get_topic_overview", "search_channel_facts", "search_qa_history"}

# Cached agent instances keyed on (mode, citation_registry_enabled, qa_new_prompt) so
# flipping either flag at runtime produces a freshly-built agent.
_agents: dict[tuple[str, bool, bool, bool], LlmAgent] = {}


def _current_registry_flag() -> bool:
    try:
        from beever_atlas.infra.config import get_settings
        return bool(get_settings().citation_registry_enabled)
    except Exception:
        logger.exception("qa_agent: failed to read citation_registry_enabled")
        return False


def _current_new_prompt_flag() -> bool:
    try:
        from beever_atlas.infra.config import get_settings
        return bool(get_settings().qa_new_prompt)
    except Exception:
        logger.exception("qa_agent: failed to read qa_new_prompt")
        return False


def _current_skills_flag() -> bool:
    try:
        from beever_atlas.infra.config import get_settings
        return bool(get_settings().qa_skills_enabled)
    except Exception:
        logger.exception("qa_agent: failed to read qa_skills_enabled")
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
        logger.exception("qa_agent: failed to load settings for follow-ups tool")
        return tools_list
    from beever_atlas.agents.query.follow_ups_tool import suggest_follow_ups
    return [*tools_list, suggest_follow_ups]


def create_qa_agent(
    mode: str = "deep",
    tools: list | None = None,
    extra_instruction: str = "",
) -> LlmAgent:
    """Create a QA LlmAgent for the specified answer mode.

    Args:
        mode: "quick", "deep", or "summarize"
        tools: Optional override for `QA_TOOLS`. When provided, mode-specific
            tool subsets are filtered against this list instead of the global
            registry. `QA_TOOLS` is never mutated.
        extra_instruction: Optional trailing text appended to the mode's
            system prompt (e.g. per-request refusal clause for disabled
            tools).

    Returns:
        LlmAgent configured for the specified mode.
    """
    from beever_atlas.llm.provider import get_llm_provider
    from beever_atlas.agents.mcp_registry import get_mcp_registry

    provider = get_llm_provider()
    model = provider.resolve_model("qa_agent")
    registry = get_mcp_registry()

    base_tools = tools if tools is not None else QA_TOOLS

    # Inject the untrusted-content system note once. Retrieved memory text
    # and message bodies are wrapped in <untrusted> tags downstream
    # (see beever_atlas.agents.prompt_safety).
    extra_instruction = f"\n\n{UNTRUSTED_SYSTEM_NOTE}\n{extra_instruction}"

    if mode == "quick":
        # Quick: 2 tools, no thinking, concise prompt
        tools_list = [t for t in base_tools if getattr(t, '__name__', '') in _WIKI_TOOLS_NAMES]
        prompt = build_qa_system_prompt(max_tool_calls=2, include_follow_ups=False, mode="quick") + QA_QUICK_SUFFIX
        prompt = prompt + extra_instruction
        agent_tools = _maybe_wrap_with_skills(tools_list)
        agent = LlmAgent(
            name="qa_agent_quick",
            model=model,
            instruction=prompt,
            tools=agent_tools,
        )
    elif mode == "summarize":
        # Summarize: 4 tools, thinking, structured output
        tools_list = [t for t in base_tools if getattr(t, '__name__', '') in _SUMMARIZE_TOOLS_NAMES]
        tools_list = [*tools_list, *registry.tools]
        tools_list = _maybe_add_follow_ups_tool(tools_list, include_follow_ups=True)
        prompt = build_qa_system_prompt(max_tool_calls=4, include_follow_ups=True, mode="summarize") + QA_SUMMARIZE_SUFFIX
        prompt = prompt + extra_instruction
        planner = _create_thinking_planner()
        agent_tools = _maybe_wrap_with_skills(tools_list)
        agent = LlmAgent(
            name="qa_agent_summarize",
            model=model,
            instruction=prompt,
            tools=agent_tools,
            planner=planner,
        )
    else:
        # Deep (default): all tools, thinking, full pipeline
        all_tools = [*base_tools, *registry.tools]
        all_tools = _maybe_add_follow_ups_tool(all_tools, include_follow_ups=True)
        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=True)
        prompt = prompt + extra_instruction
        planner = _create_thinking_planner()
        agent_tools = _maybe_wrap_with_skills(all_tools)
        agent = LlmAgent(
            name="qa_agent_deep",
            model=model,
            instruction=prompt,
            tools=agent_tools,
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

    Cache key is `(mode, citation_registry_enabled, qa_new_prompt,
    qa_skills_enabled)` so flipping any of those flags at runtime
    produces a new agent with the correct tool-set — avoids stale
    prompt/tool mismatch during rollout.
    """
    key = (
        mode,
        _current_registry_flag(),
        _current_new_prompt_flag(),
        _current_skills_flag(),
    )
    if key not in _agents:
        _agents[key] = create_qa_agent(mode)
    return _agents[key]


def get_root_agent() -> LlmAgent:
    """Get the default (deep) QA agent. Backward-compatible entry point."""
    return get_agent_for_mode("deep")
