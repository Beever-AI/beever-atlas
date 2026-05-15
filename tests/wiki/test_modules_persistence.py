"""Round-trip tests for the new ``modules`` field on WikiPage.

Verifies:
- Legacy rows (without a ``modules`` field) deserialize as ``modules: []``
- New rows preserve their modules list across persistence round-trips
- Domain WikiPage and persistence WikiPage both have the field with
  the same default
"""

from __future__ import annotations

from beever_atlas.models.domain import WikiPage as DomainWikiPage
from beever_atlas.models.persistence import WikiPage as PersistenceWikiPage


def test_persistence_wiki_page_modules_default_empty() -> None:
    """A WikiPage created without specifying ``modules`` defaults to
    an empty list — required for legacy-row backward compatibility."""
    page = PersistenceWikiPage(
        channel_id="C_TEST",
        page_id="topic-auth",
        slug="topic-auth",
        title="Authentication",
    )
    assert page.modules == []


def test_persistence_wiki_page_modules_round_trip() -> None:
    """A page persisted with modules round-trips through ``model_dump``
    and ``model_validate`` with module IDs and anchors preserved."""
    original = PersistenceWikiPage(
        channel_id="C_TEST",
        page_id="topic-auth",
        slug="topic-auth",
        title="Authentication",
        modules=[
            {"id": "key_facts", "anchor": "kf1"},
            {"id": "decision_log", "anchor": "dl1", "data": {"row_count": 3}},
        ],
    )
    dumped = original.model_dump()
    restored = PersistenceWikiPage.model_validate(dumped)
    assert restored.modules == original.modules
    assert restored.modules[1]["data"]["row_count"] == 3


def test_persistence_legacy_dict_deserializes_with_empty_modules() -> None:
    """Mongo docs persisted before this change have NO ``modules`` key.
    Pydantic must default to an empty list — never raise."""
    legacy_doc = {
        "channel_id": "C_TEST",
        "page_id": "topic-old",
        "slug": "topic-old",
        "title": "Old Topic",
        # No "modules" key — simulates pre-change row.
    }
    page = PersistenceWikiPage.model_validate(legacy_doc)
    assert page.modules == []


def test_domain_wiki_page_modules_default_empty() -> None:
    """The domain WikiPage (used by the API/serializer) shares the
    same default — round-trips on the API side stay consistent."""
    page = DomainWikiPage(id="topic-auth", slug="topic-auth", title="Authentication")
    assert page.modules == []


def test_domain_wiki_page_modules_round_trip() -> None:
    """Domain model round-trips its modules list intact."""
    original = DomainWikiPage(
        id="topic-auth",
        slug="topic-auth",
        title="Authentication",
        modules=[{"id": "open_questions", "anchor": "oq1"}],
    )
    dumped = original.model_dump()
    restored = DomainWikiPage.model_validate(dumped)
    assert restored.modules == original.modules


# ---------------------------------------------------------------------------
# wiki-narrative-articles — narrative_sections persistence
# ---------------------------------------------------------------------------


def test_persistence_wiki_page_narrative_sections_default_empty() -> None:
    """A WikiPage created without specifying ``narrative_sections``
    defaults to an empty list — required for backward compatibility
    with pages persisted before the wiki-narrative-articles change."""
    page = PersistenceWikiPage(
        channel_id="C_TEST",
        page_id="topic-auth",
        slug="topic-auth",
        title="Authentication",
    )
    assert page.narrative_sections == []


def test_persistence_legacy_doc_no_narrative_sections_key() -> None:
    """Mongo docs persisted before wiki-narrative-articles have NO
    ``narrative_sections`` key. Pydantic must default to ``[]`` and
    NOT raise."""
    legacy_doc = {
        "channel_id": "C_TEST",
        "page_id": "topic-old",
        "slug": "topic-old",
        "title": "Old Topic",
        # No "narrative_sections" key — simulates pre-change row.
    }
    page = PersistenceWikiPage.model_validate(legacy_doc)
    assert page.narrative_sections == []


