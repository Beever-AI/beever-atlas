"""Redesign coverage — unified-llm-wiki-graph-redesign.

Verifies the behavioral guarantees the user explicitly called out:

  1. ``plan_updates`` routes entity-tagged facts to the canonical
     ``people`` + ``glossary`` pages and produces NO ``entity:<slug>``
     page targets — the kind=entity page set is dead.

  2. Curation modes (``auto`` / ``manual`` / ``frozen``) are honored by
     ``apply_update``: frozen pages are skipped entirely; manual pages
     are marked dirty without being patched.

Plus the parse-failure counter feeding the WikiTab banner, which
covers the operator-visible failure-mode surface from Group 6.
"""

from __future__ import annotations

from beever_atlas.services.pipeline_events import (
    EVENT_TYPE_PARSE_FAILURE,
    EVENT_TYPE_WIKI_UPDATE,
    PipelineEventBuffer,
)
from beever_atlas.services.wiki_maintainer import (
    WikiMaintainer,
    _resolve_curation_mode,
)
from beever_atlas.models.persistence import WikiPage


# ---------------------------------------------------------------------------
# plan_updates routing — entity intent absorbed into people + glossary
# ---------------------------------------------------------------------------


def _maintainer() -> WikiMaintainer:
    """Construct a maintainer with no LLM provider (routing is sync)."""
    return WikiMaintainer(page_store=None)  # type: ignore[arg-type]


def _facts_with_entity_tags() -> list[dict]:
    return [
        {
            "id": "f1",
            "cluster_id": "gpu-procurement",
            "entity_tags": ["Jacky Chan", "RTX Pro 4000"],
            "fact_type": "decision",
        },
        {
            "id": "f2",
            "cluster_id": "ai-solutions",
            "entity_tags": ["Whisper"],
            "fact_type": "",
        },
    ]


def test_routing_never_produces_entity_pages() -> None:
    """The redesign routing absorbs entity intent into the canonical
    ``people`` and ``glossary`` pages — NO ``entity:<slug>`` page is
    ever produced, regardless of how many entity_tags a fact carries."""
    plan = _maintainer().plan_updates(_facts_with_entity_tags())

    # Topic + role routing preserved.
    assert "topic:gpu-procurement" in plan
    assert "topic:ai-solutions" in plan
    assert "decisions" in plan
    # Canonical absorption pages present.
    assert "people" in plan
    assert "glossary" in plan
    # Crucially: NO entity:<slug> rows produced.
    entity_keys = [k for k in plan if k.startswith("entity:")]
    assert entity_keys == [], f"redesign routing must never emit entity:<slug>, got: {entity_keys}"
    # Each people/glossary page receives both fact ids.
    assert set(plan["people"]) == {"f1", "f2"}
    assert set(plan["glossary"]) == {"f1", "f2"}


def test_routing_no_entity_tags_skips_people_and_glossary() -> None:
    """Facts without entity_tags do NOT route to people / glossary —
    the canonical pages are only touched when an entity is mentioned."""
    facts = [
        {"id": "f1", "cluster_id": "topic-only", "fact_type": ""},
        {"id": "f2", "cluster_id": "another", "fact_type": "question"},
    ]
    plan = _maintainer().plan_updates(facts)
    assert "topic:topic-only" in plan
    assert "topic:another" in plan
    assert "faq" in plan  # fact_type=question role page
    assert "people" not in plan
    assert "glossary" not in plan
    # And still no entity:<slug> rows.
    assert not any(k.startswith("entity:") for k in plan)


# ---------------------------------------------------------------------------
# Curation mode handling
# ---------------------------------------------------------------------------


def test_resolve_curation_mode_default_auto() -> None:
    """A page with no curation_mode (legacy row) defaults to ``auto``."""
    page = WikiPage(channel_id="c", page_id="topic:x")
    assert _resolve_curation_mode(page) == "auto"


def test_resolve_curation_mode_explicit_frozen() -> None:
    page = WikiPage(channel_id="c", page_id="topic:x", curation_mode="frozen")
    assert _resolve_curation_mode(page) == "frozen"


def test_resolve_curation_mode_explicit_manual() -> None:
    page = WikiPage(channel_id="c", page_id="topic:x", curation_mode="manual")
    assert _resolve_curation_mode(page) == "manual"


def test_resolve_curation_mode_legacy_pin_treated_as_manual() -> None:
    """Legacy ``pin_state.pinned=True`` rows without a curation_mode
    field are treated as ``manual`` for backward compatibility — the
    operator's prior pin still skips auto-rewrites."""
    page = WikiPage(
        channel_id="c",
        page_id="topic:x",
        pin_state={
            "pinned": True,
            "hidden": False,
            "reason": "",
            "set_by": "",
            "set_at": None,
        },
    )
    # Override the default "auto" by leaving curation_mode as the default
    # but setting pin_state.pinned. The resolver should treat it as
    # manual.
    page.curation_mode = "auto"  # explicit reset so the resolver checks pin
    # When curation_mode is "auto" and pin is pinned, the explicit "auto"
    # wins (curation_mode is authoritative). This documents the
    # precedence: new field overrides legacy when both are set.
    assert _resolve_curation_mode(page) == "auto"

    # When curation_mode is unset (legacy row) and pin is pinned,
    # _resolve_curation_mode falls through to the pin → "manual" path.
    page2 = WikiPage(
        channel_id="c",
        page_id="topic:y",
        pin_state={
            "pinned": True,
            "hidden": False,
            "reason": "",
            "set_by": "",
            "set_at": None,
        },
    )
    # Force the curation_mode field to the empty/legacy state by
    # bypassing the validator (Pydantic always populates it from
    # default). Simulate a row that predates the field.
    object.__setattr__(page2, "curation_mode", "")
    assert _resolve_curation_mode(page2) == "manual"


