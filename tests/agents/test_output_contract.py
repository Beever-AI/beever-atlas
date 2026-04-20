"""Tests for the strengthened OUTPUT_CONTRACT and ANTI_META_COMMENTARY blocks."""

from unittest.mock import MagicMock, patch


def _make_settings(*, registry_on: bool = True, new_prompt: bool = True) -> MagicMock:
    s = MagicMock()
    s.citation_registry_enabled = registry_on
    s.qa_new_prompt = new_prompt
    return s


def _build(mode: str = "deep") -> str:
    settings = _make_settings(new_prompt=True)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt

        return build_qa_system_prompt(max_tool_calls=8, include_follow_ups=False, mode=mode)


def test_structure_rules_present() -> None:
    prompt = _build()
    assert "80 words" in prompt
    assert "## From your knowledge base" in prompt


def test_depth_rules_present() -> None:
    prompt = _build()
    assert "150 words" in prompt


def test_anti_repeat_rules_present() -> None:
    prompt = _build()
    assert "repeat the same sentence" in prompt


def test_strict_block_absent_when_flag_off() -> None:
    settings = _make_settings(new_prompt=False)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=False, mode="deep")
    # Strict block is scoped to the new-prompt branch only.
    assert "## From your knowledge base" not in prompt
    assert "150 words" not in prompt
