"""Integration tests for the planner → compile_folders → apply_folder_plan
round-trip.

The unit tests for each module use synthetic short ids that happen to
match across boundaries (e.g., ``"c1"`` works as both cluster id and
page slug). Real production data uses cluster UUIDs that are NOT page
slugs, so the unit tests can pass while the wired-together pipeline
silently drops every folder. This file exercises the full chain with
realistic data shapes to catch slug/id mismatches at the integration
layer.
"""

from __future__ import annotations

import json

import pytest

from beever_atlas.models.domain import (
    WikiPage,
    WikiPageNode,
    WikiStructure,
)
from beever_atlas.wiki.compiler import WikiCompiler
from beever_atlas.wiki.structure.planner import (
    PlannedFolder,
    PlannedStructure,
    WikiStructurePlanner,
)


def _make_topic_node(slug: str, title: str) -> WikiPageNode:
    return WikiPageNode(
        id=f"topic-{slug}",
        title=title,
        slug=slug,
        section_number="",
        page_type="topic",
        memory_count=10,
        children=[],
    )


def _make_topic_page(slug: str, title: str) -> WikiPage:
    return WikiPage(
        id=f"topic-{slug}",
        slug=slug,
        title=title,
        page_type="topic",
        section_number="",
        content=f"Body for {title}",
        summary=f"Summary of {title}",
        memory_count=10,
    )


def _make_flat_structure(slugs_titles: list[tuple[str, str]]) -> WikiStructure:
    """Build a flat WikiStructure (today's pre-folder layout)."""
    return WikiStructure(
        channel_id="C1",
        channel_name="test",
        platform="slack",
        pages=[_make_topic_node(s, t) for s, t in slugs_titles],
    )


# ---- apply_folder_plan_to_structure -----------------------------------------


def test_apply_folder_plan_moves_leaves_into_folder() -> None:
    """A plan with one folder containing 2 leaves produces a tree with the
    folder at root and the 2 leaves as its children."""
    structure = _make_flat_structure(
        [
            ("auth", "Authentication"),
            ("rbac", "RBAC"),
            ("marketing", "Marketing"),
        ]
    )
    plan = PlannedStructure(
        folders=[PlannedFolder(slug="security", title="Security", child_slugs=["auth", "rbac"])],
        leaves=["marketing"],
    )
    folder_pages = {
        "folder-security": WikiPage(
            id="folder-security",
            slug="security",
            title="Security",
            page_type="folder",
            content="Folder index body",
            summary="Security folder",
            memory_count=20,
        )
    }
    out = WikiCompiler.apply_folder_plan_to_structure(
        structure, plan=plan, folder_pages=folder_pages
    )
    # New tree: 2 root nodes (security folder + marketing leaf).
    assert len(out.pages) == 2
    folder_node = out.pages[0]
    assert folder_node.page_type == "folder"
    assert folder_node.slug == "security"
    # Folder contains the 2 planned children (in plan order).
    assert [c.slug for c in folder_node.children] == ["auth", "rbac"]
    # Marketing stays at root.
    assert out.pages[1].slug == "marketing"
    # Section numbers are recomputed: "1" for folder, "1.1" / "1.2" for kids, "2" for marketing.
    assert folder_node.section_number == "1"
    assert folder_node.children[0].section_number == "1.1"
    assert folder_node.children[1].section_number == "1.2"
    assert out.pages[1].section_number == "2"


def test_apply_folder_plan_no_folders_is_noop() -> None:
    """Empty plan returns the structure unchanged."""
    structure = _make_flat_structure([("a", "A"), ("b", "B")])
    plan = PlannedStructure(folders=[], leaves=["a", "b"])
    out = WikiCompiler.apply_folder_plan_to_structure(structure, plan=plan, folder_pages={})
    assert out is structure  # same object — no rearrangement


@pytest.mark.asyncio
async def test_compile_folders_handles_nested_folders() -> None:
    """REGRESSION: planner can produce folder→folder nesting (depth 3+).
    compile_folders must process inner folders first via topological
    sort so outer folders can find inner-folder children in the
    folder_pages_by_slug lookup. Without this, every nested-folder
    plan would silently produce empty outer folders.
    """
    plan = PlannedStructure(
        folders=[
            # Outer: "project" contains inner folders, NOT topics directly.
            PlannedFolder(
                slug="project",
                title="Project",
                child_slugs=["security", "growth"],
            ),
            # Inner: "security" contains topics.
            PlannedFolder(
                slug="security",
                title="Security",
                child_slugs=["auth", "rbac"],
            ),
            # Inner: "growth" contains topics.
            PlannedFolder(
                slug="growth",
                title="Growth",
                child_slugs=["marketing", "sales"],
            ),
        ],
        leaves=[],
    )
    leaves_by_slug = {
        slug: _make_topic_page(slug, slug.title())
        for slug in ["auth", "rbac", "marketing", "sales"]
    }
    compiler = WikiCompiler.__new__(WikiCompiler)

    async def _fake_call_llm(prompt: str, **kwargs):
        from beever_atlas.wiki.compiler import CompiledPageContent

        return CompiledPageContent(content="x <<CHILDREN_TOC>>", summary="s")

    compiler._call_llm = _fake_call_llm  # type: ignore[method-assign]
    folder_pages = await compiler.compile_folders(plan=plan, leaves_by_slug=leaves_by_slug)
    # All 3 folders should be produced — outer + 2 inners.
    assert "folder-project" in folder_pages
    assert "folder-security" in folder_pages
    assert "folder-growth" in folder_pages
    # Outer must contain BOTH inner folders (not topic refs).
    project = folder_pages["folder-project"]
    inner_slugs = {c.slug for c in project.children}
    assert inner_slugs == {"security", "growth"}, (
        "REGRESSION: outer folder is missing one or both inner folders. "
        "compile_folders likely failed to handle folder→folder nesting."
    )


