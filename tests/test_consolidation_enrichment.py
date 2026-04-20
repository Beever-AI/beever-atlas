"""Tests for consolidation enrichment features (wiki-ready memory enrichment).

Covers:
- Schema validation (Group 1)
- Graph enrichment methods (Group 2)
- Key facts selection (Group 3)
- Cross-cluster links with shared entities (Group 4)
- Recent activity summary (Group 5)
- Channel-level aggregation (Group 6)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.agents.schemas.consolidation import (
    FaqCandidate,
    GlossaryTerm,
)
from beever_atlas.models.domain import (
    AtomicFact,
    GraphEntity,
    GraphRelationship,
    TopicCluster,
)


# ── Group 1: Schema Validation ──────────────────────────────────


class TestSchemaValidation:
    """Tests for TopicSummaryResult and ChannelSummaryResult schemas."""

    def test_topic_summary_result_valid_input(self):
        from beever_atlas.agents.schemas.consolidation import TopicSummaryResult

        result = TopicSummaryResult(
            title="JWT Migration to RS256",
            summary_text="The team migrated JWT signing from HS256 to RS256.",
            current_state="Migration is complete and deployed to production.",
            open_questions="Whether to adopt OAuth 2.1 fully remains undecided.",
            impact_note="Affects 3 downstream services and all mobile clients.",
            topic_tags=["authentication", "security", "migration"],
            faq_candidates=[
                FaqCandidate(question="Why RS256?", answer="Better key rotation support.")
            ],
        )
        assert result.title == "JWT Migration to RS256"
        assert len(result.topic_tags) == 3
        assert len(result.faq_candidates) == 1

    def test_topic_summary_result_defaults(self):
        from beever_atlas.agents.schemas.consolidation import TopicSummaryResult

        result = TopicSummaryResult()
        assert result.title == ""
        assert result.summary_text == ""
        assert result.current_state == ""
        assert result.open_questions == ""
        assert result.impact_note == ""
        assert result.topic_tags == []
        assert result.faq_candidates == []

    def test_channel_summary_result_valid_input(self):
        from beever_atlas.agents.schemas.consolidation import ChannelSummaryResult

        result = ChannelSummaryResult(
            summary_text="The backend team focuses on auth and infra.",
            description="Backend architecture decisions and deployment workflows.",
            themes="Auth and API design are closely linked.",
            momentum="3 topics active, 2 completed in last 7 days.",
            team_dynamics="Alice leads arch decisions, Bob implements.",
            glossary_terms=[
                GlossaryTerm(
                    term="RS256",
                    definition="RSA Signature with SHA-256.",
                    first_mentioned_by="Alice",
                    related_topics=["Authentication"],
                )
            ],
        )
        assert result.description == "Backend architecture decisions and deployment workflows."
        assert len(result.glossary_terms) == 1

    def test_channel_summary_result_defaults(self):
        from beever_atlas.agents.schemas.consolidation import ChannelSummaryResult

        result = ChannelSummaryResult()
        assert result.summary_text == ""
        assert result.description == ""
        assert result.themes == ""
        assert result.momentum == ""
        assert result.team_dynamics == ""
        assert result.glossary_terms == []

    def test_faq_candidate_schema(self):
        from beever_atlas.agents.schemas.consolidation import FaqCandidate

        faq = FaqCandidate(question="Why Redis?", answer="Pub/sub support.")
        assert faq.question == "Why Redis?"
        assert faq.answer == "Pub/sub support."

    def test_glossary_term_schema(self):
        from beever_atlas.agents.schemas.consolidation import GlossaryTerm

        term = GlossaryTerm(
            term="CQRS",
            definition="Command Query Responsibility Segregation.",
            first_mentioned_by="Bob",
            related_topics=["Architecture", "Database"],
        )
        assert term.term == "CQRS"
        assert len(term.related_topics) == 2


# ── Group 2: Graph Enrichment Methods ──────────────────────────


def _make_consolidation_service(graph=None):
    """Create a ConsolidationService with mocked dependencies."""
    from beever_atlas.services.consolidation import ConsolidationService

    settings = MagicMock()
    settings.cluster_similarity_threshold = 0.7
    settings.cluster_merge_threshold = 0.95
    settings.cluster_max_size = 1000
    settings.consolidation_max_concurrent_llm = 3
    weaviate = AsyncMock()
    return ConsolidationService(weaviate=weaviate, settings=settings, graph=graph)


class TestEnrichDecisions:
    """Tests for _enrich_decisions method."""

    @pytest.mark.asyncio
    async def test_returns_empty_without_graph(self):
        svc = _make_consolidation_service(graph=None)
        result = await svc._enrich_decisions(["Redis"], "ch1")
        assert result == []

    @pytest.mark.asyncio
    async def test_resolves_supersede_chains(self):
        graph = AsyncMock()
        graph.get_decisions.return_value = [
            GraphEntity(name="Use RS256", type="Decision", properties={"decided_by": "Alice"}),
            GraphEntity(name="Use HS256", type="Decision", properties={"decided_by": "Alice"}),
        ]
        graph.list_relationships.return_value = [
            GraphRelationship(
                type="SUPERSEDES", source="Use RS256", target="Use HS256", confidence=1.0
            ),
        ]
        svc = _make_consolidation_service(graph=graph)
        result = await svc._enrich_decisions(["use rs256", "use hs256"], "ch1")

        active = [d for d in result if d["status"] == "active"]
        superseded = [d for d in result if d["status"] == "superseded"]
        assert len(active) >= 1
        assert len(superseded) >= 1
        assert superseded[0]["superseded_by"] == "Use RS256"


class TestEnrichPeople:
    """Tests for _enrich_people method."""

    @pytest.mark.asyncio
    async def test_fallback_to_authors_without_graph(self):
        svc = _make_consolidation_service(graph=None)
        result = await svc._enrich_people(["Redis"], ["Alice", "Bob"], "ch1")
        assert len(result) == 2
        assert all(p["role"] == "mentioned" for p in result)

    @pytest.mark.asyncio
    async def test_decision_maker_role_assigned(self):
        graph = AsyncMock()
        graph.list_entities.return_value = [
            GraphEntity(name="Alice", type="Person"),
        ]
        graph.list_relationships.return_value = [
            GraphRelationship(type="DECIDED", source="Alice", target="Use Redis", confidence=1.0),
        ]
        svc = _make_consolidation_service(graph=graph)
        result = await svc._enrich_people(["use redis"], ["Alice"], "ch1")
        alice = next((p for p in result if p["name"] == "Alice"), None)
        assert alice is not None
        assert alice["role"] == "decision_maker"


class TestEnrichTechnologies:
    """Tests for _enrich_technologies method."""

    @pytest.mark.asyncio
    async def test_returns_empty_without_graph(self):
        svc = _make_consolidation_service(graph=None)
        result = await svc._enrich_technologies(["Redis"], "ch1")
        assert result == []

    @pytest.mark.asyncio
    async def test_extracts_tech_with_champion(self):
        graph = AsyncMock()
        graph.list_entities.return_value = [
            GraphEntity(name="Redis", type="Technology", properties={"category": "database"}),
        ]
        graph.list_relationships.return_value = [
            GraphRelationship(type="USES", source="Alice", target="Redis", confidence=0.9),
        ]
        svc = _make_consolidation_service(graph=graph)
        result = await svc._enrich_technologies(["redis"], "ch1")
        assert len(result) == 1
        assert result[0]["name"] == "Redis"
        assert result[0]["champion"] == "Alice"


class TestEnrichProjects:
    """Tests for _enrich_projects method."""

    @pytest.mark.asyncio
    async def test_returns_empty_without_graph(self):
        svc = _make_consolidation_service(graph=None)
        result = await svc._enrich_projects(["Atlas"], "ch1")
        assert result == []

    @pytest.mark.asyncio
    async def test_resolves_blockers(self):
        graph = AsyncMock()
        graph.list_entities.return_value = [
            GraphEntity(name="Rate Limiting", type="Project", properties={"status": "blocked"}),
        ]
        graph.list_relationships.return_value = [
            GraphRelationship(
                type="BLOCKED_BY", source="Rate Limiting", target="Redis Upgrade", confidence=0.9
            ),
            GraphRelationship(
                type="WORKS_ON", source="Bob", target="Rate Limiting", confidence=0.8
            ),
        ]
        svc = _make_consolidation_service(graph=graph)
        result = await svc._enrich_projects(["rate limiting"], "ch1")
        assert len(result) == 1
        assert result[0]["owner"] == "Bob"
        assert "Redis Upgrade" in result[0]["blockers"]


# ── Group 3: Key Facts Selection ──────────────────────────────


class TestKeyFactsSelection:
    """Tests for key_facts selection logic (top 5, superseded excluded)."""

    def test_top_5_by_quality_score(self):
        facts = [
            AtomicFact(id=f"f{i}", memory_text=f"Fact {i}", quality_score=i * 0.1)
            for i in range(10)
        ]
        active = [f for f in facts if f.superseded_by is None]
        active.sort(key=lambda f: f.quality_score, reverse=True)
        key_facts = [
            {"fact_id": f.id, "memory_text": f.memory_text, "quality_score": f.quality_score}
            for f in active[:5]
        ]
        assert len(key_facts) == 5
        assert key_facts[0]["quality_score"] == 0.9
        assert key_facts[4]["quality_score"] == 0.5

    def test_superseded_excluded(self):
        facts = [
            AtomicFact(id="f1", memory_text="Active fact", quality_score=0.9),
            AtomicFact(
                id="f2", memory_text="Superseded fact", quality_score=0.95, superseded_by="f3"
            ),
            AtomicFact(id="f3", memory_text="Newer fact", quality_score=0.8),
        ]
        active = [f for f in facts if f.superseded_by is None]
        active.sort(key=lambda f: f.quality_score, reverse=True)
        key_facts = [f.id for f in active[:5]]
        assert "f2" not in key_facts
        assert "f1" in key_facts
        assert "f3" in key_facts

    def test_fewer_than_5_facts(self):
        facts = [
            AtomicFact(id="f1", memory_text="Fact 1", quality_score=0.9),
            AtomicFact(id="f2", memory_text="Fact 2", quality_score=0.8),
        ]
        active = [f for f in facts if f.superseded_by is None]
        active.sort(key=lambda f: f.quality_score, reverse=True)
        key_facts = active[:5]
        assert len(key_facts) == 2


# ── Group 4: Cross-Cluster Links with Shared Entities ──────────


class TestCrossClusterSharedEntities:
    """Tests for _compute_cross_cluster_shared_entities."""

    def test_shared_entities_returned(self):
        from beever_atlas.services.consolidation import ConsolidationService

        c1 = TopicCluster(id="c1", channel_id="ch1", title="Auth")
        c2 = TopicCluster(id="c2", channel_id="ch1", title="API Design")
        members = {
            "c1": [
                AtomicFact(id="f1", memory_text="f1", entity_tags=["Redis", "Alice", "JWT"]),
                AtomicFact(id="f2", memory_text="f2", entity_tags=["Alice", "OAuth"]),
            ],
            "c2": [
                AtomicFact(id="f3", memory_text="f3", entity_tags=["Redis", "Alice", "GraphQL"]),
            ],
        }
        edges = ConsolidationService._compute_cross_cluster_shared_entities([c1, c2], members)
        assert len(edges) == 1
        assert set(edges[0]["shared_entities"]) == {"alice", "redis"}
        assert edges[0]["source_title"] == "Auth"
        assert edges[0]["target_title"] == "API Design"

    def test_no_shared_entities(self):
        from beever_atlas.services.consolidation import ConsolidationService

        c1 = TopicCluster(id="c1", channel_id="ch1", title="Auth")
        c2 = TopicCluster(id="c2", channel_id="ch1", title="Infra")
        members = {
            "c1": [AtomicFact(id="f1", memory_text="f1", entity_tags=["JWT"])],
            "c2": [AtomicFact(id="f2", memory_text="f2", entity_tags=["Terraform"])],
        }
        edges = ConsolidationService._compute_cross_cluster_shared_entities([c1, c2], members)
        assert len(edges) == 0


# ── Group 5: Recent Activity Summary ──────────────────────────


class TestRecentActivitySummary:
    """Tests for _compute_recent_activity."""

    @pytest.mark.asyncio
    async def test_computes_7_day_counts(self):
        from beever_atlas.models.api import PaginatedFacts

        svc = _make_consolidation_service()
        now = datetime.now(tz=UTC)
        recent_ts = str(now.timestamp() - 3600)  # 1 hour ago

        svc._weaviate.list_facts.return_value = PaginatedFacts(
            memories=[
                AtomicFact(
                    id="f1",
                    memory_text="Decision fact",
                    fact_type="decision",
                    importance="high",
                    quality_score=0.9,
                    message_ts=recent_ts,
                ),
                AtomicFact(
                    id="f2",
                    memory_text="Observation",
                    fact_type="observation",
                    importance="medium",
                    quality_score=0.6,
                    message_ts=recent_ts,
                ),
            ],
            total=2,
        )

        clusters = [
            TopicCluster(id="c1", channel_id="ch1", title="Auth", created_at=now, updated_at=now),
        ]
        result = await svc._compute_recent_activity("ch1", clusters)
        assert result["facts_added_7d"] == 2
        assert result["decisions_added_7d"] == 1
        assert len(result["highlights"]) == 1  # only high-importance
        assert "Auth" in result["new_topics"]

    @pytest.mark.asyncio
    async def test_empty_when_no_recent_facts(self):
        from beever_atlas.models.api import PaginatedFacts

        svc = _make_consolidation_service()
        svc._weaviate.list_facts.return_value = PaginatedFacts(memories=[], total=0)

        result = await svc._compute_recent_activity("ch1", [])
        assert result["facts_added_7d"] == 0
        assert result["decisions_added_7d"] == 0
        assert result["highlights"] == []


# ── Group 6: Channel-Level Aggregation ──────────────────────────


class TestChannelAggregation:
    """Tests for channel-level people/tech/project aggregation."""

    def test_top_people_highest_role_wins(self):
        """When a person appears in multiple clusters, highest role wins."""
        role_priority = {"decision_maker": 4, "expert": 3, "contributor": 2, "mentioned": 1}

        clusters = [
            TopicCluster(
                id="c1",
                channel_id="ch1",
                title="Auth",
                people=[{"name": "Alice", "role": "contributor", "entity_id": ""}],
            ),
            TopicCluster(
                id="c2",
                channel_id="ch1",
                title="API",
                people=[{"name": "Alice", "role": "decision_maker", "entity_id": ""}],
            ),
        ]

        people_map: dict[str, dict] = {}
        for c in clusters:
            for p in c.people:
                name = p["name"]
                if name not in people_map:
                    people_map[name] = {
                        "name": name,
                        "role": p["role"],
                        "topic_count": 1,
                        "expertise_topics": [c.title],
                    }
                else:
                    people_map[name]["topic_count"] += 1
                    people_map[name]["expertise_topics"].append(c.title)
                    if role_priority.get(p["role"], 0) > role_priority.get(
                        people_map[name]["role"], 0
                    ):
                        people_map[name]["role"] = p["role"]

        assert people_map["Alice"]["role"] == "decision_maker"
        assert people_map["Alice"]["topic_count"] == 2

    def test_tech_stack_deduplication(self):
        """Technologies appearing in multiple clusters get topic_count > 1."""
        clusters = [
            TopicCluster(
                id="c1",
                channel_id="ch1",
                technologies=[{"name": "Redis", "category": "database", "champion": "Alice"}],
            ),
            TopicCluster(
                id="c2",
                channel_id="ch1",
                technologies=[{"name": "Redis", "category": "database", "champion": "Bob"}],
            ),
        ]

        tech_map: dict[str, dict] = {}
        for c in clusters:
            for t in c.technologies:
                name = t["name"]
                if name not in tech_map:
                    tech_map[name] = {**t, "topic_count": 1}
                else:
                    tech_map[name]["topic_count"] += 1

        assert tech_map["Redis"]["topic_count"] == 2

    def test_active_projects_deduplication(self):
        """Projects deduplicated by name, last cluster wins."""
        clusters = [
            TopicCluster(
                id="c1",
                channel_id="ch1",
                projects=[
                    {"name": "Atlas", "status": "in_progress", "owner": "Alice", "blockers": []}
                ],
            ),
            TopicCluster(
                id="c2",
                channel_id="ch1",
                projects=[
                    {
                        "name": "Atlas",
                        "status": "active",
                        "owner": "Alice",
                        "blockers": ["Redis Upgrade"],
                    }
                ],
            ),
        ]

        project_map: dict[str, dict] = {}
        for c in clusters:
            for p in c.projects:
                project_map[p["name"]] = {**p, "topic_cluster_id": c.id}

        assert project_map["Atlas"]["status"] == "active"
        assert project_map["Atlas"]["topic_cluster_id"] == "c2"
        assert "Redis Upgrade" in project_map["Atlas"]["blockers"]
