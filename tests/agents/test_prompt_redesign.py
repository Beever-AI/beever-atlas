"""Tests for the QA_NEW_PROMPT flag-gated prompt redesign."""

from unittest.mock import patch, MagicMock


def _make_settings(*, registry_on: bool = True, new_prompt: bool) -> MagicMock:
    s = MagicMock()
    s.citation_registry_enabled = registry_on
    s.qa_new_prompt = new_prompt
    return s


def test_legacy_prompt_unchanged_when_flag_off():
    """Flag off must produce a prompt that still contains the prescriptive pipeline header."""
    settings = _make_settings(new_prompt=False)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=False, mode="deep")
    assert "Required Retrieval Pipeline" in prompt


def test_new_prompt_excludes_prescriptive_steps():
    """Flag on: prescriptive step markers must NOT appear in the prompt."""
    settings = _make_settings(new_prompt=True)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=False, mode="deep")
    assert "Step 1 —" not in prompt
    assert "ALWAYS" not in prompt
    assert "REQUIRED for" not in prompt


def test_new_prompt_contains_output_contract():
    """Flag on: OUTPUT_CONTRACT first line and ANTI_META_COMMENTARY markers must be present."""
    settings = _make_settings(new_prompt=True)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=False, mode="deep")
    assert "Your final message is the answer the user reads" in prompt
    assert "Never describe your reasoning" in prompt
    assert "Emit the finished answer only" in prompt


def test_new_prompt_cached_independently():
    """Agents built with flag off and flag on must be distinct cached instances."""
    import beever_atlas.agents.query.qa_agent as qa_agent_module

    # Clear agent cache between flag states
    qa_agent_module._agents.clear()

    mock_agent_off = MagicMock(name="agent_flag_off")
    mock_agent_on = MagicMock(name="agent_flag_on")

    call_count = {"n": 0}

    def fake_create(mode="deep"):
        call_count["n"] += 1
        return mock_agent_off if call_count["n"] == 1 else mock_agent_on

    settings_off = _make_settings(new_prompt=False)
    settings_on = _make_settings(new_prompt=True)

    with patch.object(qa_agent_module, "create_qa_agent", side_effect=fake_create):
        with patch("beever_atlas.infra.config.get_settings", return_value=settings_off):
            agent1 = qa_agent_module.get_agent_for_mode("deep")
        with patch("beever_atlas.infra.config.get_settings", return_value=settings_on):
            agent2 = qa_agent_module.get_agent_for_mode("deep")

    assert agent1 is not agent2


def test_new_prompt_contains_greeting_response():
    """Flag on: the greeting/identity directive must be present and name Beever Atlas."""
    settings = _make_settings(new_prompt=True)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import GREETING_RESPONSE, build_qa_system_prompt

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=False, mode="deep")
    assert GREETING_RESPONSE in prompt
    assert "Beever Atlas" in GREETING_RESPONSE
    # A concrete capability and example-question nudge are part of the contract.
    assert "greeting" in GREETING_RESPONSE.lower()
    assert "example questions" in GREETING_RESPONSE.lower()
    # Load-bearing clause: a greeting must NOT trigger retrieval/tool calls.
    assert "do not call any tools" in GREETING_RESPONSE.lower()


def test_new_prompt_contains_channel_context_privacy():
    """Flag on: the channel-id privacy directive must be present in the new branch."""
    settings = _make_settings(new_prompt=True)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import (
            CHANNEL_CONTEXT_PRIVACY,
            build_qa_system_prompt,
        )

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=False, mode="deep")
    assert CHANNEL_CONTEXT_PRIVACY in prompt
    assert "[Channel: <id>]" in CHANNEL_CONTEXT_PRIVACY
    assert "channel_name" in CHANNEL_CONTEXT_PRIVACY
    # Load-bearing clause: the raw channel id must never be echoed back.
    assert "never echo" in CHANNEL_CONTEXT_PRIVACY.lower()


def test_greeting_and_privacy_absent_when_flag_off():
    """Legacy branch must not include the new additive blocks."""
    settings = _make_settings(new_prompt=False)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import (
            CHANNEL_CONTEXT_PRIVACY,
            GREETING_RESPONSE,
            build_qa_system_prompt,
        )

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=False, mode="deep")
    assert GREETING_RESPONSE not in prompt
    assert CHANNEL_CONTEXT_PRIVACY not in prompt


def test_deep_mode_skips_length_hint_both_paths():
    """Deep mode never includes the onboarding length hint — on either prompt path."""
    from beever_atlas.agents.query.prompts import ONBOARDING_LENGTH_HINT, build_qa_system_prompt

    for new_prompt in (False, True):
        settings = _make_settings(new_prompt=new_prompt)
        with patch("beever_atlas.infra.config.get_settings", return_value=settings):
            prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=False, mode="deep")
        hint_text = ONBOARDING_LENGTH_HINT.splitlines()[0]
        assert hint_text not in prompt, (
            f"Length hint found in deep-mode prompt (new_prompt={new_prompt})"
        )


def test_prompt_contains_todays_date_both_paths():
    """A dated line (computed at call time) must be injected on both prompt paths."""
    from datetime import datetime, timezone

    fixed = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    expected = f"{fixed:%A, %Y-%m-%d}"
    for new_prompt in (False, True):
        settings = _make_settings(new_prompt=new_prompt)
        with patch("beever_atlas.infra.config.get_settings", return_value=settings):
            with patch("beever_atlas.agents.query.prompts.datetime", _FixedDatetime):
                from beever_atlas.agents.query.prompts import build_qa_system_prompt

                prompt = build_qa_system_prompt(
                    max_tool_calls=8, include_follow_ups=False, mode="deep"
                )
        assert f"Today's date is {expected}" in prompt, (
            f"Date line missing (new_prompt={new_prompt})"
        )
        # Relative-time anchoring clause must accompany the date.
        assert "relative to this date" in prompt


def test_prompt_identifies_as_beever_atlas_no_model_leak_both_paths():
    """Invariants: identifies as Beever Atlas; never leaks model/provider identity."""
    for new_prompt in (False, True):
        settings = _make_settings(new_prompt=new_prompt)
        with patch("beever_atlas.infra.config.get_settings", return_value=settings):
            from beever_atlas.agents.query.prompts import build_qa_system_prompt

            prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=False, mode="deep")
        assert "Beever Atlas" in prompt
        lowered = prompt.lower()
        for leak in ("gemini", "google", "openai", "anthropic", "claude"):
            assert leak not in lowered, f"model/provider leak '{leak}' (new_prompt={new_prompt})"
