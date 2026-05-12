"""Tests that EVERY page compiler runs ``_rewrite_topic_wikilinks`` as a
final post-process pass so an LLM that emits ``[[Page Title]]`` references
never leaks a red broken link.

This file is the umbrella check across all page kinds the compiler emits:

  * Overview
  * Decisions
  * FAQ
  * Activity
  * Topic (legacy flat path)
  * Sub-topic
  * Thin topic
  * Folder index

Parameterised wherever the LLM-call shape is uniform; per-kind tests
otherwise. Each compiler is exercised via a fake LLM whose JSON response
contains both a compiled-topic wikilink and a non-compiled-topic
wikilink; the assertions check that:

  1. Wikilinks to compiled topics produce ``/wiki/<slug>`` markdown links.
  2. Wikilinks to non-compiled topics become plain text (no ``[[...]]``).
  3. The rewrite is idempotent.

The compiler instance is constructed via ``__new__`` to bypass the
heavy network-bound ``__init__`` — only the methods we directly call
need state, and we patch the LLM transport (``_call_llm``,
``_llm_generate_json``) on the bare instance.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from beever_atlas.models.domain import AtomicFact
from beever_atlas.wiki.compiler import (
    CompiledPageContent,
    WikiCompiler,
    _rewrite_topic_wikilinks,
    _topic_slug_for_title,
)


def _make_fact(memory_text: str = "fact") -> AtomicFact:
    """Build a minimal AtomicFact for compiler input shape."""
    return AtomicFact(
        memory_text=memory_text,
        author_name="alice",
        message_ts="2026-01-01T00:00:00Z",
        quality_score=0.5,
        fact_type="discussion",
        importance="high",
    )


# ── shared fixtures ─────────────────────────────────────────────────────────


COMPILED = "Memory Hierarchy Design"
SKIPPED = "Some Skipped Topic"


def _compiler() -> WikiCompiler:
    """Build a minimal WikiCompiler instance for direct method calls.

    ``WikiCompiler.__init__`` reaches out to providers + Neo4j; we bypass
    it via ``__new__`` and seed only the attributes our test paths read.
    """
    c = WikiCompiler.__new__(WikiCompiler)
    c._target_lang = "en"
    c._source_lang = "en"
    return c


def _llm_payload(content: str, summary: str = "stub summary") -> str:
    return json.dumps({"content": content, "summary": summary})


def _patch_call_llm(compiler: WikiCompiler, content: str, summary: str = "s") -> None:
    """Patch ``_call_llm`` to return the given content/summary."""

    async def _fake(*args: Any, **kwargs: Any) -> CompiledPageContent:
        return CompiledPageContent(content=content, summary=summary)

    compiler._call_llm = _fake  # type: ignore[attr-defined]


def _patch_llm_generate_json(compiler: WikiCompiler, content: str, summary: str = "s") -> None:
    """Patch ``_llm_generate_json`` to return a JSON payload."""

    async def _fake(*args: Any, **kwargs: Any) -> str:
        return _llm_payload(content, summary)

    compiler._llm_generate_json = _fake  # type: ignore[attr-defined]


def _make_wikilink_body() -> str:
    """Markdown body with one compiled and one skipped wikilink + a header.

    The header satisfies ``required_headings(("Overview",))`` validators
    on topic/subtopic paths.
    """
    return (
        f"**TL;DR sentence.**\n\n"
        f"## Overview\n\n"
        f"See [[{COMPILED}]] for context, and also [[{SKIPPED}]] (legacy).\n\n"
        f"## Key Facts\n\n"
        f"| Fact | Source |\n|---|---|\n| Something true | [[{COMPILED}]] |\n"
    )


def _assert_wikilink_rewrite_invariants(out: str) -> None:
    """All three rewrite invariants in one place."""
    slug = _topic_slug_for_title(COMPILED)
    # 1. Compiled topic → real markdown link.
    assert f"({COMPILED}](/wiki/{slug})" not in out  # double-link guard
    assert f"[{COMPILED}](/wiki/{slug})" in out, (
        f"Expected compiled wikilink resolved to /wiki/{slug} in:\n{out}"
    )
    # 2. Skipped topic → plain text (no [[...]] left).
    assert f"[[{SKIPPED}]]" not in out, f"Skipped wikilink leaked in:\n{out}"
    assert SKIPPED in out, "Skipped topic name should remain as plain text"
    # 3. No raw [[ ]] wrappers for the compiled topic either.
    assert f"[[{COMPILED}]]" not in out


# ── helper: gathered scaffold ───────────────────────────────────────────────


def _make_gathered(*, with_compiled_set: bool = True) -> dict:
    """Build a minimal ``gathered`` dict the fixed-page compilers read.

    ``with_compiled_set=False`` exercises the cluster-title fallback path
    that fires when ``compile()`` has not stashed ``_compiled_topic_titles``.
    """

    class _Cluster:
        def __init__(self, title: str, cid: str) -> None:
            self.title = title
            self.id = cid
            self.member_count = 5
            self.faq_candidates = []
            self.summary = ""
            self.current_state = ""
            self.open_questions = []
            self.impact_note = ""
            self.topic_tags = []
            self.date_range_start = ""
            self.date_range_end = ""
            self.authors = []
            self.key_facts = []
            self.decisions = []
            self.people = []
            self.technologies = []
            self.projects = []
            self.key_entities = []
            self.key_relationships = []
            self.related_cluster_ids = []
            self.topic_graph_edges = []

    class _ChannelSummary:
        channel_name = "test-channel"
        description = "desc"
        text = ""
        themes = []
        momentum = ""
        team_dynamics = ""
        top_people = []
        top_decisions = []
        active_projects = []
        tech_stack = []
        media_count = 0
        glossary_terms = []
        topic_graph_edges = []
        recent_activity_summary = {}

    clusters = [_Cluster(COMPILED, "topic-compiled")]
    gathered: dict = {
        "channel_summary": _ChannelSummary(),
        "clusters": clusters,
        "cluster_facts": {"topic-compiled": []},
        "media_facts": [],
        "recent_facts": [],
        "decisions": [],
        "persons": [],
        "technologies": [],
        "projects": [],
        "total_facts": 0,
    }
    if with_compiled_set:
        gathered["_compiled_topic_titles"] = [COMPILED]
    return gathered


# ── 1. Helper itself — sanity ───────────────────────────────────────────────


def test_helper_rewrites_compiled_and_strips_skipped() -> None:
    out = _rewrite_topic_wikilinks(_make_wikilink_body(), [COMPILED])
    _assert_wikilink_rewrite_invariants(out)


def test_helper_idempotent() -> None:
    once = _rewrite_topic_wikilinks(_make_wikilink_body(), [COMPILED])
    twice = _rewrite_topic_wikilinks(once, [COMPILED])
    assert once == twice


# ── 2. Parameterised compiler-call check ────────────────────────────────────
#
# Each entry: (compiler_method_name, gathered_factory). The factory lets a
# test scope its own gathered fixture (e.g. Decisions wants ≥1 decision).


def _gathered_for_decisions() -> dict:
    g = _make_gathered()
    g["decisions"] = [{"name": "decision A", "decided_by": "alice", "date": "2026-01-01"}]
    return g


def _gathered_for_faq() -> dict:
    g = _make_gathered()
    # FAQ reads cluster.faq_candidates — keep it empty so the deterministic
    # fallback doesn't override the LLM output; the LLM body is what we
    # need to rewrite.
    g["clusters"][0].faq_candidates = ["What is X?"]
    return g


def _gathered_for_activity() -> dict:
    g = _make_gathered()
    g["recent_facts"] = [_make_fact()]
    return g


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "gathered_factory"),
    [
        ("_compile_overview", _make_gathered),
        ("_compile_decisions", _gathered_for_decisions),
        ("_compile_faq", _gathered_for_faq),
        ("_compile_activity", _gathered_for_activity),
    ],
)
async def test_fixed_page_compilers_rewrite_wikilinks(method: str, gathered_factory) -> None:
    """Each fixed-page compiler must funnel LLM output through the wikilink
    rewrite so compiled topics resolve and skipped topics become plain text."""
    compiler = _compiler()
    body = _make_wikilink_body()
    _patch_call_llm(compiler, body)

    gathered = gathered_factory()
    page = await getattr(compiler, method)(gathered)
    _assert_wikilink_rewrite_invariants(page.content)


# ── 3. Topic flat-page path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compile_thin_topic_rewrites_wikilinks() -> None:
    """``_compile_thin_topic`` is the catastrophic-modular fallback path for
    small clusters; defensive rewrite still must run."""
    compiler = _compiler()
    body = _make_wikilink_body()
    _patch_call_llm(compiler, body)

    gathered = _make_gathered()
    cluster = gathered["clusters"][0]
    page = await compiler._compile_thin_topic(cluster, gathered)
    _assert_wikilink_rewrite_invariants(page.content)


@pytest.mark.asyncio
async def test_compile_topic_page_legacy_flat_path_rewrites_wikilinks() -> None:
    """When the modular path catastrophically fails, the legacy flat
    ``TOPIC_PROMPT`` path takes over — the rewrite must still run."""
    compiler = _compiler()
    body = _make_wikilink_body()

    # Force the modular path to fall back by making it raise — the legacy
    # ``TOPIC_PROMPT`` path takes over and that's what we want to test.
    async def _raise_modular(*args: Any, **kwargs: Any):
        raise RuntimeError("force legacy path")

    compiler._try_compile_topic_modular = _raise_modular  # type: ignore[attr-defined]
    _patch_call_llm(compiler, body)

    # Need at least one fact so the legacy "flat" path is reached;
    # otherwise compile_topic_page exits early with the no-facts branch.
    gathered = _make_gathered()
    gathered["cluster_facts"] = {"topic-compiled": [_make_fact()]}
    cluster = gathered["clusters"][0]

    result = await compiler._compile_topic_page(cluster, gathered)
    page = result[0] if isinstance(result, list) else result
    _assert_wikilink_rewrite_invariants(page.content)


# ── 4. Sub-topic ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compile_subtopic_page_rewrites_wikilinks() -> None:
    """``[[Parent Title]]`` anchor + any ``[[Title]]`` inline references."""
    compiler = _compiler()
    body = _make_wikilink_body()
    _patch_call_llm(compiler, body)

    page = await compiler._compile_subtopic_page(
        parent_slug="memory-hierarchy-design",
        parent_title=COMPILED,
        sub_info={"title": "Sub Slice", "summary": "scoped", "fact_indices": [0]},
        all_sorted_facts=[_make_fact()],
        compiled_topic_titles=[COMPILED],
    )
    _assert_wikilink_rewrite_invariants(page.content)


# ── 5. Folder index ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compile_folder_page_legacy_path_rewrites_wikilinks() -> None:
    """Force the modular folder path to fall back and verify the legacy
    ``FOLDER_INDEX_PROMPT`` path also routes through the rewrite."""
    from beever_atlas.wiki.compiler import WikiPage

    compiler = _compiler()
    body = _make_wikilink_body()
    _patch_call_llm(compiler, body)

    # Patch the modular folder import to return ``None`` so the compiler
    # falls back to the legacy FOLDER_INDEX_PROMPT path. ``compile_folder_page_modular``
    # is imported INSIDE the method, so monkey-patching the orchestrator
    # module attribute hits the same callable.
    import beever_atlas.wiki.modules.orchestrator as orch

    async def _fail_modular(*args: Any, **kwargs: Any):
        raise RuntimeError("force legacy folder path")

    orch.compile_folder_page_modular = _fail_modular  # type: ignore[attr-defined]

    child = WikiPage(
        id="topic-compiled",
        slug="memory-hierarchy-design",
        title=COMPILED,
        page_type="topic",
        content="child body",
        summary="child summary",
        memory_count=5,
    )
    page = await compiler._compile_folder_page(
        folder_slug="memory",
        folder_title="Memory",
        children_pages=[child],
        compiled_topic_titles=[COMPILED],
    )
    _assert_wikilink_rewrite_invariants(page.content)


# ── 6. Cluster-title fallback (when _compiled_topic_titles is missing) ─────


@pytest.mark.asyncio
async def test_overview_fallbacks_to_cluster_titles_when_compiled_set_missing() -> None:
    """When ``compile()`` has not stashed ``_compiled_topic_titles``,
    every cluster title is treated as a valid wikilink target — i.e. the
    fallback uses ``[c.title for c in clusters]`` so legacy/test callers
    aren't broken."""
    compiler = _compiler()
    body = f"**TL;DR.**\n\n## Overview\n\nSee [[{COMPILED}]] — should resolve via fallback.\n"
    _patch_call_llm(compiler, body)
    gathered = _make_gathered(with_compiled_set=False)
    page = await compiler._compile_overview(gathered)
    slug = _topic_slug_for_title(COMPILED)
    assert f"[{COMPILED}](/wiki/{slug})" in page.content
    assert f"[[{COMPILED}]]" not in page.content
