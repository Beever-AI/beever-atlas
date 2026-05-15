# Design Decisions, Open Questions & Research

> **Purpose**: Architectural rationale, open questions, and research paper integration for Beever Atlas v2.
> Sourced from the v1 monolith (§7, §10, §11, Sources). Complements the individual v2 docs — read alongside `01-architecture-overview.md`.

---

## Key Design Decisions

| Decision | Choice | Rationale | Rejected Alternative |
|----------|--------|-----------|---------------------|
| Memory architecture | Dual (Weaviate + Neo4j) | Each does what it's best at — semantic vs. relational | Neo4j only (can't do hybrid BM25+vector), Weaviate only (can't do multi-hop graph) |
| Weaviate tiers | Keep 3 tiers, fix bugs | Sound design; Tier 0+1 give free reads (wiki-first); just needs working cluster linking | Remove tiers (loses free wiki reads, loses topic scoping) |
| Graph schema | Guided-flexible | Core types + LLM creates extensions; captures any relationship | Fixed schema (misses Budget, Team, Meeting...), Full triplets (too noisy) |
| Relationships | Fully flexible | LLM extracts whatever verb phrase captures the meaning | Fixed relationship list (can't capture BLOCKED_BY, POSTPONED_UNTIL...) |
| Query routing | Hybrid (route OR parallel) | Semantic-first saves cost (80%); parallel for ambiguous | Pure router (misclassification), Pure parallel (wasteful) |
| Multi-platform | Python adapters | Chat SDK is TS-only, can't fetch history | Chat SDK only (no batch history) |
| Quality gate | Reject at extraction | Prevent garbage from entering system | Post-hoc cleanup (harder) |
| Cluster linking | Actually write cluster_id | v1's biggest bug — no-op | Keep as no-op (breaks everything) |
| Agent framework | [Google ADK](https://google.github.io/adk-docs/) | Native agent orchestration (Sequential, Parallel, Loop), LiteLLM fallback, session state, FunctionTool wrapping of store operations. See [`13-adk-integration.md`](13-adk-integration.md) | Direct LLM calls (no orchestration, manual retry logic), LangChain (heavier abstraction, more dependencies) |
| Chat bot | [Vercel Chat SDK](https://chat-sdk.dev/) | Multi-platform real-time chat (Slack, Teams, Discord) with adapter pattern, action buttons, Redis state. See [`13-adk-integration.md`](13-adk-integration.md) | Custom webhook handlers per platform (more code), Slack Bolt only (single platform) |

See `04-query-router.md` for routing strategy detail. See `02-semantic-memory.md` for tier and cluster linking implementation. See `05-ingestion-pipeline.md` for quality gate implementation.

---

## Open Questions

1. **Entity extraction cost**: ~$0.001/message for flash-lite. 10K messages = ~$10 initial sync. Acceptable?
2. **Graph type normalization**: How aggressively should we merge "Team"/"Group"/"Squad" into one type? LLM pass or rule-based?
3. ~~**Consolidation frequency**~~: **RESOLVED** — Three triggers: after sync (incremental), daily 2 AM UTC (full), on-demand API. See `06-wiki-generation.md`.
4. ~~**MCP surface**~~: **RESOLVED** — Graph queries abstracted behind `ask_questions`. 7 tools defined. See `07-deployment.md`.
5. **Chat SDK bridge**: Worth building the TypeScript webhook service for real-time ingestion in Phase 2?
6. **Decomposition threshold**: When should queries be decomposed vs. sent as-is? Token length? LLM confidence?

---

## Research Paper Integration

| Paper | Core Insight | How v2 Uses It |
|-------|-------------|----------------|
| **GraphRAG (Weaviate+Neo4j)** | Hybrid vector-graph search | Dual memory: Weaviate for semantic, Neo4j for relational |
| **H-MEM** | 4-layer hierarchical memory | 3-tier Weaviate (summary→topic→atomic) with fixes |
| **System-1/System-2 Routing** | Dual-process retrieval | Smart router: semantic (fast) / graph (deep) / both |
| **Ebbinghaus Forgetting** | R = e^(-t/S) | Applied to retrieval ranking (actually wired in v2) |
| **MemoryBank** | Nightly distillation | Scheduled consolidation: clusters + summaries + wiki |
| **Dynamic Knowledge Graphs** | Episodic edges + fact replacement | Event nodes linking Neo4j↔Weaviate; SUPERSEDES edges |
| **Zep** | Bi-temporal tracking | valid_from/valid_until/created_at on all relationships |
| **Mem0/Mem0g** | LLM judge for consolidation | Entity extraction dedup: MERGE vs ADD vs SUPERSEDE |

Full paper summaries, diagrams, and application notes are in [`reference-papers.md`](reference-papers.md).

---

## Sources

- [Vercel Chat SDK](https://chat-sdk.dev/) — [GitHub (vercel/chat)](https://github.com/vercel/chat)
- [Chat SDK Adapters](https://chat-sdk.dev/docs/adapters) — [Changelog](https://vercel.com/changelog/chat-sdk)
- [GraphRAG via Weaviate & Neo4j](https://weaviate.io/blog/graph-rag)
- [H-MEM: Hierarchical Memory](https://arxiv.org/pdf/2507.22925)
- [System-1/System-2 Graph Retrieval](https://arxiv.org/pdf/2602.15313)
- [Zep Bi-Temporal Model](https://arxiv.org/pdf/2501.13956)
- [Mem0/Mem0g](https://arxiv.org/pdf/2504.19413)
- [Dynamic Knowledge Graphs](https://www.ijcai.org/proceedings/2025/0002.pdf)
