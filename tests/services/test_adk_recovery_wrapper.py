"""Tests for the centralized ADK recovery wrapper (adk_recovery.wrap_with_recovery).

Covers:
- truncated JSON that can be recovered → agent output_key populated, no failed_recoverable
- completely unrecoverable input → failed_recoverable=True + truncation_report emitted
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pydantic import BaseModel

from beever_atlas.services.adk_recovery import wrap_with_recovery
from beever_atlas.services.json_recovery import recover_truncated_json


# ---------------------------------------------------------------------------
# Minimal Pydantic model for testing
# ---------------------------------------------------------------------------


class _SimpleResult(BaseModel):
    items: list[str] = []
    count: int = 0


def _recovery_fn(text: str) -> dict | None:
    result = recover_truncated_json(text)
    if isinstance(result, dict):
        return result
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(output_key: str = "my_output") -> MagicMock:
    agent = MagicMock()
    agent.output_key = output_key
    agent.after_agent_callback = None
    return agent


def _make_callback_context(state: dict) -> MagicMock:
    ctx = MagicMock()
    ctx.state = state
    return ctx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_wrap_installs_callback() -> None:
    agent = _make_agent()
    result = wrap_with_recovery(agent, _recovery_fn, _SimpleResult)
    assert result is agent
    assert callable(agent.after_agent_callback)


def test_valid_dict_passes_through() -> None:
    """When output_key already holds a valid dict, callback is a no-op."""
    agent = _make_agent("my_output")
    wrap_with_recovery(agent, _recovery_fn, _SimpleResult)

    state = {"my_output": {"items": ["a", "b"], "count": 2}}
    ctx = _make_callback_context(state)
    agent.after_agent_callback(ctx)

    assert ctx.state.get("failed_recoverable") is None
    assert ctx.state["my_output"] == {"items": ["a", "b"], "count": 2}


def test_truncated_json_is_recovered() -> None:
    """A truncated but partially valid JSON string is recovered and validated."""
    agent = _make_agent("my_output")
    wrap_with_recovery(agent, _recovery_fn, _SimpleResult)

    # Truncated JSON — missing closing brace
    truncated = '{"items": ["x", "y"], "count": 2'
    state = {"my_output": truncated}
    ctx = _make_callback_context(state)
    agent.after_agent_callback(ctx)

    assert ctx.state.get("failed_recoverable") is None, (
        "Recoverable truncation should not set failed_recoverable"
    )
    assert isinstance(ctx.state["my_output"], dict)
    assert ctx.state["my_output"]["items"] == ["x", "y"]


def test_unrecoverable_input_marks_failed() -> None:
    """When recovery_fn returns None, failed_recoverable=True and truncation_report is set."""
    agent = _make_agent("my_output")

    def _always_fail(text: str) -> dict | None:
        return None

    wrap_with_recovery(agent, _always_fail, _SimpleResult)

    state = {"my_output": "this is not json at all %%%%"}
    ctx = _make_callback_context(state)
    agent.after_agent_callback(ctx)

    assert ctx.state.get("failed_recoverable") is True
    report = ctx.state.get("truncation_report")
    assert report is not None
    assert report["model"] == "_SimpleResult"
    assert report["output_key"] == "my_output"
    assert report["recovered_count"] == 0


def test_none_output_marks_failed() -> None:
    """When output_key is missing/None, failed_recoverable is set."""
    agent = _make_agent("my_output")
    wrap_with_recovery(agent, _recovery_fn, _SimpleResult)

    state = {}  # key missing
    ctx = _make_callback_context(state)
    agent.after_agent_callback(ctx)

    assert ctx.state.get("failed_recoverable") is True


def test_recovered_data_invalid_against_model_marks_failed() -> None:
    """recovery_fn returns data but it fails Pydantic validation → failed_recoverable."""
    agent = _make_agent("my_output")

    def _bad_recovery(text: str) -> dict | None:
        # Returns wrong types that won't validate
        return {"items": "not-a-list", "count": "not-an-int"}

    wrap_with_recovery(agent, _bad_recovery, _SimpleResult)

    state = {"my_output": '{"items": "bad"}'}
    ctx = _make_callback_context(state)
    agent.after_agent_callback(ctx)

    assert ctx.state.get("failed_recoverable") is True


def test_already_valid_dict_no_failed_recoverable() -> None:
    """A dict that validates cleanly never sets failed_recoverable."""
    agent = _make_agent("out")
    wrap_with_recovery(agent, _recovery_fn, _SimpleResult)

    state = {"out": {"items": [], "count": 0}}
    ctx = _make_callback_context(state)
    agent.after_agent_callback(ctx)

    assert "failed_recoverable" not in ctx.state


def test_output_schema_stripped_after_wrap() -> None:
    """wrap_with_recovery must set output_schema=None so ADK does not validate before callback."""
    from google.adk.agents import LlmAgent

    agent = LlmAgent(
        name="test_strip",
        model="gemini-2.0-flash",
        instruction="test",
        output_key="result",
        output_schema=_SimpleResult,
    )
    assert agent.output_schema is _SimpleResult, "precondition: output_schema was set"

    wrap_with_recovery(agent, _recovery_fn, _SimpleResult)

    assert agent.output_schema is None, (
        "wrap_with_recovery must strip output_schema to prevent ADK's internal "
        "model_validate_json from raising before the recovery callback runs"
    )