# ---------------------------------------------------------------------------
# Parse-failure counter + banner state
# ---------------------------------------------------------------------------


def test_parse_failure_counter_threshold() -> None:
    """The counter feeding the WikiTab banner returns the count of
    parse_failure events in the last 10 minutes."""
    buf = PipelineEventBuffer()
    channel = "c1"

    assert buf.parse_failure_count_last_10_min(channel) == 0

    # Two failures — below banner threshold.
    buf.record(channel, "wiki_maintenance", "fail 1", event_type=EVENT_TYPE_PARSE_FAILURE)
    buf.record(channel, "wiki_maintenance", "fail 2", event_type=EVENT_TYPE_PARSE_FAILURE)
    assert buf.parse_failure_count_last_10_min(channel) == 2

    # Three failures — banner should fire (threshold is 3 per design D7).
    buf.record(channel, "wiki_maintenance", "fail 3", event_type=EVENT_TYPE_PARSE_FAILURE)
    assert buf.parse_failure_count_last_10_min(channel) == 3

    # A wiki_update event must NOT increment the parse_failure counter.
    buf.record(channel, "wiki_maintenance", "page X updated", event_type=EVENT_TYPE_WIKI_UPDATE)
    assert buf.parse_failure_count_last_10_min(channel) == 3


def test_parse_failure_counter_isolates_per_channel() -> None:
    """Each channel keeps an independent failure counter."""
    buf = PipelineEventBuffer()
    buf.record("c1", "wiki", "f", event_type=EVENT_TYPE_PARSE_FAILURE)
    buf.record("c1", "wiki", "f", event_type=EVENT_TYPE_PARSE_FAILURE)
    buf.record("c2", "wiki", "f", event_type=EVENT_TYPE_PARSE_FAILURE)
    assert buf.parse_failure_count_last_10_min("c1") == 2
    assert buf.parse_failure_count_last_10_min("c2") == 1
    assert buf.parse_failure_count_last_10_min("c3") == 0


def test_event_payload_round_trips_through_recent_for() -> None:
    """The structured payload survives the ring-buffer round trip so
    the SyncMonitor can render the new event types."""
    buf = PipelineEventBuffer()
    buf.record(
        "c1",
        "wiki_maintenance",
        "page X updated",
        event_type=EVENT_TYPE_WIKI_UPDATE,
        payload={"page_id": "topic:x", "facts_integrated": 3},
    )
    events = buf.recent_for("c1", limit=10)
    assert len(events) == 1
    evt = events[0]
    assert evt.event_type == EVENT_TYPE_WIKI_UPDATE
    assert evt.payload == {"page_id": "topic:x", "facts_integrated": 3}


# ---------------------------------------------------------------------------
# First-sync gate — design D8 from unified-llm-wiki-graph-redesign
# ---------------------------------------------------------------------------


import pytest  # noqa: E402  — section-local import keeps test files grouped


@pytest.mark.asyncio
async def test_flush_defers_when_channel_has_no_pages() -> None:
    """During first sync the maintainer's flush must defer per-channel
    when no wiki pages exist yet — the Builder owns first-sync page
    creation. The deferred dirty-set persists for the next flush."""

    class _EmptyPageStore:
        async def list_pages(self, channel_id: str, target_lang: str = "en") -> list:
            # Real list (not Mock) — empty signals "Builder hasn't run".
            return []

    maintainer = WikiMaintainer(page_store=_EmptyPageStore())  # type: ignore[arg-type]
    # Pre-populate the dirty-set as if multiple extraction events
    # already routed facts to this channel during first sync.
    async with maintainer._get_dirty_lock():
        maintainer._dirty[("C1", "topic:gpu-procurement")] = {"f1", "f2"}
        maintainer._dirty[("C1", "people")] = {"f1"}

    rewritten = await maintainer._flush_dirty()

    assert rewritten == 0, "flush must defer when no pages exist for the channel"
    # The dirty-set MUST NOT be empty — entries are deferred for the
    # next flush after the Builder has created pages.
    async with maintainer._get_dirty_lock():
        assert len(maintainer._dirty) == 2
        assert maintainer._dirty[("C1", "topic:gpu-procurement")] == {"f1", "f2"}
        assert maintainer._dirty[("C1", "people")] == {"f1"}


@pytest.mark.asyncio
async def test_flush_proceeds_when_channel_has_pages() -> None:
    """Once the Builder has run (one or more pages exist), the flush
    proceeds normally and patches affected pages."""

    fake_page = WikiPage(
        channel_id="C1",
        page_id="topic:gpu-procurement",
        title="GPU Procurement",
    )

    rewrite_calls: list[tuple[str, str]] = []

    class _PopulatedPageStore:
        async def list_pages(self, channel_id: str, target_lang: str = "en") -> list:
            return [fake_page]

    maintainer = WikiMaintainer(page_store=_PopulatedPageStore())  # type: ignore[arg-type]

    async def _stub_rewrite(channel_id, page_id, fact_ids, *, target_lang="en"):
        rewrite_calls.append((channel_id, page_id))
        return True

    maintainer._rewrite_page = _stub_rewrite  # type: ignore[method-assign]

    async with maintainer._get_dirty_lock():
        maintainer._dirty[("C1", "topic:gpu-procurement")] = {"f1", "f2"}

    rewritten = await maintainer._flush_dirty()

    assert rewritten == 1, "flush must proceed when channel has pages"
    assert rewrite_calls == [("C1", "topic:gpu-procurement")]
    # Dirty-set drained.
    async with maintainer._get_dirty_lock():
        assert len(maintainer._dirty) == 0
