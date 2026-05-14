"""Tests for the wiki-llm-native-redesign per-kind synthesis dispatcher.

Covers tasks §3.11 / §3.12 / §3.13 / §3.14 from
``openspec/changes/wiki-llm-native-redesign/tasks.md``.

The fakes here mirror the pattern used by ``tests/services/test_wiki_maintainer.py``:
AsyncMock for ``WikiPageStore``, in-process monkeypatches of
``_load_facts`` + ``_invoke_apply_update_llm`` to seed deterministic
responses without exercising the real Gemini path.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from beever_atlas.models.persistence import WikiPage, WikiPageSection
from beever_atlas.services import wiki_maintainer as wm_mod
from beever_atlas.services.wiki_maintainer import WikiMaintainer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    redesign_on: bool,
    drift_ab: bool = False,
) -> None:
    """Stub Settings so apply_update reads predictable flag values."""

    fake = SimpleNamespace(
        wiki_drift_ab=drift_ab,
        wiki_drift_ab_rate_limit_seconds=60,
        wiki_llm_native_redesign=redesign_on,
    )
    monkeypatch.setattr("beever_atlas.infra.config.get_settings", lambda: fake)


def _make_maintainer_with_page(
    *,
    page: WikiPage | None,
) -> tuple[WikiMaintainer, AsyncMock]:
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=page)
    page_store.save_page = AsyncMock()
    maintainer = WikiMaintainer(page_store=page_store)
    return maintainer, page_store


def _stub_facts(maintainer: WikiMaintainer, fact_ids: list[str]) -> None:
    """Make ``_load_facts`` resolve the given ids to minimal fact dicts."""

    async def _load(channel_id: str, ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "id": fid,
                "memory_text": f"fact-{fid}",
                "cluster_id": "auth",
                "entity_tags": [],
                "fact_type": "observation",
                "source_message_id": f"m-{fid}",
            }
            for fid in ids
        ]

    maintainer._load_facts = _load  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# §3.11 — valid response persists both content_md AND kind_schema (per kind)
# ---------------------------------------------------------------------------


_RESPONSES_PER_KIND: dict[str, dict[str, Any]] = {
    "topic": {
        "page_id": "topic:auth",
        "kind_schema": {
            "summary": "Auth uses OAuth2 with PKCE; sessions are 24h.",
            "key_decisions": ["OAuth2 with PKCE"],
            "key_people": ["Alice"],
            "key_dates": ["2026-01-15"],
            "open_questions": [],
        },
        "section_id": "overview",
    },
    "entity": {
        "page_id": "entity:alice",
        "kind_schema": {
            "name": "Alice",
            "role": "engineer",
            "owns": ["auth-service"],
            "decides": ["session-policy"],
            "contributes": ["RFC-42"],
            "associated_pages": ["Authentication"],
        },
        "section_id": "overview",
    },
    "decisions": {
        "page_id": "decisions",
        "kind_schema": {
            "decisions": [
                {
                    "title": "Adopt OAuth2 with PKCE",
                    "decided_at": "2026-01-15",
                    "decided_by": "Alice",
                    "alternatives": ["session cookies", "JWT"],
                    "rationale": "PKCE flow is safer for SPAs",
                    "affected": ["Authentication"],
                }
            ]
        },
        "section_id": "decisions",
    },
    "faq": {
        "page_id": "faq",
        "kind_schema": {
            "qas": [
                {
                    "question": "How long do sessions live?",
                    "answer": "24h with rolling refresh",
                    "asked_at": "2026-01-16",
                    "source_pages": ["Authentication"],
                }
            ]
        },
        "section_id": "faq",
    },
    "action_items": {
        "page_id": "action-items",
        "kind_schema": {
            "items": [
                {
                    "title": "Document PKCE flow",
                    "status": "in_progress",
                    "owner": "Alice",
                    "due": "2026-02-01",
                    "blocked_by": [],
                    "related_pages": ["Authentication"],
                }
            ]
        },
        "section_id": "action-items",
    },
}


@pytest.mark.parametrize("kind", sorted(_RESPONSES_PER_KIND.keys()))
async def test_valid_response_persists_content_md_and_kind_schema(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """§3.11 — every kind: a valid LLM response persists both
    ``content_md`` (in ``page.sections``) and ``kind_schema`` on the
    saved page document.
    """
    _patch_settings(monkeypatch, redesign_on=True)
    spec = _RESPONSES_PER_KIND[kind]
    maintainer, page_store = _make_maintainer_with_page(page=None)
    _stub_facts(maintainer, ["f1"])

    canned = {
        "affected_sections": [
            {
                "id": spec["section_id"],
                "title": spec["section_id"].replace("-", " ").title(),
                "content_md": f"# {kind} body — see [[Authentication]] for context.",
            }
        ],
        "kind_schema": spec["kind_schema"],
        "reason": "test",
    }

    async def _stub_llm(prompt: str) -> str:
        return json.dumps(canned)

    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]

    applied = await maintainer.apply_update(
        channel_id="C1",
        page_id=spec["page_id"],
        new_fact_ids=["f1"],
        target_lang="en",
    )
    assert applied is True
    page_store.save_page.assert_awaited_once()
    saved: WikiPage = page_store.save_page.call_args.args[0]
    # Markdown body landed.
    assert any(s.id == spec["section_id"] for s in saved.sections)
    section = next(s for s in saved.sections if s.id == spec["section_id"])
    assert "[[Authentication]]" in section.content_md
    # Structured payload landed.
    assert saved.kind_schema == spec["kind_schema"]
    # Page kind reflects the dispatched kind (operator-set or derived).
    assert saved.kind == kind


# ---------------------------------------------------------------------------
# §3.12 — invalid JSON triggers single retry; 2× failure saves markdown
# only + emits wiki_kind_schema_validation_failed
# ---------------------------------------------------------------------------


async def test_invalid_kind_schema_retries_once_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First response has a schema-violating ``kind_schema`` → maintainer
    retries with the validation error in the prompt; second response is
    valid → page is saved with the (valid) ``kind_schema``."""

    _patch_settings(monkeypatch, redesign_on=True)
    maintainer, page_store = _make_maintainer_with_page(page=None)
    _stub_facts(maintainer, ["f1"])

    bad = {
        "affected_sections": [{"id": "overview", "title": "Overview", "content_md": "first body"}],
        # Missing required `summary` for topic kind → triggers retry.
        "kind_schema": {"key_decisions": ["foo"]},
    }
    good = {
        "affected_sections": [{"id": "overview", "title": "Overview", "content_md": "second body"}],
        "kind_schema": {
            "summary": "fixed",
            "key_decisions": [],
            "key_people": [],
            "key_dates": [],
            "open_questions": [],
        },
    }
    calls: list[str] = []

    async def _stub_llm(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(bad if len(calls) == 1 else good)

    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]

    applied = await maintainer.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    assert applied is True
    assert len(calls) == 2  # exactly one retry
    # The retry prompt must include the validation error so the LLM
    # knows what went wrong.
    assert "summary" in calls[1].lower() or "RETRY" in calls[1]
    saved: WikiPage = page_store.save_page.call_args.args[0]
    assert saved.kind_schema == good["kind_schema"]
    # Body from the SECOND (successful) attempt is what got saved.
    overview = next(s for s in saved.sections if s.id == "overview")
    assert overview.content_md == "second body"


