"""Tests for the echo agent and model configuration."""

from __future__ import annotations

import os
from unittest.mock import patch

from beever_atlas.agents.echo import echo_agent, _get_model, _DEFAULT_FAST_MODEL


class TestEchoAgent:
    def test_agent_name(self):
        assert echo_agent.name == "query_router_agent"

    def test_agent_has_description(self):
        assert echo_agent.description
        assert "echo" in echo_agent.description.lower()

    def test_agent_has_instruction(self):
        assert echo_agent.instruction
        assert "echo" in echo_agent.instruction.lower()

    def test_agent_model_default(self):
        assert echo_agent.model == _DEFAULT_FAST_MODEL


class TestModelConfiguration:
    def test_default_fast_model(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("LLM_FAST_MODEL", None)
            result = _get_model("LLM_FAST_MODEL", "gemini/gemini-2.0-flash-lite")
            assert result == "gemini/gemini-2.0-flash-lite"

    def test_custom_fast_model(self):
        with patch.dict(os.environ, {"LLM_FAST_MODEL": "anthropic/claude-haiku-4-5"}):
            result = _get_model("LLM_FAST_MODEL", "gemini/gemini-2.0-flash-lite")
            assert result == "anthropic/claude-haiku-4-5"

    def test_default_quality_model(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("LLM_QUALITY_MODEL", None)
            result = _get_model("LLM_QUALITY_MODEL", "gemini/gemini-2.0-flash")
            assert result == "gemini/gemini-2.0-flash"


class TestRootAgentExport:
    def test_root_agent_is_echo_agent(self):
        from beever_atlas.agents import root_agent

        assert root_agent is echo_agent
        assert root_agent.name == "query_router_agent"
