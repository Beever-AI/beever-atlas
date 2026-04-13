"""Tests for the QA agent and root agent export.

Updated from the former echo agent tests after echo.py was replaced by qa_agent.
"""

from __future__ import annotations


class TestQAAgent:
    def test_agent_name(self):
        from beever_atlas.llm.provider import init_llm_provider
        from beever_atlas.infra.config import Settings

        settings = Settings(google_api_key="test")
        init_llm_provider(settings)

        from beever_atlas.agents.query.qa_agent import create_qa_agent

        agent = create_qa_agent()
        assert agent.name.startswith("qa_agent")

    def test_agent_has_instruction(self):
        from beever_atlas.llm.provider import init_llm_provider
        from beever_atlas.infra.config import Settings

        settings = Settings(google_api_key="test")
        init_llm_provider(settings)

        from beever_atlas.agents.query.qa_agent import create_qa_agent

        agent = create_qa_agent()
        assert agent.instruction
        instruction_text = agent.instruction if isinstance(agent.instruction, str) else ""
        assert "wiki" in instruction_text.lower()

    def test_agent_has_tools(self):
        from beever_atlas.llm.provider import init_llm_provider
        from beever_atlas.infra.config import Settings

        settings = Settings(google_api_key="test")
        init_llm_provider(settings)

        from beever_atlas.agents.query.qa_agent import create_qa_agent

        agent = create_qa_agent()
        # Agent should have at least the 10 internal tools
        assert len(agent.tools) >= 10


class TestRootAgentExport:
    def test_get_root_agent_returns_qa_agent(self):
        from beever_atlas.llm.provider import init_llm_provider
        from beever_atlas.infra.config import Settings

        settings = Settings(google_api_key="test")
        init_llm_provider(settings)

        # Reset cached agent so it picks up the freshly initialized provider
        import beever_atlas.agents as agents_module
        agents_module._root_agent = None

        from beever_atlas.agents import get_root_agent

        agent = get_root_agent()
        assert agent.name.startswith("qa_agent")
