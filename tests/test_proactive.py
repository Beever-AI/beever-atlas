"""Tests for proactive tension surfacing (pure relevance logic, no I/O)."""

from __future__ import annotations

from types import SimpleNamespace

from beever_atlas.capabilities.proactive import (
    _keywords,
    _summarize_positions,
    extract_relevant_tensions,
)


def _page(modules):
    return SimpleNamespace(modules=modules)


def _tension_module(title, positions=None, status="open"):
    return {
        "id": "tension_callout",
        "data": {"title": title, "status": status, "positions": positions or []},
    }


class TestExtractRelevantTensions:
    def test_surfaces_tension_when_answer_overlaps_title(self):
        pages = [
            _page(
                [
                    _tension_module(
                        "Launch order disputed",
                        [
                            {"author": "Jacy", "stance": "MAGIC first"},
                            {"author": "Pak", "stance": "Atlas first"},
                        ],
                    )
                ]
            )
        ]
        out = extract_relevant_tensions(pages, "Are we launching in a certain order?", limit=2)
        assert len(out) == 1
        assert out[0]["title"] == "Launch order disputed"
        assert "Jacy: MAGIC first" in out[0]["detail"]

    def test_no_false_tension_when_topic_unrelated(self):
        pages = [_page([_tension_module("Launch order disputed")])]
        assert extract_relevant_tensions(pages, "What is our refund policy?", limit=2) == []

    def test_empty_answer_returns_nothing(self):
        pages = [_page([_tension_module("Launch order disputed")])]
        assert extract_relevant_tensions(pages, "", limit=2) == []

    def test_caps_at_limit(self):
        mods = [_tension_module(f"Budget item {i} disputed") for i in range(5)]
        out = extract_relevant_tensions([_page(mods)], "the budget is disputed", limit=2)
        assert len(out) == 2

    def test_ignores_non_tension_modules_and_junk(self):
        pages = [_page([{"id": "summary", "data": {}}, "junk", _tension_module("")])]
        assert extract_relevant_tensions(pages, "anything relevant here", limit=2) == []


def test_summarize_positions():
    assert _summarize_positions({"status": "blocked"}) == "status: blocked"
    assert _summarize_positions({"positions": [{"author": "A", "stance": "x"}]}) == "A: x"


def test_keywords_drops_short_and_stopwords():
    kw = _keywords("The budget allocation is disputed")
    assert {"budget", "allocation", "disputed"} <= kw
    assert "the" not in kw and "is" not in kw


from unittest.mock import AsyncMock, patch  # noqa: E402

from beever_atlas.capabilities.proactive import get_relevant_tensions  # noqa: E402


async def test_get_relevant_tensions_rejects_empty_args():
    assert await get_relevant_tensions("", "principal", "text") == []
    assert await get_relevant_tensions("channel", "", "text") == []


async def test_get_relevant_tensions_returns_empty_on_access_denied():
    with patch(
        "beever_atlas.infra.channel_access.assert_channel_access",
        new=AsyncMock(side_effect=PermissionError("denied")),
    ):
        out = await get_relevant_tensions("ch-eng", "user:abc", "the budget is disputed")
    assert out == []
