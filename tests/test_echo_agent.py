"""Tests for the echo agent and model configuration."""

from __future__ import annotations


class TestEchoAgent:
    def test_agent_name(self):
        from beever_atlas.agents.query.echo import create_echo_agent

        agent = create_echo_agent()
        assert agent.name == "query_router_agent"

    def test_agent_has_description(self):
        from beever_atlas.agents.query.echo import create_echo_agent

        agent = create_echo_agent()
        assert agent.description
        assert "echo" in agent.description.lower()

    def test_agent_has_instruction(self):
        from beever_atlas.agents.query.echo import create_echo_agent

        agent = create_echo_agent()
        assert agent.instruction
        assert "echo" in agent.instruction.lower()


class TestRootAgentExport:
    def test_get_root_agent_returns_echo_agent(self):
        from beever_atlas.agents import get_root_agent

        agent = get_root_agent()
        assert agent.name == "query_router_agent"
