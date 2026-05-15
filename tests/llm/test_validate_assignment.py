"""PR-C: per-Assignment capability validation + suggestion helpers."""

from __future__ import annotations

from beever_atlas.llm.model_resolver import (
    AGENT_CAPABILITIES,
    suggest_compatible_assignments,
    validate_assignment_compatibility,
)


def test_qa_agent_requires_tools() -> None:
    assert AGENT_CAPABILITIES["qa_agent"] == {"tools"}
    assert AGENT_CAPABILITIES["qa_router"] == {"tools"}


def test_vision_agents_require_vision() -> None:
    assert "vision" in AGENT_CAPABILITIES["image_describer"]
    assert "vision" in AGENT_CAPABILITIES["video_analyzer"]
    assert "vision" in AGENT_CAPABILITIES["document_digester"]


def test_qa_agent_compatible_with_claude_sonnet() -> None:
    missing = validate_assignment_compatibility("qa_agent", "anthropic/claude-sonnet-4-6")
    assert missing == []


def test_qa_agent_incompatible_with_deepseek_reasoner() -> None:
    """The reasoner model lacks tool-calling — drives the 422 gate."""
    missing = validate_assignment_compatibility("qa_agent", "deepseek/deepseek-reasoner")
    assert missing == ["tools"]


def test_image_describer_incompatible_with_non_vision() -> None:
    missing = validate_assignment_compatibility("image_describer", "deepseek/deepseek-chat")
    assert missing == ["vision"]


def test_image_describer_compatible_with_gemini_flash() -> None:
    missing = validate_assignment_compatibility("image_describer", "gemini/gemini-2.5-flash")
    assert missing == []


def test_no_requirements_means_always_compatible() -> None:
    """``echo`` and other agents with no AGENT_CAPABILITIES entry pass."""
    missing = validate_assignment_compatibility("echo", "anything/whatever")
    assert missing == []


def test_operator_override_can_unblock_assignment() -> None:
    """When the operator hand-marks an unknown model as tool-capable, the
    validator respects the override."""
    overrides = {"custom/internal-llm": {"supports_tools": True, "supports_vision": True}}
    missing = validate_assignment_compatibility(
        "qa_agent", "custom/internal-llm", endpoint_overrides=overrides
    )
    assert missing == []


def test_unknown_model_defaults_to_blocked_for_tool_agents() -> None:
    """No catalog entry + no override + heuristic-fails → blocked."""
    missing = validate_assignment_compatibility("qa_agent", "custom/totally-mystery")
    assert "tools" in missing


# ── suggest_compatible_assignments ───────────────────────────────────────


def test_suggestions_for_image_describer_prefer_local() -> None:
    """Among compatible models, local Ollama ranks before paid cloud."""
    candidates = [
        ("ep-anthropic", "anthropic/claude-sonnet-4-6"),
        ("ep-gemini", "gemini/gemini-2.5-flash"),
        ("ep-ollama", "ollama_chat/gemma3:e4b"),  # vision yes
    ]
    suggested = suggest_compatible_assignments("image_describer", candidates, n=3)
    # gemma3:e4b is local + vision-capable → first
    assert suggested[0] == ("ep-ollama", "ollama_chat/gemma3:e4b")


def test_suggestions_filter_incompatible() -> None:
    """Tool-required agents get only tool-capable suggestions."""
    candidates = [
        ("ep-deepseek-reasoner", "deepseek/deepseek-reasoner"),  # no tools
        ("ep-anthropic", "anthropic/claude-haiku-4-5"),  # tools
        ("ep-openai", "openai/gpt-4o-mini"),  # tools
    ]
    suggested = suggest_compatible_assignments("qa_agent", candidates, n=3)
    suggested_ids = {model for _, model in suggested}
    assert "deepseek/deepseek-reasoner" not in suggested_ids
    assert "anthropic/claude-haiku-4-5" in suggested_ids


def test_suggestions_sort_by_ascending_cost() -> None:
    """Within compatible cloud options, cheaper wins."""
    candidates = [
        ("ep-haiku", "anthropic/claude-haiku-4-5"),  # input $1.00/M
        ("ep-opus", "anthropic/claude-opus-4-7"),  # input $15.00/M
        ("ep-mini", "openai/gpt-4o-mini"),  # input $0.15/M (cheapest)
    ]
    suggested = suggest_compatible_assignments("qa_agent", candidates, n=3)
    assert suggested[0] == ("ep-mini", "openai/gpt-4o-mini")
    assert suggested[-1] == ("ep-opus", "anthropic/claude-opus-4-7")


def test_suggestions_capped_at_n() -> None:
    candidates = [
        ("ep1", "openai/gpt-4o-mini"),
        ("ep2", "anthropic/claude-haiku-4-5"),
        ("ep3", "gemini/gemini-2.5-flash"),
        ("ep4", "mistral/mistral-small-latest"),
    ]
    suggested = suggest_compatible_assignments("qa_agent", candidates, n=2)
    assert len(suggested) == 2


def test_suggestions_empty_when_no_compatible() -> None:
    candidates = [
        ("ep-reasoner", "deepseek/deepseek-reasoner"),
    ]
    suggested = suggest_compatible_assignments("qa_agent", candidates, n=3)
    assert suggested == []
