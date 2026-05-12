"""Integration-test conftest.

Phase 5 of ``sync-pipeline-feedback-and-auto-wiki``. Provides:

* ``sim_stack`` — yields a ready-to-use :class:`SimStack` and patches the
  global ``get_stores()`` access points so the production
  ``ExtractionWorker`` reads from the in-memory fake mongo without any
  docker dependency.
* ``patch_litellm`` — monkeypatches ``litellm.acompletion`` /
  ``litellm.aembedding`` onto the harness's ``LLMMock`` so tests that
  exercise the throttle / dispatch path observe deterministic
  responses.
* ``reset_pipeline_state`` (autouse) — drops the pipeline event ring,
  the LLM throttle singleton, and the auto-overview subscriber
  registration between tests so cross-test bleed is impossible.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from tests.integration.sim_harness import (
    LLMMock,
    SimStack,
    build_sim_stack,
)


@pytest.fixture(autouse=True)
def _reset_pipeline_state() -> Iterator[None]:
    """Clear process-wide singletons between simulator tests."""
    from beever_atlas.services.llm_throttle import reset_llm_throttle_for_tests
    from beever_atlas.services.pipeline_events import get_pipeline_events

    get_pipeline_events().clear()
    reset_llm_throttle_for_tests()
    yield
    get_pipeline_events().clear()
    reset_llm_throttle_for_tests()


@pytest.fixture
def sim_stack(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimStack]:
    """Construct a SimStack and route ``get_stores()`` to the fake mongo.

    The production ExtractionWorker imports
    ``beever_atlas.stores.get_stores`` lazily inside ``tick`` and
    ``_process_channel_batch``. Patching the module attribute redirects
    every read to the harness's fake stores for the duration of the
    test.
    """
    stack = build_sim_stack()

    def _fake_get_stores() -> object:
        return stack.worker._sim_stores  # type: ignore[attr-defined]

    # Patch every import path the worker / subscriber code uses to read
    # the stores. The worker imports ``get_stores`` from
    # ``beever_atlas.stores`` inside the function body, so a module-level
    # monkeypatch covers it. The subscriber's ``_get_stores`` is already
    # overridden in ``build_sim_stack``.
    import beever_atlas.stores as stores_mod

    monkeypatch.setattr(stores_mod, "get_stores", _fake_get_stores, raising=True)

    yield stack

    # Drain any subscriber tasks the worker fanned out so test teardown
    # never leaves orphaned awaitables behind.
    pending = getattr(stack.worker, "_sim_pending_tasks", [])
    if pending:
        import asyncio

        async def _drain() -> None:
            await asyncio.gather(*pending, return_exceptions=True)

        # Best-effort drain; if the loop is already closed, skip.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Caller is responsible — pytest-asyncio cleans up.
                pass
            else:
                loop.run_until_complete(_drain())
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def patch_litellm(monkeypatch: pytest.MonkeyPatch) -> Iterator[LLMMock]:
    """Patch ``litellm.acompletion`` / ``litellm.aembedding`` onto an LLMMock.

    Used by Scenario E (throttle e2e) and Scenario I (storm recovery)
    where tests drive ``dispatch_completion`` directly rather than going
    through the simulated worker. SimStack's own LLMMock is independent
    of this fixture so non-throttle tests do not pay for litellm
    monkeypatching.
    """
    mock = LLMMock()
    import litellm  # type: ignore[import-untyped]

    monkeypatch.setattr(litellm, "acompletion", mock.acompletion)
    monkeypatch.setattr(litellm, "aembedding", mock.aembedding)
    # Make ``litellm.RateLimitError`` resolve to the harness's stub so
    # ``_is_429`` in the dispatcher catches mock-raised faults.
    from tests.integration.sim_harness import _StubRateLimit

    monkeypatch.setattr(litellm, "RateLimitError", _StubRateLimit, raising=False)

    yield mock