async def test_two_invalid_responses_save_markdown_only_and_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both attempts invalid → page saved with markdown but
    ``kind_schema=None``; a ``wiki_kind_schema_validation_failed`` warning
    is emitted."""

    _patch_settings(monkeypatch, redesign_on=True)
    maintainer, page_store = _make_maintainer_with_page(page=None)
    _stub_facts(maintainer, ["f1"])

    bad = {
        "affected_sections": [
            {"id": "overview", "title": "Overview", "content_md": "best-effort body"}
        ],
        "kind_schema": {"unknown_field": "no summary key"},
    }

    async def _stub_llm(prompt: str) -> str:
        return json.dumps(bad)

    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]

    # ``beever_atlas`` logger sets ``propagate=False`` once
    # ``server.app`` is imported, so pytest's caplog can't see warnings
    # via the root logger. Attach a list-collecting handler directly
    # to the wiki_maintainer logger.
    captured: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _ListHandler(level=logging.WARNING)
    wm_mod.logger.addHandler(handler)
    try:
        applied = await maintainer.apply_update(
            channel_id="C1",
            page_id="topic:auth",
            new_fact_ids=["f1"],
            target_lang="en",
        )
    finally:
        wm_mod.logger.removeHandler(handler)

    assert applied is True  # markdown still landed
    page_store.save_page.assert_awaited_once()
    saved: WikiPage = page_store.save_page.call_args.args[0]
    assert saved.kind_schema is None
    # Markdown body still landed.
    overview = next(s for s in saved.sections if s.id == "overview")
    assert overview.content_md == "best-effort body"
    # Warning carries the structured event name.
    assert any("wiki_kind_schema_validation_failed" in rec.getMessage() for rec in captured)


# ---------------------------------------------------------------------------
# §3.13 — flag OFF: legacy single-prompt path is byte-identical
# ---------------------------------------------------------------------------


async def test_flag_off_uses_legacy_prompt_and_does_not_set_kind_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``WIKI_LLM_NATIVE_REDESIGN=False``, apply_update uses the
    legacy ``_render_apply_update_prompt`` and does NOT touch
    ``kind_schema`` even if the LLM happened to emit one."""

    _patch_settings(monkeypatch, redesign_on=False)
    maintainer, page_store = _make_maintainer_with_page(page=None)
    _stub_facts(maintainer, ["f1"])

    captured_prompts: list[str] = []
    legacy_response = {
        "affected_sections": [{"id": "overview", "title": "Overview", "content_md": "legacy body"}],
        # If the redesign accidentally fired, this would land on the
        # page; the assertion below proves it does NOT.
        "kind_schema": {"summary": "should be ignored on legacy path"},
    }

    async def _stub_llm(prompt: str) -> str:
        captured_prompts.append(prompt)
        return json.dumps(legacy_response)

    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]

    applied = await maintainer.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    assert applied is True
    saved: WikiPage = page_store.save_page.call_args.args[0]
    # The legacy path leaves kind_schema at its model default (None).
    assert saved.kind_schema is None
    # Legacy prompt does NOT include the per-kind retry header.
    assert "--- RETRY ---" not in captured_prompts[0]
    # Legacy prompt has the legacy system header, not the per-kind one.
    assert "SYSTEM — Topic page synthesis" not in captured_prompts[0]