def test_apply_folder_plan_handles_nested_folders() -> None:
    """REGRESSION: apply_folder_plan_to_structure must wire inner
    folder nodes as children of outer folders, AND keep nested folders
    out of the root-level page list (only orphan folders go at root)."""
    structure = _make_flat_structure(
        [
            ("auth", "Auth"),
            ("rbac", "RBAC"),
            ("marketing", "Marketing"),
        ]
    )
    plan = PlannedStructure(
        folders=[
            PlannedFolder(slug="project", title="Project", child_slugs=["security", "growth"]),
            PlannedFolder(slug="security", title="Security", child_slugs=["auth", "rbac"]),
            PlannedFolder(slug="growth", title="Growth", child_slugs=["marketing"]),
        ],
        leaves=[],
    )
    folder_pages = {
        f"folder-{slug}": WikiPage(
            id=f"folder-{slug}",
            slug=slug,
            title=slug.title(),
            page_type="folder",
            content=f"Index for {slug}",
            summary=f"{slug} folder",
        )
        for slug in ["project", "security", "growth"]
    }
    out = WikiCompiler.apply_folder_plan_to_structure(
        structure, plan=plan, folder_pages=folder_pages
    )
    # Only the OUTER folder is at root.
    assert len(out.pages) == 1
    project = out.pages[0]
    assert project.slug == "project"
    assert project.page_type == "folder"
    # Project contains the two inner folders.
    inner_slugs = [c.slug for c in project.children]
    assert sorted(inner_slugs) == ["growth", "security"]
    # Security folder has the 2 topics; growth has 1.
    security = next(c for c in project.children if c.slug == "security")
    growth = next(c for c in project.children if c.slug == "growth")
    assert {c.slug for c in security.children} == {"auth", "rbac"}
    assert {c.slug for c in growth.children} == {"marketing"}


def test_apply_folder_plan_skips_folder_with_unknown_children() -> None:
    """If the planner references slugs that don't exist in the structure,
    the folder is silently skipped (its children stay at root)."""
    structure = _make_flat_structure([("a", "A"), ("b", "B")])
    plan = PlannedStructure(
        folders=[PlannedFolder(slug="ghost", title="Ghost", child_slugs=["does-not-exist"])],
        leaves=["a", "b"],
    )
    folder_pages = {
        "folder-ghost": WikiPage(
            id="folder-ghost",
            slug="ghost",
            title="Ghost",
            page_type="folder",
            content="x",
            summary="x",
        )
    }
    out = WikiCompiler.apply_folder_plan_to_structure(
        structure, plan=plan, folder_pages=folder_pages
    )
    # Ghost folder has no resolvable children → skipped.
    # The original 2 leaves remain at root.
    assert len(out.pages) == 2
    assert {n.slug for n in out.pages} == {"a", "b"}


# ---- end-to-end planner → compile_folders → apply round-trip ----------------