def test_persistence_wiki_page_narrative_sections_round_trip() -> None:
    """Sections persist through model_dump → model_validate intact."""
    section = {
        "anchor": "context",
        "heading": "Context",
        "paragraphs": [
            {
                "text": "The team adopted Authlib for OIDC.",
                "citations": ["f_1"],
                "is_inference": False,
            }
        ],
        "citations": ["f_1"],
        "visual": None,
        "citation_coverage": 1.0,
    }
    original = PersistenceWikiPage(
        channel_id="C_TEST",
        page_id="topic-auth",
        slug="topic-auth",
        title="Authentication",
        narrative_sections=[section],
    )
    dumped = original.model_dump()
    restored = PersistenceWikiPage.model_validate(dumped)
    assert restored.narrative_sections == original.narrative_sections
    assert restored.narrative_sections[0]["anchor"] == "context"


def test_domain_wiki_page_narrative_sections_default_empty() -> None:
    """Domain WikiPage shares the same default — required for the
    compiler-output round-trip (the compiler returns ``domain.WikiPage``;
    builder calls ``model_dump`` and stuffs the dict into the legacy
    cache; per-page persistence reads it back via
    ``persistence.WikiPage.model_validate``). Without this default,
    legacy domain pages would crash on validation."""
    page = DomainWikiPage(id="topic-auth", slug="topic-auth", title="Authentication")
    assert page.narrative_sections == []


def test_domain_to_persistence_narrative_sections_round_trip() -> None:
    """C-1 regression: ``narrative_sections`` survives the
    domain → ``model_dump`` → persistence path the WikiBuilder /
    compiler use to write the legacy cache row that the per-page store
    reads back. Without ``narrative_sections`` on the DOMAIN model,
    the compiler's output would be silently dropped between compile +
    persistence even though the validator + orchestrator produced it.
    """
    section = {
        "anchor": "context",
        "heading": "Integrate OpenClaw",
        "paragraphs": [
            {
                "text": "The team chose OpenClaw as primary connector.",
                "citations": ["f_12"],
                "is_inference": False,
            }
        ],
        "citations": ["f_12"],
        "visual": None,
        "citation_coverage": 1.0,
    }
    domain_page = DomainWikiPage(
        id="topic-openclaw",
        slug="topic-openclaw",
        title="OpenClaw Integration",
        page_type="topic",
        narrative_sections=[section],
    )
    # ``model_dump(mode="json")`` is exactly what
    # ``WikiBuilder.compile_wiki_for_channel`` runs before stuffing the
    # subdoc into the legacy cache.
    dumped = domain_page.model_dump(mode="json")
    assert dumped["narrative_sections"] == [section]
    # Persistence layer reads the same dict back via ``model_validate``
    # (the path used by ``WikiCache.get_page`` when ``per_page_wiki=True``
    # falls back to legacy doc, and by the migration script).
    persistence_doc = {
        "channel_id": "C_TEST",
        "page_id": "topic-openclaw",
        "slug": "topic-openclaw",
        "title": "OpenClaw Integration",
        "narrative_sections": dumped["narrative_sections"],
    }
    persisted = PersistenceWikiPage.model_validate(persistence_doc)
    assert persisted.narrative_sections == [section]
    assert persisted.narrative_sections[0]["paragraphs"][0]["text"].startswith(
        "The team chose OpenClaw"
    )


def test_domain_to_persistence_empty_narrative_sections_default() -> None:
    """Legacy domain pages (no narrative payload) round-trip without
    losing the empty-list invariant — required for backward compat
    with pages persisted before wiki-narrative-articles."""
    domain_page = DomainWikiPage(
        id="topic-old",
        slug="topic-old",
        title="Old Topic",
    )
    dumped = domain_page.model_dump(mode="json")
    assert dumped["narrative_sections"] == []
    persisted = PersistenceWikiPage.model_validate(
        {
            "channel_id": "C_TEST",
            "page_id": "topic-old",
            "slug": "topic-old",
            "title": "Old Topic",
            "narrative_sections": dumped["narrative_sections"],
        }
    )
    assert persisted.narrative_sections == []
