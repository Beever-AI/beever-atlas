"""Tests for the WikiStructurePlanner orchestration.

Mocks the LLM caller via the ``llm`` constructor injection so we can
exercise the happy path, every fallback path (no LLM, LLM exception,
JSON parse error, validator failure), and the below-threshold short-
circuit — all without touching a real provider.
"""

from __future__ import annotations

import json


from beever_atlas.wiki.structure.planner import (
    PlannedFolder,
    PlannedStructure,
    WikiStructurePlanner,
)


def _cluster(cid: str, title: str = "Topic") -> dict:
    return {"id": cid, "title": f"{title} {cid}", "summary": "", "member_count": 5}


# ---- below-min-topics short circuit ----------------------------------------


def test_below_min_topics_returns_flat_with_reason() -> None:
    planner = WikiStructurePlanner(llm=lambda _p: '{"folders": [], "leaves": []}')
    out = planner.plan(
        channel_summary="x",
        clusters=[_cluster(f"c{i}") for i in range(3)],  # below default 8
    )
    assert out.folders == []
    assert sorted(out.leaves) == ["c0", "c1", "c2"]
    assert out.fallback_reason == "below_min_topics"


# ---- happy path -----------------------------------------------------------


def test_happy_path_llm_returns_valid_plan() -> None:
    """LLM returns a valid 2-folder + 2-leaves plan; planner accepts it."""
    fake_response = json.dumps(
        {
            "folders": [
                {
                    "slug": "security",
                    "title": "Security",
                    "child_slugs": ["c1", "c2"],
                    "rationale": "auth-related cluster",
                },
                {
                    "slug": "growth",
                    "title": "Growth",
                    "child_slugs": ["c3", "c4"],
                    "rationale": "marketing campaigns",
                },
            ],
            "leaves": ["c5", "c6", "c7", "c8"],
        }
    )
    planner = WikiStructurePlanner(llm=lambda _p: fake_response)
    out = planner.plan(
        channel_summary="A channel about security and marketing.",
        clusters=[_cluster(f"c{i}") for i in range(1, 9)],
    )
    assert out.fallback_reason is None
    assert len(out.folders) == 2
    assert {f.slug for f in out.folders} == {"security", "growth"}
    # All 8 clusters placed exactly once.
    placed = sum(len(f.child_slugs) for f in out.folders) + len(out.leaves)
    assert placed == 8


# ---- LLM exception → fallback ---------------------------------------------


def test_llm_exception_falls_back_to_flat() -> None:
    def boom(_prompt: str) -> str:
        raise RuntimeError("provider unavailable")

    planner = WikiStructurePlanner(llm=boom)
    out = planner.plan(
        channel_summary="x",
        clusters=[_cluster(f"c{i}") for i in range(10)],
    )
    assert out.fallback_reason == "llm_exception"
    assert sorted(out.leaves) == [f"c{i}" for i in range(10)]


# ---- JSON parse error → fallback ------------------------------------------


def test_unparseable_llm_response_falls_back() -> None:
    planner = WikiStructurePlanner(llm=lambda _p: "this is not JSON {[ broken")
    out = planner.plan(
        channel_summary="x",
        clusters=[_cluster(f"c{i}") for i in range(10)],
    )
    assert out.fallback_reason == "json_parse"


def test_llm_response_with_code_fences_is_unwrapped() -> None:
    """Common Gemini behaviour: wraps JSON in ```json ... ``` despite instruction."""
    body = {"folders": [], "leaves": [f"c{i}" for i in range(10)]}
    fake = "```json\n" + json.dumps(body) + "\n```"
    planner = WikiStructurePlanner(llm=lambda _p: fake)
    out = planner.plan(
        channel_summary="x",
        clusters=[_cluster(f"c{i}") for i in range(10)],
    )
    assert out.fallback_reason is None
    assert sorted(out.leaves) == [f"c{i}" for i in range(10)]


# ---- validator failure → fallback -----------------------------------------


def test_validator_failure_falls_back_with_reason() -> None:
    """LLM returns plan with duplicate cluster → planner falls back."""
    fake = json.dumps(
        {
            "folders": [
                {"slug": "f1", "title": "F1", "child_slugs": ["c1", "c2"]},
                {"slug": "f2", "title": "F2", "child_slugs": ["c2", "c3"]},  # c2 dupe
            ],
            "leaves": [f"c{i}" for i in range(4, 11)],
        }
    )
    planner = WikiStructurePlanner(llm=lambda _p: fake)
    out = planner.plan(
        channel_summary="x",
        clusters=[_cluster(f"c{i}") for i in range(1, 11)],
    )
    assert out.fallback_reason == "cluster_duplicate"


# ---- no LLM injected → fallback ------------------------------------------


def test_no_llm_falls_back_with_reason() -> None:
    planner = WikiStructurePlanner(llm=None)
    out = planner.plan(
        channel_summary="x",
        clusters=[_cluster(f"c{i}") for i in range(10)],
    )
    assert out.fallback_reason == "no_llm_configured"


# ---- PlannedStructure.flat helper ----------------------------------------


def test_planned_structure_flat_helper() -> None:
    out = PlannedStructure.flat(["a", "b"], reason="testing")
    assert out.folders == []
    assert out.leaves == ["a", "b"]
    assert out.fallback_reason == "testing"


def test_planned_folder_defaults() -> None:
    f = PlannedFolder(slug="x", title="X")
    assert f.child_slugs == []
    assert f.rationale == ""
