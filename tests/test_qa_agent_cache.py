"""Verify qa_agent caches respect qa_skills_enabled in the cache key.

Flipping `qa_skills_enabled` at runtime must yield a fresh agent
instance — otherwise a stale agent built without the skill toolset
survives after the flag flips on (and vice-versa).
"""

from __future__ import annotations

import pytest

from beever_atlas.agents.query import qa_agent as qa_mod


@pytest.fixture(autouse=True)
def _clear_agent_cache():
    qa_mod._agents.clear()
    yield
    qa_mod._agents.clear()


def test_cache_key_includes_qa_skills_enabled(monkeypatch):
    """Toggling qa_skills_enabled produces distinct cached instances."""
    build_calls: list[tuple] = []
    skills_flag = {"value": False}

    def fake_create(mode: str = "deep", **_kw):
        # Capture the set of flags at build time so the test can assert
        # that a rebuild happens with the new flag value.
        build_calls.append(
            (
                mode,
                qa_mod._current_registry_flag(),
                qa_mod._current_new_prompt_flag(),
                qa_mod._current_skills_flag(),
            )
        )
        return object()

    monkeypatch.setattr(qa_mod, "create_qa_agent", fake_create)
    monkeypatch.setattr(qa_mod, "_current_registry_flag", lambda: True)
    monkeypatch.setattr(qa_mod, "_current_new_prompt_flag", lambda: True)
    monkeypatch.setattr(qa_mod, "_current_skills_flag", lambda: skills_flag["value"])

    agent_off = qa_mod.get_agent_for_mode("deep")
    # Second fetch with same flags returns the cached instance.
    agent_off_cached = qa_mod.get_agent_for_mode("deep")
    assert agent_off is agent_off_cached
    assert len(build_calls) == 1

    # Flip skills flag — must rebuild.
    skills_flag["value"] = True
    agent_on = qa_mod.get_agent_for_mode("deep")
    assert agent_on is not agent_off
    assert len(build_calls) == 2
    assert build_calls[0][3] is False
    assert build_calls[1][3] is True


def test_cache_key_tuple_has_four_entries(monkeypatch):
    """Guard against regressions that drop the skills flag from the key."""
    monkeypatch.setattr(qa_mod, "create_qa_agent", lambda mode="deep", **_k: object())
    monkeypatch.setattr(qa_mod, "_current_registry_flag", lambda: False)
    monkeypatch.setattr(qa_mod, "_current_new_prompt_flag", lambda: False)
    monkeypatch.setattr(qa_mod, "_current_skills_flag", lambda: False)
    qa_mod.get_agent_for_mode("deep")
    key = next(iter(qa_mod._agents))
    assert len(key) == 4
    assert key == ("deep", False, False, False)
