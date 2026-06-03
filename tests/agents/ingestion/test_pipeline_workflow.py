"""Regression tests for the ADK Workflow ingestion pipeline.

``create_ingestion_pipeline`` now returns a ``google.adk.workflow.Workflow``
instead of a deprecated ``SequentialAgent``. The graph fans out to two
extractors, then to embedder + validator, with ``JoinNode``s gating each
fan-in so the downstream stage (ultimately the persister) runs EXACTLY ONCE.

A plain fan-in target runs once per predecessor trigger, so without the
``join_enrich`` node the persister would run twice and double-write to
Weaviate/the graph. These tests lock in:

  1. the persister-equivalent stage runs exactly once (the JoinNode guard),
  2. state written by the preprocessor is visible to downstream stages,
  3. the pipeline completes without error.

The LLM-dependent extractors are monkeypatched to lightweight fake
``BaseAgent``s that only write marker keys to ``session.state``, so no LLM
provider call is required.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.workflow import Workflow


# ── helpers ─────────────────────────────────────────────────────────────────


class _MarkerAgent(BaseAgent):
    """A fake BaseAgent that writes a single marker key via state_delta."""

    out_key: str
    out_val: Any

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(state_delta={self.out_key: self.out_val}),
        )


class _CountingPersister(BaseAgent):
    """A fake persister that counts its own invocations and records whether
    the preprocessor's marker was visible in shared ``session.state``.
    """

    calls: list[int]

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        self.calls.append(1)
        saw_preprocessed = ctx.session.state.get("marker_preprocessed")
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(
                state_delta={
                    "persist_result": {
                        "saw_preprocessed": saw_preprocessed,
                        "run_count": len(self.calls),
                    }
                }
            ),
        )


def _patch_pipeline_stages(monkeypatch, persister: _CountingPersister) -> None:
    """Replace every pipeline stage factory with a lightweight fake so the
    real graph topology (edges + JoinNodes) is exercised end-to-end without
    touching the LLM provider or the stores.
    """
    import beever_atlas.agents.ingestion.pipeline as pipeline_mod

    monkeypatch.setattr(
        pipeline_mod,
        "PreprocessorAgent",
        lambda name: _MarkerAgent(name=name, out_key="marker_preprocessed", out_val="PP"),
    )
    monkeypatch.setattr(
        pipeline_mod,
        "create_fact_extractor",
        lambda: _MarkerAgent(name="fact_extractor", out_key="marker_facts", out_val="F"),
    )
    monkeypatch.setattr(
        pipeline_mod,
        "create_entity_extractor",
        lambda: _MarkerAgent(name="entity_extractor", out_key="marker_entities", out_val="E"),
    )
    monkeypatch.setattr(
        pipeline_mod,
        "EmbedderAgent",
        lambda name: _MarkerAgent(name=name, out_key="marker_embedded", out_val="EM"),
    )
    monkeypatch.setattr(
        pipeline_mod,
        "DeterministicCrossBatchValidator",
        lambda name: _MarkerAgent(name=name, out_key="marker_validated", out_val="V"),
    )
    monkeypatch.setattr(pipeline_mod, "PersisterAgent", lambda name: persister)


async def _run_pipeline(workflow: Workflow) -> dict[str, Any]:
    """Drive the workflow to completion via a fresh Runner + InMemory session
    and return the final session state. ``app_name`` must match the Runner's
    resolved app name (Runner(node=...) defaults it to ``node.name``), so we
    pass it explicitly to both.
    """
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    app_name = "beever_atlas"
    svc = InMemorySessionService()
    runner = Runner(node=workflow, app_name=app_name, session_service=svc)
    session = await svc.create_session(
        app_name=app_name, user_id="system", session_id="s-test", state={}
    )

    async for _event in runner.run_async(
        user_id="system",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="process")]),
    ):
        pass  # Drive to completion

    final = await svc.get_session(app_name=app_name, user_id="system", session_id=session.id)
    return dict(final.state) if final else {}


# ── tests ────────────────────────────────────────────────────────────────────


def test_create_ingestion_pipeline_returns_workflow(monkeypatch):
    """The composition layer now returns a Workflow graph, not a
    SequentialAgent."""
    persister = _CountingPersister(name="persister", calls=[])
    _patch_pipeline_stages(monkeypatch, persister)

    from beever_atlas.agents.ingestion.pipeline import create_ingestion_pipeline

    workflow = create_ingestion_pipeline()
    assert isinstance(workflow, Workflow)
    assert workflow.name == "ingestion_pipeline"


@pytest.mark.asyncio
async def test_persister_runs_exactly_once(monkeypatch):
    """JoinNode regression guard: the persister must run EXACTLY ONCE despite
    two incoming enrich-stage predecessors. Without ``join_enrich`` it would
    run twice and double-write to Weaviate/the graph.
    """
    persister = _CountingPersister(name="persister", calls=[])
    _patch_pipeline_stages(monkeypatch, persister)

    from beever_atlas.agents.ingestion.pipeline import create_ingestion_pipeline

    state = await _run_pipeline(create_ingestion_pipeline())

    assert len(persister.calls) == 1, (
        f"persister must run exactly once (JoinNode guard); ran {len(persister.calls)} times"
    )
    assert state["persist_result"]["run_count"] == 1


@pytest.mark.asyncio
async def test_preprocessor_state_visible_downstream(monkeypatch):
    """State written by the preprocessor stage propagates through the graph
    and is visible to the downstream persister stage."""
    persister = _CountingPersister(name="persister", calls=[])
    _patch_pipeline_stages(monkeypatch, persister)

    from beever_atlas.agents.ingestion.pipeline import create_ingestion_pipeline

    state = await _run_pipeline(create_ingestion_pipeline())

    assert state["persist_result"]["saw_preprocessed"] == "PP"


@pytest.mark.asyncio
async def test_all_stages_complete_without_error(monkeypatch):
    """Every stage writes its marker, confirming the full graph executed to
    completion."""
    persister = _CountingPersister(name="persister", calls=[])
    _patch_pipeline_stages(monkeypatch, persister)

    from beever_atlas.agents.ingestion.pipeline import create_ingestion_pipeline

    state = await _run_pipeline(create_ingestion_pipeline())

    assert state["marker_preprocessed"] == "PP"
    assert state["marker_facts"] == "F"
    assert state["marker_entities"] == "E"
    assert state["marker_embedded"] == "EM"
    assert state["marker_validated"] == "V"
    assert state["persist_result"]["run_count"] == 1