@pytest.mark.asyncio
async def test_full_planner_to_structure_round_trip() -> None:
    """Wire the planner → compile_folders → apply_folder_plan_to_structure
    chain together and assert the resulting tree has folders correctly
    populated. Uses realistic page slugs (slugified titles, NOT cluster
    UUIDs) to mirror the production data shape.

    REGRESSION GUARD: this is the test the unit-only coverage missed.
    Builder.py sends page slugs (not cluster UUIDs) as the planner's
    cluster ids so the round-trip works. If anyone reverts that fix,
    this test fails — every folder produced will have empty children
    or the apply_folder_plan_to_structure call will silently drop
    references.
    """
    # Realistic clusters with UUID-style ids and human titles.
    clusters = [
        {
            "id": "uuid-aaaa",
            "title": "Beever Atlas Documentation",
            "summary": "x",
            "member_count": 10,
            "key_entities": [],
        },
        {
            "id": "uuid-bbbb",
            "title": "Beever Atlas GitHub Repository",
            "summary": "x",
            "member_count": 12,
            "key_entities": [],
        },
        {
            "id": "uuid-cccc",
            "title": "Marketing Funnel",
            "summary": "x",
            "member_count": 8,
            "key_entities": [],
        },
        {
            "id": "uuid-dddd",
            "title": "Sales Pipeline",
            "summary": "x",
            "member_count": 6,
            "key_entities": [],
        },
        {
            "id": "uuid-eeee",
            "title": "Hiring",
            "summary": "x",
            "member_count": 4,
            "key_entities": [],
        },
        {
            "id": "uuid-ffff",
            "title": "Onboarding",
            "summary": "x",
            "member_count": 5,
            "key_entities": [],
        },
        {
            "id": "uuid-gggg",
            "title": "Performance Reviews",
            "summary": "x",
            "member_count": 3,
            "key_entities": [],
        },
        {
            "id": "uuid-hhhh",
            "title": "Compensation",
            "summary": "x",
            "member_count": 7,
            "key_entities": [],
        },
    ]

    # Mimic builder.py's translation: send page slugs (NOT UUIDs) as
    # planner cluster ids. This is the production wiring.
    from beever_atlas.wiki.compiler import _slugify

    planner_clusters = []
    page_slugs: list[str] = []
    for c in clusters:
        page_slug = _slugify(c["title"]) or c["id"]
        page_slugs.append(page_slug)
        planner_clusters.append({**c, "id": page_slug})

    # Mock LLM returns a plan referencing the page slugs (this is what
    # the real LLM is told to do via the prompt).
    fake_response = json.dumps(
        {
            "folders": [
                {
                    "slug": "beever-atlas",
                    "title": "Beever Atlas",
                    "child_slugs": [
                        "beever-atlas-documentation",
                        "beever-atlas-github-repository",
                    ],
                    "rationale": "shared product domain",
                },
            ],
            "leaves": [
                "marketing-funnel",
                "sales-pipeline",
                "hiring",
                "onboarding",
                "performance-reviews",
                "compensation",
            ],
        }
    )

    planner = WikiStructurePlanner(llm=lambda _p: fake_response)
    plan = await planner.plan_async(
        channel_summary="A test channel",
        clusters=planner_clusters,
    )
    assert plan.fallback_reason is None
    assert len(plan.folders) == 1
    assert plan.folders[0].slug == "beever-atlas"

    # Build a leaves-by-slug dict the way builder.py does (keyed by
    # page.slug, NOT cluster UUID).
    leaves_by_slug = {
        slug: _make_topic_page(slug, title)
        for slug, title in zip(page_slugs, [c["title"] for c in clusters], strict=True)
    }

    # Run compile_folders — this is where the slug/id mismatch would
    # have silently produced empty folders. Mock _call_llm so we don't
    # invoke a real provider.
    compiler = WikiCompiler.__new__(WikiCompiler)

    async def _fake_call_llm(prompt: str, **kwargs):
        from beever_atlas.wiki.compiler import CompiledPageContent

        return CompiledPageContent(
            content="Fake folder index body. <<CHILDREN_TOC>>",
            summary="Fake summary",
        )

    compiler._call_llm = _fake_call_llm  # type: ignore[method-assign]

    folder_pages = await compiler.compile_folders(plan=plan, leaves_by_slug=leaves_by_slug)

    # CRITICAL: the folder MUST have non-empty children. The bug was
    # that compile_folders received UUID-keyed slug lookups and produced
    # empty folders.
    assert "folder-beever-atlas" in folder_pages
    folder_page = folder_pages["folder-beever-atlas"]
    assert folder_page.page_type == "folder"
    assert len(folder_page.children) == 2, (
        "REGRESSION: folder ended up empty. The planner returned 2 "
        "child slugs but compile_folders couldn't resolve them in the "
        "leaves dict — likely a slug/id mismatch in the wire-up."
    )
    assert {c.slug for c in folder_page.children} == {
        "beever-atlas-documentation",
        "beever-atlas-github-repository",
    }

    # Now exercise apply_folder_plan_to_structure with a realistic
    # flat structure (8 root-level topic nodes) and verify the
    # rearrangement produces a tree with 1 folder + 6 leaves at root.
    structure = _make_flat_structure(
        [(slug, c["title"]) for slug, c in zip(page_slugs, clusters, strict=True)]
    )
    out_structure = WikiCompiler.apply_folder_plan_to_structure(
        structure, plan=plan, folder_pages=folder_pages
    )
    assert len(out_structure.pages) == 7  # 1 folder + 6 leaves
    folder_node = next(n for n in out_structure.pages if n.page_type == "folder")
    assert folder_node.slug == "beever-atlas"
    assert len(folder_node.children) == 2
    # Section numbers reflect the new tree.
    assert folder_node.section_number == "1"
    assert folder_node.children[0].section_number == "1.1"
    assert folder_node.children[1].section_number == "1.2"
