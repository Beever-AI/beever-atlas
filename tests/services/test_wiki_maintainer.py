"""Unit tests for the WikiMaintainer service (PR-F).

Covers the deterministic routing contract (no LLM call in
``plan_updates``) and the per-page rewrite invariants (title
preserved, version bumped, is_dirty cleared, page voice does not
drift across iterations).

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/wiki-maintainer/``
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from beever_atlas.models.persistence import WikiPage, WikiPageSection
from beever_atlas.services.wiki_maintainer import (
    WikiMaintainer,
    _hash_fact_ids,
    _slug_for_entity,
    _slug_for_fact_type,
    _slug_for_topic,
    get_wiki_maintainer,
    init_wiki_maintainer,
)


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------


def test_slug_for_topic_prefixes_with_topic() -> None:
    assert _slug_for_topic("auth") == "topic:auth"


def test_slug_for_topic_replaces_slashes_with_dashes() -> None:
    assert _slug_for_topic("auth/sso") == "topic:auth-sso"


def test_slug_for_topic_handles_empty_cluster_id() -> None:
    assert _slug_for_topic("") == "topic:unspecified"


def test_slug_for_entity_lowercases_and_dashes() -> None:
    assert _slug_for_entity("Alice Wonderland") == "entity:alice-wonderland"


def test_slug_for_entity_returns_empty_when_input_blank() -> None:
    assert _slug_for_entity("") == ""
    assert _slug_for_entity("   ") == ""


def test_slug_for_fact_type_maps_known_roles() -> None:
    assert _slug_for_fact_type("decision") == "decisions"
    assert _slug_for_fact_type("question") == "faq"
    assert _slug_for_fact_type("action_item") == "action-items"


def test_slug_for_fact_type_returns_none_for_unmapped() -> None:
    """``observation``, ``opinion`` are not standalone pages — they
    belong on topic / entity pages alongside their cluster."""
    assert _slug_for_fact_type("observation") is None
    assert _slug_for_fact_type("opinion") is None
    assert _slug_for_fact_type("") is None


# ---------------------------------------------------------------------------
# plan_updates — deterministic routing
# ---------------------------------------------------------------------------


def _store_stub() -> Any:
    """Build a minimal WikiPageStore stub for routing-only tests."""
    stub = object.__new__(WikiMaintainer.__init__.__annotations__["page_store"])
    return stub


def _make_maintainer(page_store=None) -> WikiMaintainer:
    """Maintainer with a no-op LLM provider for routing tests."""
    if page_store is None:
        page_store = AsyncMock()
    return WikiMaintainer(page_store=page_store)


def test_plan_updates_routes_cluster_to_topic_page() -> None:
    """Spec scenario: ``Single fact touches multiple pages``."""
    m = _make_maintainer()
    plan = m.plan_updates(
        [
            {
                "id": "f1",
                "cluster_id": "auth",
                "entity_tags": [],
                "fact_type": "observation",
            }
        ]
    )
    assert plan == {"topic:auth": ["f1"]}


def test_plan_updates_routes_entity_tags_to_entity_pages() -> None:
    """``unified-llm-wiki-graph-redesign``: entity_tags route to the
    canonical ``people`` + ``glossary`` pages (single canonical pages
    each), NOT to per-entity ``entity:<slug>`` rows."""
    m = _make_maintainer()
    plan = m.plan_updates(
        [
            {
                "id": "f1",
                "cluster_id": None,
                "entity_tags": ["Alice", "Bob"],
                "fact_type": "observation",
            }
        ]
    )
    assert plan == {"people": ["f1"], "glossary": ["f1"]}


def test_plan_updates_routes_decision_to_decisions_page() -> None:
    m = _make_maintainer()
    plan = m.plan_updates(
        [
            {
                "id": "f1",
                "cluster_id": "auth",
                "entity_tags": ["alice"],
                "fact_type": "decision",
            }
        ]
    )
    assert plan == {
        "topic:auth": ["f1"],
        "people": ["f1"],
        "glossary": ["f1"],
        "decisions": ["f1"],
    }


def test_plan_updates_is_deterministic_across_runs() -> None:
    """Spec scenario: ``Routing is deterministic across runs``."""
    m = _make_maintainer()
    facts = [
        {
            "id": "f1",
            "cluster_id": "auth",
            "entity_tags": ["alice", "auth-service"],
            "fact_type": "decision",
        }
    ]
    plan_a = m.plan_updates(facts)
    plan_b = m.plan_updates(facts)
    assert plan_a == plan_b


def test_plan_updates_skips_facts_without_id() -> None:
    """A fact without a valid id is dropped from routing — better than
    silently writing to a page with empty fact provenance."""
    m = _make_maintainer()
    plan = m.plan_updates(
        [
            {"cluster_id": "auth"},  # no id
            {"id": "", "cluster_id": "auth"},  # empty id
            {"id": "f1", "cluster_id": "auth"},
        ]
    )
    assert plan == {"topic:auth": ["f1"]}


def test_plan_updates_does_NOT_call_llm() -> None:
    """Spec contract: routing must be a pure function — no LLM call.

    The maintainer is created with llm_provider=None; if any code path
    in plan_updates tried to invoke it, this would AttributeError.
    """
    m = WikiMaintainer(page_store=AsyncMock(), llm_provider=None)
    plan = m.plan_updates([{"id": "f1", "cluster_id": "auth", "entity_tags": ["alice"]}])
    assert "topic:auth" in plan


# ---------------------------------------------------------------------------
# on_extraction_done — manual mode (mark dirty)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_extraction_done_manual_marks_pages_dirty() -> None:
    """Spec scenario: ``WIKI_MAINTENANCE_MODE=manual``."""
    page_store = AsyncMock()
    page_store.mark_dirty = AsyncMock(return_value=2)
    maintainer = WikiMaintainer(page_store=page_store)

    async def _stub_load(*args, **kwargs):
        return [
            {
                "id": "f1",
                "cluster_id": "auth",
                "entity_tags": ["alice"],
                "fact_type": "decision",
            }
        ]

    maintainer._load_facts = _stub_load  # type: ignore[method-assign]
    counters = await maintainer.on_extraction_done("C1", ["f1"], mode="manual")
    # Redesign routing: topic + people + glossary + decisions.
    # Entity-tagged facts route to canonical people + glossary pages
    # (one each), not to per-entity ``entity:<slug>`` rows.
    assert counters["affected_pages"] == 4
    page_store.mark_dirty.assert_awaited_once()
    # apply_update was NOT called in manual mode.
    page_store.save_page.assert_not_awaited()


# ---------------------------------------------------------------------------
# on_extraction_done — auto mode (apply rewrite)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_extraction_done_auto_applies_rewrites() -> None:
    """Spec scenario: ``WIKI_MAINTENANCE_MODE=auto``.

    Uses ``debounce_seconds=0`` to flush inline so the synchronous
    ``rewritten`` counter reflects the per-page work. The debounce
    mechanics are exercised in ``test_wiki_maintainer_debounce.py``.
    """
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=None)  # first-touch path
    page_store.save_page = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store, debounce_seconds=0)

    async def _stub_load(*args, **kwargs):
        return [{"id": "f1", "cluster_id": "auth", "entity_tags": []}]

    async def _stub_llm(prompt: str) -> str:
        return (
            '{"affected_sections": [{"id": "overview", "title": "Overview", '
            '"content_md": "Auth fact integrated [f1]."}]}'
        )

    maintainer._load_facts = _stub_load  # type: ignore[method-assign]
    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]
    counters = await maintainer.on_extraction_done("C1", ["f1"], mode="auto")
    assert counters["rewritten"] >= 1
    page_store.save_page.assert_awaited()


@pytest.mark.asyncio
async def test_on_extraction_done_empty_fact_list_is_noop() -> None:
    page_store = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store)
    counters = await maintainer.on_extraction_done("C1", [], mode="auto")
    assert counters == {"affected_pages": 0, "marked_dirty": 0, "rewritten": 0}
    page_store.save_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_extraction_done_auto_isolates_per_page_failures() -> None:
    """A bad page rewrite must not stop other affected pages from updating.

    Mirrors the ExtractionWorker subscriber-isolation contract — this
    is the receiving end of that pipeline. ``debounce_seconds=0`` flushes
    inline so the synchronous counters reflect the per-page outcomes.
    """
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=None)
    save_results: list[Any] = [RuntimeError("flaky"), None]
    page_store.save_page = AsyncMock(side_effect=save_results)
    maintainer = WikiMaintainer(page_store=page_store, debounce_seconds=0)

    async def _stub_load(*args, **kwargs):
        return [
            {
                "id": "f1",
                "cluster_id": "auth",
                "entity_tags": ["alice"],
                "fact_type": "observation",
            }
        ]

    async def _stub_llm(prompt: str) -> str:
        return (
            '{"affected_sections": [{"id": "overview", "title": "Overview", '
            '"content_md": "Fact integrated [f1]."}]}'
        )

    maintainer._load_facts = _stub_load  # type: ignore[method-assign]
    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]
    counters = await maintainer.on_extraction_done("C1", ["f1"], mode="auto")
    # At least one page rewrote successfully; the other was logged.
    # Redesign routing: topic + people + glossary (no role page since
    # fact_type=observation isn't a role).
    assert counters["affected_pages"] == 3
    assert counters["rewritten"] >= 1


# ---------------------------------------------------------------------------
# apply_update — page-voice invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_update_preserves_title_and_slug() -> None:
    """Spec scenario: ``Maintainer updates one section of a page``.

    Title and slug MUST be byte-identical across rewrites; the whole
    point of the maintainer is that page identity is stable.
    """
    saved_pages: list[WikiPage] = []
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(
        return_value=WikiPage(
            channel_id="C1",
            target_lang="en",
            page_id="topic:auth",
            title="Authentication Architecture",
            slug="auth-architecture",
            sections=[WikiPageSection(id="overview", title="Overview", content_md="# A")],
        )
    )

    async def _capture_save(page: WikiPage) -> None:
        saved_pages.append(page)

    page_store.save_page = AsyncMock(side_effect=_capture_save)
    maintainer = WikiMaintainer(page_store=page_store)

    async def _stub_load(channel_id, fact_ids):
        return [{"id": "f1", "memory_text": "x", "cluster_id": "auth"}]

    async def _stub_llm(prompt: str) -> str:
        return (
            '{"affected_sections": [{"id": "overview", "title": "Overview", '
            '"content_md": "# A\\nUpdated [f1]."}]}'
        )

    maintainer._load_facts = _stub_load  # type: ignore[method-assign]
    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]
    await maintainer.apply_update("C1", "topic:auth", ["f1"])
    assert len(saved_pages) == 1
    saved = saved_pages[0]
    assert saved.title == "Authentication Architecture"
    assert saved.slug == "auth-architecture"


@pytest.mark.asyncio
async def test_apply_update_clears_is_dirty() -> None:
    """A successful rewrite drains the page from the manual-mode queue."""
    saved_pages: list[WikiPage] = []
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(
        return_value=WikiPage(
            channel_id="C1",
            target_lang="en",
            page_id="topic:auth",
            title="Auth",
            is_dirty=True,
        )
    )
    page_store.save_page = AsyncMock(side_effect=lambda p: saved_pages.append(p))
    maintainer = WikiMaintainer(page_store=page_store)

    async def _stub_load(channel_id, fact_ids):
        return [{"id": "f1", "memory_text": "x", "cluster_id": "auth"}]

    async def _stub_llm(prompt: str) -> str:
        return '{"affected_sections": [{"id": "overview", "title": "Overview", "content_md": "x [f1]"}]}'

    maintainer._load_facts = _stub_load  # type: ignore[method-assign]
    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]
    await maintainer.apply_update("C1", "topic:auth", ["f1"])
    assert saved_pages[0].is_dirty is False


@pytest.mark.asyncio
async def test_apply_update_records_new_fact_ids_in_last_facts_seen() -> None:
    saved_pages: list[WikiPage] = []
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(
        return_value=WikiPage(
            channel_id="C1",
            target_lang="en",
            page_id="topic:auth",
            title="Auth",
            last_facts_seen=["existing-1"],
        )
    )
    page_store.save_page = AsyncMock(side_effect=lambda p: saved_pages.append(p))
    maintainer = WikiMaintainer(page_store=page_store)

    async def _stub_load(channel_id, fact_ids):
        return [{"id": fid, "memory_text": fid, "cluster_id": "auth"} for fid in fact_ids]

    async def _stub_llm(prompt: str) -> str:
        return '{"affected_sections": [{"id": "overview", "title": "Overview", "content_md": "x"}]}'

    maintainer._load_facts = _stub_load  # type: ignore[method-assign]
    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]
    await maintainer.apply_update("C1", "topic:auth", ["f-new-1", "f-new-2"])
    assert "existing-1" in saved_pages[0].last_facts_seen
    assert "f-new-1" in saved_pages[0].last_facts_seen
    assert "f-new-2" in saved_pages[0].last_facts_seen


@pytest.mark.asyncio
async def test_apply_update_returns_false_when_no_truly_new_facts() -> None:
    """If every new_fact_id is already in last_facts_seen, the
    maintainer skips the LLM call entirely. Cost guard."""
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(
        return_value=WikiPage(
            channel_id="C1",
            target_lang="en",
            page_id="topic:auth",
            title="Auth",
            last_facts_seen=["f1", "f2"],
        )
    )
    maintainer = WikiMaintainer(page_store=page_store)
    applied = await maintainer.apply_update("C1", "topic:auth", ["f1", "f2"])
    assert applied is False
    page_store.save_page.assert_not_awaited()


# ---------------------------------------------------------------------------
# Singleton wiring
# ---------------------------------------------------------------------------


def test_init_and_get_singleton() -> None:
    page_store = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store)
    init_wiki_maintainer(maintainer)
    assert get_wiki_maintainer() is maintainer


# ---------------------------------------------------------------------------
# _hash_fact_ids — deterministic with null-byte separator
# ---------------------------------------------------------------------------


def test_hash_fact_ids_is_deterministic() -> None:
    assert _hash_fact_ids(["a", "b"]) == _hash_fact_ids(["a", "b"])


def test_hash_fact_ids_order_invariant() -> None:
    """Same set of ids in different orders → same hash."""
    assert _hash_fact_ids(["a", "b"]) == _hash_fact_ids(["b", "a"])


def test_hash_fact_ids_different_for_different_sets() -> None:
    assert _hash_fact_ids(["a", "b"]) != _hash_fact_ids(["a", "c"])


# ---------------------------------------------------------------------------
# _load_facts wiring — production path uses Weaviate
# ---------------------------------------------------------------------------


class _FakeAtomicFact:
    """Tiny duck-typed AtomicFact stand-in for the routing dict converter."""

    def __init__(
        self,
        fact_id: str,
        cluster_id: str | None = None,
        entity_tags: list[str] | None = None,
        fact_type: str = "observation",
    ) -> None:
        self.id = fact_id
        self.cluster_id = cluster_id
        self.entity_tags = entity_tags or []
        self.fact_type = fact_type


class _FakePaginatedFacts:
    def __init__(self, memories: list[_FakeAtomicFact], page: int, pages: int) -> None:
        self.memories = memories
        self.page = page
        self.pages = pages
        self.total = len(memories) * pages


class _FakeWeaviate:
    def __init__(
        self,
        ids_to_facts: dict[str, _FakeAtomicFact] | None = None,
        channel_facts: list[_FakeAtomicFact] | None = None,
        page_size: int = 500,
    ) -> None:
        self._ids = ids_to_facts or {}
        self._channel_facts = channel_facts or []
        self._page_size = page_size
        self.fetch_calls: list[list[str]] = []
        self.list_calls: list[tuple[int, int]] = []

    async def fetch_by_ids(self, fact_ids: list[str]):
        self.fetch_calls.append(list(fact_ids))
        return [self._ids[i] for i in fact_ids if i in self._ids]

    async def list_facts(self, channel_id: str, filters, page: int = 1, limit: int = 500):
        self.list_calls.append((page, limit))
        offset = (page - 1) * limit
        slice_ = self._channel_facts[offset : offset + limit]
        total = len(self._channel_facts)
        pages = max(1, (total + limit - 1) // limit)
        return _FakePaginatedFacts(slice_, page=page, pages=pages)


@pytest.fixture
def _fake_stores(monkeypatch):
    """Patch ``stores.get_stores`` to inject a fake weaviate."""
    fake_weaviate = _FakeWeaviate()

    class _StoresContainer:
        weaviate = fake_weaviate

    container = _StoresContainer()

    monkeypatch.setattr(
        "beever_atlas.stores.get_stores",
        lambda: container,
    )
    return container


@pytest.mark.asyncio
async def test_load_facts_by_explicit_ids_uses_fetch_by_ids(_fake_stores) -> None:
    _fake_stores.weaviate._ids = {
        "f1": _FakeAtomicFact("f1", cluster_id="auth", entity_tags=["alice"], fact_type="decision"),
        "f2": _FakeAtomicFact("f2", cluster_id="ops", entity_tags=[], fact_type="observation"),
    }
    page_store = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store)

    out = await maintainer._load_facts("C1", ["f1", "f2"])

    assert len(out) == 2
    # Routing dict carries the four routing keys plus memory_text +
    # source_message_id (used by apply_update's prompt). Assert the
    # routing keys are correct without locking the test to the exact
    # extra-key set.
    assert out[0]["id"] == "f1"
    assert out[0]["cluster_id"] == "auth"
    assert out[0]["entity_tags"] == ["alice"]
    assert out[0]["fact_type"] == "decision"
    assert out[1]["fact_type"] == "observation"
    # Used the explicit-id path; no list_facts scan
    assert _fake_stores.weaviate.fetch_calls == [["f1", "f2"]]
    assert _fake_stores.weaviate.list_calls == []


@pytest.mark.asyncio
async def test_load_facts_channel_wide_pages_through_all(_fake_stores) -> None:
    # 1200 facts across 3 pages of 500 each (last page has 200)
    _fake_stores.weaviate._channel_facts = [
        _FakeAtomicFact(f"f{i}", cluster_id="c", fact_type="observation") for i in range(1200)
    ]
    page_store = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store)

    out = await maintainer._load_facts("C1", None)

    assert len(out) == 1200
    # Three list_facts calls (page 1, 2, 3) with limit=500
    assert [c[0] for c in _fake_stores.weaviate.list_calls] == [1, 2, 3]
    assert all(c[1] == 500 for c in _fake_stores.weaviate.list_calls)


@pytest.mark.asyncio
async def test_load_facts_caps_at_5000_and_emits_warning(_fake_stores, monkeypatch) -> None:
    # 6500 synthetic facts — cap should kick in at 5000
    _fake_stores.weaviate._channel_facts = [
        _FakeAtomicFact(f"f{i}", cluster_id="c") for i in range(6500)
    ]
    page_store = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store)

    # The ``beever_atlas`` logger sets ``propagate=False`` in app startup,
    # so pytest's caplog (which attaches to root) won't see records. Spy
    # on the module logger directly.
    warnings_seen: list[str] = []
    from beever_atlas.services import wiki_maintainer as wm_mod

    real_warning = wm_mod.logger.warning

    def _capture(msg, *args, **kwargs):
        try:
            warnings_seen.append(msg % args if args else msg)
        except TypeError:
            warnings_seen.append(str(msg))
        real_warning(msg, *args, **kwargs)

    monkeypatch.setattr(wm_mod.logger, "warning", _capture)

    out = await maintainer._load_facts("C1", None)

    assert len(out) == 5000
    assert any(
        "wiki_maintainer_fact_load_truncated" in m and "channel_id=C1" in m for m in warnings_seen
    )


# ---------------------------------------------------------------------------
# apply_update — real LLM call (mocked) replaces the placeholder
# ---------------------------------------------------------------------------


def _make_existing_page() -> WikiPage:
    return WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="topic:auth",
        title="Authentication & Authorization",
        slug="topic-auth",
        page_voice_seed="formal-technical-3rd-person",
        sections=[
            WikiPageSection(
                id="overview", title="Overview", content_md="OIDC across all services."
            ),
            WikiPageSection(id="decisions", title="Decisions", content_md="- Use Keycloak."),
            WikiPageSection(id="risks", title="Risks", content_md="- Token leakage."),
        ],
        last_facts_seen=["f1", "f2"],
    )


@pytest.mark.asyncio
async def test_apply_update_uses_llm_response_not_placeholder(_fake_stores) -> None:
    """The smoking-gun regression test: saved content_md MUST NOT be the
    legacy ``"New facts integrated: f7"`` placeholder.
    """
    _fake_stores.weaviate._ids = {
        "f7": _FakeAtomicFact("f7", cluster_id="auth", entity_tags=["alice"], fact_type="decision"),
    }
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=_make_existing_page())
    page_store.save_page = AsyncMock()

    maintainer = WikiMaintainer(page_store=page_store)
    # Mock the LLM to return a real JSON section diff.

    async def _fake_llm(prompt: str) -> str:
        return (
            '{"affected_sections": [{"id": "decisions", "title": "Decisions", '
            '"content_md": "- Use Keycloak.\\n- Mandate MFA for admins [f7]."}], '
            '"reason": "new MFA decision"}'
        )

    maintainer._invoke_apply_update_llm = _fake_llm  # type: ignore[method-assign]

    applied = await maintainer.apply_update("C1", "topic:auth", ["f7"])

    assert applied is True
    saved_page: WikiPage = page_store.save_page.call_args.args[0]
    decisions = next(s for s in saved_page.sections if s.id == "decisions")
    # The actual saved content_md is the LLM output, not the legacy placeholder.
    assert decisions.content_md != "New facts integrated: f7"
    assert "MFA" in decisions.content_md
    assert "[f7]" in decisions.content_md


@pytest.mark.asyncio
async def test_apply_update_preserves_title_slug_and_voice(_fake_stores) -> None:
    """Title / slug / page_voice_seed are byte-identical across LLM rewrite."""
    _fake_stores.weaviate._ids = {
        "f9": _FakeAtomicFact("f9", cluster_id="auth", fact_type="decision"),
    }
    original = _make_existing_page()
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=original)
    page_store.save_page = AsyncMock()

    maintainer = WikiMaintainer(page_store=page_store)

    async def _fake_llm(prompt: str) -> str:
        return (
            '{"affected_sections": [{"id": "decisions", "title": "Decisions", '
            '"content_md": "- Use Keycloak.\\n- Adopt OIDC [f9]."}]}'
        )

    maintainer._invoke_apply_update_llm = _fake_llm  # type: ignore[method-assign]
    await maintainer.apply_update("C1", "topic:auth", ["f9"])

    saved: WikiPage = page_store.save_page.call_args.args[0]
    assert saved.title == "Authentication & Authorization"
    assert saved.slug == "topic-auth"
    assert saved.page_voice_seed == "formal-technical-3rd-person"


@pytest.mark.asyncio
async def test_apply_update_preserves_section_order(_fake_stores) -> None:
    """Regression: rewriting a middle section MUST NOT shift it to the
    end. Code-review HIGH finding — the merge had been list-comprehension
    + extend, which dropped affected sections to the bottom.
    """
    _fake_stores.weaviate._ids = {
        "f9": _FakeAtomicFact("f9", cluster_id="auth", fact_type="decision"),
    }
    page = _make_existing_page()
    # Sanity check: the original order is overview / decisions / risks.
    assert [s.id for s in page.sections] == ["overview", "decisions", "risks"]

    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=page)
    page_store.save_page = AsyncMock()

    maintainer = WikiMaintainer(page_store=page_store)

    async def _fake_llm(prompt: str) -> str:
        # Rewrite the middle section. After merge, order MUST still be
        # overview / decisions / risks — NOT overview / risks / decisions.
        return (
            '{"affected_sections": [{"id": "decisions", "title": "Decisions", '
            '"content_md": "- Use Keycloak.\\n- Adopt OIDC [f9]."}]}'
        )

    maintainer._invoke_apply_update_llm = _fake_llm  # type: ignore[method-assign]
    await maintainer.apply_update("C1", "topic:auth", ["f9"])

    saved: WikiPage = page_store.save_page.call_args.args[0]
    assert [s.id for s in saved.sections] == ["overview", "decisions", "risks"]


@pytest.mark.asyncio
async def test_apply_update_appends_new_sections_after_existing(_fake_stores) -> None:
    """A truly new section (id not on the page) is appended at the end —
    not interleaved into the existing order.
    """
    _fake_stores.weaviate._ids = {
        "f9": _FakeAtomicFact("f9", cluster_id="auth", fact_type="action_item"),
    }
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=_make_existing_page())
    page_store.save_page = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store)

    async def _fake_llm(prompt: str) -> str:
        return (
            '{"affected_sections": ['
            '{"id": "decisions", "title": "Decisions", "content_md": "- Updated [f9]."},'
            '{"id": "next-steps", "title": "Next Steps", "content_md": "- Roll out MFA"}'
            "]}"
        )

    maintainer._invoke_apply_update_llm = _fake_llm  # type: ignore[method-assign]
    await maintainer.apply_update("C1", "topic:auth", ["f9"])

    saved: WikiPage = page_store.save_page.call_args.args[0]
    # decisions stays in place at index 1; the brand-new ``next-steps``
    # is appended at the end (index 3).
    assert [s.id for s in saved.sections] == ["overview", "decisions", "risks", "next-steps"]


@pytest.mark.asyncio
async def test_apply_update_unaffected_sections_byte_identical(_fake_stores) -> None:
    _fake_stores.weaviate._ids = {
        "f9": _FakeAtomicFact("f9", cluster_id="auth", fact_type="decision"),
    }
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=_make_existing_page())
    page_store.save_page = AsyncMock()

    maintainer = WikiMaintainer(page_store=page_store)

    async def _fake_llm(prompt: str) -> str:
        # LLM only returns the "decisions" section; "overview" and "risks"
        # MUST stay byte-identical to their original content.
        return (
            '{"affected_sections": [{"id": "decisions", "title": "Decisions", '
            '"content_md": "- Use Keycloak.\\n- Adopt OIDC [f9]."}]}'
        )

    maintainer._invoke_apply_update_llm = _fake_llm  # type: ignore[method-assign]
    await maintainer.apply_update("C1", "topic:auth", ["f9"])

    saved: WikiPage = page_store.save_page.call_args.args[0]
    overview = next(s for s in saved.sections if s.id == "overview")
    risks = next(s for s in saved.sections if s.id == "risks")
    assert overview.content_md == "OIDC across all services."
    assert risks.content_md == "- Token leakage."


@pytest.mark.asyncio
async def test_apply_update_returns_false_and_skips_save_on_llm_failure(_fake_stores) -> None:
    _fake_stores.weaviate._ids = {
        "f9": _FakeAtomicFact("f9", cluster_id="auth", fact_type="decision"),
    }
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=_make_existing_page())
    page_store.save_page = AsyncMock()

    maintainer = WikiMaintainer(page_store=page_store)

    async def _failing_llm(prompt: str) -> str:
        raise RuntimeError("LLM provider 503")

    maintainer._invoke_apply_update_llm = _failing_llm  # type: ignore[method-assign]
    applied = await maintainer.apply_update("C1", "topic:auth", ["f9"])

    assert applied is False
    page_store.save_page.assert_not_called()


@pytest.mark.asyncio
async def test_apply_update_preserves_voice_across_three_consecutive_updates(_fake_stores) -> None:
    _fake_stores.weaviate._ids = {
        f"f{i}": _FakeAtomicFact(f"f{i}", cluster_id="auth", fact_type="observation")
        for i in (10, 11, 12)
    }
    original = _make_existing_page()
    saved_titles: list[str] = []
    saved_voice_seeds: list[str] = []

    state: dict[str, WikiPage] = {"page": original}

    async def _get_page(channel_id, page_id, target_lang="en"):
        return state["page"]

    async def _save_page(page: WikiPage):
        saved_titles.append(page.title)
        saved_voice_seeds.append(page.page_voice_seed)
        state["page"] = page

    page_store = AsyncMock()
    page_store.get_page = _get_page
    page_store.save_page = _save_page

    maintainer = WikiMaintainer(page_store=page_store)

    iter_idx = {"i": 0}

    async def _fake_llm(prompt: str) -> str:
        iter_idx["i"] += 1
        return (
            f'{{"affected_sections": [{{"id": "overview", "title": "Overview", '
            f'"content_md": "Iteration {iter_idx["i"]}."}}]}}'
        )

    maintainer._invoke_apply_update_llm = _fake_llm  # type: ignore[method-assign]

    for fid in ["f10", "f11", "f12"]:
        await maintainer.apply_update("C1", "topic:auth", [fid])

    assert saved_titles == ["Authentication & Authorization"] * 3
    assert saved_voice_seeds == ["formal-technical-3rd-person"] * 3


# ---------------------------------------------------------------------------
# Prompt + parser unit tests
# ---------------------------------------------------------------------------


def test_render_apply_update_prompt_includes_existing_sections_and_new_facts() -> None:
    from beever_atlas.services.wiki_maintainer import _render_apply_update_prompt

    page = _make_existing_page()
    new_facts = [
        {
            "id": "f7",
            "memory_text": "MFA mandated for admin accounts.",
            "cluster_id": "auth",
            "entity_tags": ["alice"],
            "fact_type": "decision",
        }
    ]
    prompt = _render_apply_update_prompt(page, new_facts)
    # Existing sections present
    assert "OIDC across all services." in prompt
    # New fact present with id + memory_text
    assert "MFA mandated for admin accounts." in prompt
    assert "f7" in prompt
    # System contract present
    assert "Return ONLY the sections that need to change" in prompt
    assert "affected_sections" in prompt


def test_parse_apply_update_response_returns_sections() -> None:
    from beever_atlas.services.wiki_maintainer import _parse_apply_update_response

    raw = (
        '{"affected_sections": [{"id": "overview", "title": "Overview", '
        '"content_md": "Hello world"}]}'
    )
    sections = _parse_apply_update_response(raw)
    assert len(sections) == 1
    assert sections[0].id == "overview"
    assert sections[0].content_md == "Hello world"


def test_parse_apply_update_response_returns_empty_on_malformed_json() -> None:
    from beever_atlas.services.wiki_maintainer import _parse_apply_update_response

    sections = _parse_apply_update_response("{not valid json")
    assert sections == []


def test_parse_apply_update_response_drops_entries_with_empty_id_or_content() -> None:
    from beever_atlas.services.wiki_maintainer import _parse_apply_update_response

    raw = (
        '{"affected_sections": ['
        '{"id": "", "title": "Bad", "content_md": "x"},'
        '{"id": "ok", "title": "Ok", "content_md": ""},'
        '{"id": "valid", "title": "Valid", "content_md": "yes"}'
        "]}"
    )
    sections = _parse_apply_update_response(raw)
    assert len(sections) == 1
    assert sections[0].id == "valid"


# ---------------------------------------------------------------------------
# First-touch title resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_first_touch_title_topic_uses_cluster_label(monkeypatch) -> None:
    class _FakeCluster:
        title = "Product Roadmap Q3"

    fake_weaviate = AsyncMock()
    fake_weaviate.get_cluster = AsyncMock(return_value=_FakeCluster())

    class _Stores:
        weaviate = fake_weaviate
        entity_registry = None

    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: _Stores())
    maintainer = WikiMaintainer(page_store=AsyncMock())
    title = await maintainer._resolve_first_touch_title("topic:product-roadmap", "C1")
    assert title == "Product Roadmap Q3"
    fake_weaviate.get_cluster.assert_awaited_once_with("product-roadmap")


@pytest.mark.asyncio
async def test_resolve_first_touch_title_entity_uses_registry_canonical(monkeypatch) -> None:
    fake_registry = AsyncMock()
    # First call (un-slugified "alice yang") returns canonical
    fake_registry.get_canonical = AsyncMock(return_value="Alice Yang")

    class _Stores:
        weaviate = None
        entity_registry = fake_registry

    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: _Stores())
    maintainer = WikiMaintainer(page_store=AsyncMock())
    title = await maintainer._resolve_first_touch_title("entity:alice-yang", "C1")
    assert title == "Alice Yang"


@pytest.mark.asyncio
async def test_resolve_first_touch_title_role_pages_use_constants(monkeypatch) -> None:
    class _Stores:
        weaviate = None
        entity_registry = None

    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: _Stores())
    maintainer = WikiMaintainer(page_store=AsyncMock())
    assert await maintainer._resolve_first_touch_title("decisions", "C1") == "Decisions"
    assert await maintainer._resolve_first_touch_title("faq", "C1") == "Frequently Asked Questions"
    assert await maintainer._resolve_first_touch_title("action-items", "C1") == "Action Items"


@pytest.mark.asyncio
async def test_resolve_first_touch_title_falls_back_to_slug(monkeypatch) -> None:
    class _Stores:
        weaviate = None
        entity_registry = None

    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: _Stores())
    maintainer = WikiMaintainer(page_store=AsyncMock())
    title = await maintainer._resolve_first_touch_title("topic:unknown-topic", "C1")
    assert title == "Unknown Topic"


# ---------------------------------------------------------------------------
# on_consolidation_complete — replaces legacy mark_all_stale hammer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_consolidation_complete_with_fact_ids_routes_in_auto(_fake_stores) -> None:
    """memory-then-wiki realignment: consolidation_complete routes facts via
    the accumulator path (on_memory_changed) and does NOT fire apply_update
    or save_page inline — the terminal flush is owned by memory_settled.

    The ``mode`` argument is accepted for backwards-compatibility but is
    ignored: routing is always queue-only.
    """
    _fake_stores.weaviate._ids = {
        "f10": _FakeAtomicFact(
            "f10", cluster_id="auth", entity_tags=["alice"], fact_type="decision"
        ),
        "f11": _FakeAtomicFact("f11", cluster_id="ops", entity_tags=[], fact_type="observation"),
    }
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=None)
    page_store.save_page = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store, debounce_seconds=0)

    counters = await maintainer.on_consolidation_complete("C1", ["f10", "f11"], mode="auto")
    # Routing is unchanged:
    #   f10 → topic:auth + people + glossary + decisions = 4 pages.
    #   f11 → topic:ops (no entity_tags, no role) = 1 page.
    # Total affected: 5 pages.
    assert counters["affected_pages"] == 5
    # Critically: NO inline save_page calls — the realignment moves the
    # flush to memory_settled. This is the regression test for the
    # mid-sync wiki rewrite bug.
    page_store.save_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_consolidation_complete_with_fact_ids_marks_dirty_in_manual(_fake_stores) -> None:
    """memory-then-wiki realignment: consolidation_complete is queue-only in
    BOTH manual and auto mode. The legacy distinction (manual marks dirty
    via page_store, auto fires apply_update) is gone — both paths accumulate
    into the internal dirty set for the terminal flush owned by
    memory_settled."""
    _fake_stores.weaviate._ids = {
        "f10": _FakeAtomicFact(
            "f10", cluster_id="auth", entity_tags=["alice"], fact_type="decision"
        ),
    }
    page_store = AsyncMock()
    page_store.mark_dirty = AsyncMock(return_value=0)
    page_store.save_page = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store)

    counters = await maintainer.on_consolidation_complete("C1", ["f10"], mode="manual")
    # Routing still surfaces affected pages so callers can observe scope.
    assert counters["affected_pages"] >= 1
    # No inline mark_dirty or save_page — flush is deferred to
    # memory_settled (or to operator-triggered maintain_now).
    page_store.save_page.assert_not_awaited()
    page_store.mark_dirty.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_consolidation_complete_with_empty_fact_ids_is_noop(_fake_stores) -> None:
    """Spec scenario: empty fact_ids → maintainer is a no-op."""
    page_store = AsyncMock()
    page_store.save_page = AsyncMock()
    page_store.mark_dirty = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store)

    counters = await maintainer.on_consolidation_complete("C1", [], mode="auto")

    # on_memory_changed returns its own counter shape — affected_pages=0
    # is the load-bearing assertion. The flush counters from the legacy
    # on_extraction_done path are no longer relevant here.
    assert counters.get("affected_pages", 0) == 0
    page_store.save_page.assert_not_awaited()
    page_store.mark_dirty.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_first_touch_title_swallows_lookup_errors(monkeypatch) -> None:
    """A Weaviate hiccup must not block page creation — just fall back."""
    fake_weaviate = AsyncMock()
    fake_weaviate.get_cluster = AsyncMock(side_effect=RuntimeError("weaviate down"))

    class _Stores:
        weaviate = fake_weaviate
        entity_registry = None

    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: _Stores())
    maintainer = WikiMaintainer(page_store=AsyncMock())
    title = await maintainer._resolve_first_touch_title("topic:auth", "C1")
    assert title == "Auth"


@pytest.mark.asyncio
async def test_load_facts_returns_empty_when_no_weaviate(monkeypatch) -> None:
    """If the stores singleton has no weaviate (rare init order), return [] rather than raise."""

    class _NoWeaviate:
        pass

    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: _NoWeaviate())
    page_store = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store)

    out = await maintainer._load_facts("C1", ["f1"])

    assert out == []