# ---------------------------------------------------------------------------
# §3.14 — topic-kind page and entity-kind page produce structurally
# DIFFERENT outputs (different schema fields populated)
# ---------------------------------------------------------------------------


async def test_topic_and_entity_pages_produce_structurally_different_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two pages of different kinds rewritten in the same batch end up
    with structurally different ``kind_schema`` fields populated. This
    is the whole point of the redesign — the user's complaint that
    'pages all read the same' was driven by a single generic prompt."""

    _patch_settings(monkeypatch, redesign_on=True)

    # Two separate maintainer invocations against two separate pages, but
    # the same LLM stub picks the kind from prompt content so we exercise
    # both prompts in one test.
    topic_response = {
        "affected_sections": [{"id": "overview", "title": "Overview", "content_md": "topic body"}],
        "kind_schema": {
            "summary": "Auth uses OAuth2",
            "key_decisions": ["OAuth2"],
            "key_people": ["Alice"],
            "key_dates": ["2026-01-15"],
            "open_questions": [],
        },
    }
    entity_response = {
        "affected_sections": [{"id": "overview", "title": "Overview", "content_md": "entity body"}],
        "kind_schema": {
            "name": "Alice",
            "role": "engineer",
            "owns": ["auth-service"],
            "decides": [],
            "contributes": [],
            "associated_pages": [],
        },
    }

    async def _stub_llm(prompt: str) -> str:
        # Dispatch by which prompt was rendered — the per-kind system
        # blocks are distinguishable.
        if "Entity page synthesis" in prompt:
            return json.dumps(entity_response)
        return json.dumps(topic_response)

    saved_pages: list[WikiPage] = []

    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=None)

    async def _save(page: WikiPage) -> None:
        saved_pages.append(page)

    page_store.save_page = AsyncMock(side_effect=_save)
    maintainer = WikiMaintainer(page_store=page_store)
    _stub_facts(maintainer, ["f1"])
    maintainer._invoke_apply_update_llm = _stub_llm  # type: ignore[method-assign]

    await maintainer.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    await maintainer.apply_update(
        channel_id="C1",
        page_id="entity:alice",
        new_fact_ids=["f1"],
        target_lang="en",
    )

    assert len(saved_pages) == 2
    topic_page = next(p for p in saved_pages if p.kind == "topic")
    entity_page = next(p for p in saved_pages if p.kind == "entity")

    topic_schema = topic_page.kind_schema
    entity_schema = entity_page.kind_schema
    assert topic_schema is not None
    assert entity_schema is not None
    topic_keys = set(topic_schema)
    entity_keys = set(entity_schema)

    # Each kind populates fields the OTHER kind does NOT have. This is
    # the structural-difference assertion the spec calls for.
    topic_only = topic_keys - entity_keys
    entity_only = entity_keys - topic_keys
    assert "summary" in topic_only
    assert "key_decisions" in topic_only
    assert "name" in entity_only
    assert "owns" in entity_only
    # And the field-shape contract holds: topic.summary is a string,
    # entity.owns is a list.
    assert isinstance(topic_schema["summary"], str)
    assert isinstance(entity_schema["owns"], list)


# ---------------------------------------------------------------------------
# §3.7 — ``_validate_kind_schema`` and ``_load_kind_prompt`` direct tests
# ---------------------------------------------------------------------------


def test_validate_kind_schema_returns_none_for_valid_topic_payload() -> None:
    err = wm_mod._validate_kind_schema(
        "topic",
        {
            "summary": "ok",
            "key_decisions": [],
            "key_people": [],
            "key_dates": [],
            "open_questions": [],
        },
    )
    assert err is None


def test_validate_kind_schema_rejects_missing_required_field() -> None:
    err = wm_mod._validate_kind_schema("topic", {"key_decisions": []})
    assert err is not None
    assert "summary" in err  # error message names the missing required field


def test_validate_kind_schema_rejects_non_dict_payload() -> None:
    err = wm_mod._validate_kind_schema("topic", "not a dict")
    assert err is not None


def test_load_kind_prompt_contains_voice_seed_placeholder() -> None:
    # The topic prompt must reference voice_seed so the model receives
    # the channel's voice anchor.
    body = wm_mod._load_kind_prompt("topic")
    assert "voice_seed" in body.lower() or "voice seed" in body.lower()


def test_load_kind_prompt_raises_on_unknown_kind() -> None:
    with pytest.raises(KeyError):
        wm_mod._load_kind_prompt("not-a-real-kind")


def test_derive_kind_from_page_id_maps_structural_prefixes() -> None:
    assert wm_mod.derive_kind_from_page_id("topic:auth") == "topic"
    assert wm_mod.derive_kind_from_page_id("entity:alice") == "entity"
    assert wm_mod.derive_kind_from_page_id("decisions") == "decisions"
    assert wm_mod.derive_kind_from_page_id("faq") == "faq"
    assert wm_mod.derive_kind_from_page_id("action-items") == "action_items"
    # Unknown structure falls back to topic (the safe default — its
    # synthesis prompt is the broadest).
    assert wm_mod.derive_kind_from_page_id("custom:foo") == "topic"
    assert wm_mod.derive_kind_from_page_id("") == "topic"


def test_resolve_dispatch_kind_prefers_explicit_kind_over_derivation() -> None:
    page = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="topic:auth",
        title="Auth",
        kind="entity",  # operator-set after a split/merge
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
    )
    assert wm_mod._resolve_dispatch_kind(page) == "entity"


def test_resolve_dispatch_kind_falls_back_to_derivation_on_default_kind() -> None:
    # Legacy entity page that predates the redesign — kind defaults to "topic"
    # but page_id structure says it's an entity.
    page = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="entity:alice",
        title="Alice",
        # ``kind`` left at the model default ("topic")
        sections=[WikiPageSection(id="overview", title="Overview", content_md="x")],
    )
    assert wm_mod._resolve_dispatch_kind(page) == "entity"
