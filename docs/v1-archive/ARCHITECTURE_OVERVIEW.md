# Beever Atlas: Comprehensive Architecture Overview

> **For**: Development Team, Product Team, Stakeholders
> **Purpose**: Understand how Beever Atlas works and what makes it different from competitors

---

## TL;DR: What Makes Beever Atlas Different?

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     BEEVER ATLAS vs COMPETITORS AT A GLANCE                      │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  TRADITIONAL MEMORY SYSTEMS (memU, Mem0, Zep):                                  │
│  ┌──────────┐     ┌──────────┐     ┌──────────┐                                 │
│  │  Query   │ ──▶ │ Retrieve │ ──▶ │   LLM    │ ──▶  $0.05/query               │
│  └──────────┘     └──────────┘     └──────────┘                                 │
│       Every query hits LLM = HIGH COST, text-only, no free exploration          │
│                                                                                  │
│  ─────────────────────────────────────────────────────────────────────────────  │
│                                                                                  │
│  BEEVER ATLAS (Wiki-First + Multimodal):                                        │
│  ┌──────────┐     ┌──────────┐                                                  │
│  │  Query   │ ──▶ │   Wiki   │ ──▶  FREE (80% of queries)                      │
│  └──────────┘     └──────────┘                                                  │
│       │                                                                          │
│       │ (only if needed)                                                         │
│       ▼                                                                          │
│  ┌──────────┐     ┌──────────┐                                                  │
│  │ Retrieve │ ──▶ │   LLM    │ ──▶  $0.05 (20% of queries)                     │
│  └──────────┘     └──────────┘                                                  │
│       AVERAGE COST: $0.01/query (5x cheaper)                                    │
│       + True multimodal (text, image, video, PDF)                               │
│       + Cross-modal search ("find auth diagrams" returns images)                │
│       + Intelligent forgetting (memories decay like human brain)                │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Part 1: The Complete User Journey

The following diagram shows how a user interacts with Beever Atlas from start to finish, with the underlying technical components at each step.

```mermaid
flowchart TB
    subgraph UserJourney["👤 USER JOURNEY"]
        direction LR
        U1["1. Connect<br/>Sources"] --> U2["2. Auto-Sync<br/>& Process"]
        U2 --> U3["3. Browse<br/>Wiki (FREE)"]
        U3 --> U4["4. Search<br/>& Ask"]
        U4 --> U5["5. Export<br/>& Integrate"]
    end

    subgraph Sources["📥 STEP 1: CONNECT SOURCES"]
        direction TB
        S1["Slack"]
        S2["Notion"]
        S3["GitHub"]
        S4["Local Files"]
        S5["Web/URLs"]
        S6["Calendar<br/>(Meetings)"]
    end

    subgraph Pipeline["⚙️ STEP 2: PROCESSING PIPELINE"]
        direction TB
        P1["INGEST<br/>Fetch raw content"]
        P2["PREPROCESS<br/>Modality detection<br/>Image/PDF/Video parsing"]
        P3["EXTRACT<br/>Facts + Narrative<br/>(LLM: Gemini Flash)"]
        P4["CLASSIFY<br/>Domain/Entity/Action tags<br/>Knowledge type"]
        P5["EMBED<br/>Jina v4 (2048-dim)<br/>Unified multimodal space"]
        P6["CLUSTER<br/>Auto-topic grouping<br/>Label propagation"]
        P7["PERSIST<br/>Weaviate + MongoDB"]

        P1 --> P2 --> P3 --> P4 --> P5 --> P6 --> P7
    end

    subgraph Storage["💾 STEP 3: HIERARCHICAL STORAGE"]
        direction TB
        T0["TIER 0: Collection Summary<br/>• Overall overview<br/>• Key themes & decisions<br/>• Updated weekly"]
        T1["TIER 1: Topic Clusters<br/>• Auto-grouped by theme<br/>• Cluster summary + members<br/>• Updated on new content"]
        T2["TIER 2: Atomic Memories<br/>• facts[] + narrative<br/>• Full metadata & tags<br/>• Multimodal vectors"]

        T0 --> T1 --> T2
    end

    subgraph Retrieval["🔍 STEP 4: DUAL RETRIEVAL"]
        direction TB
        R1{"Query<br/>Complexity?"}
        R2["WIKI PATH (FREE)<br/>• Cached markdown<br/>• Topic tree<br/>• Decision log"]
        R3["RAG PATH (Fast)<br/>• BM25 + Vector hybrid<br/>• RRF fusion<br/>• < 100ms"]
        R4["LLM PATH (Deep)<br/>• Semantic ranking<br/>• CoT decomposition<br/>• Complex queries"]
        R5["SUFFICIENCY CHECK<br/>Stop early if enough<br/>Expand if needed"]

        R1 -->|"Simple/Browse"| R2
        R1 -->|"Keyword/Fact"| R3
        R1 -->|"Complex/Why"| R4
        R3 --> R5
        R4 --> R5
    end

    subgraph Output["📤 STEP 5: OUTPUT FORMATS"]
        direction TB
        O1["WIKI<br/>• FREE reads<br/>• Auto-updated<br/>• Topics/Decisions"]
        O2["SEARCH<br/>• Progressive disclosure<br/>• Index → Full → Source<br/>• Multimodal results"]
        O3["GROUNDED RESPONSE<br/>• Answer + Citations<br/>• Source permalinks<br/>• Confidence score"]
        O4["TRAINING DATA<br/>• Instruction pairs<br/>• Trajectories<br/>• Quality filtered"]
        O5["MCP SERVER<br/>• Agent integration<br/>• Tool interface<br/>• SDK"]
    end

    subgraph Lifecycle["🔄 BACKGROUND: MEMORY LIFECYCLE"]
        direction TB
        L1["NOVELTY DETECTION<br/>Skip duplicates<br/>Reinforce similar"]
        L2["SELF-EVOLUTION<br/>Auto-update summaries<br/>on CRUD events"]
        L3["FORGETTING<br/>Ebbinghaus curve<br/>Source-aware decay"]
        L4["CONFLICT DETECTION<br/>Find contradictions<br/>Temporal supersession"]
    end

    %% Connections
    Sources --> Pipeline
    Pipeline --> Storage
    Storage --> Retrieval
    Retrieval --> Output
    Storage <--> Lifecycle

    style UserJourney fill:#e1f5fe
    style Sources fill:#fff3e0
    style Pipeline fill:#f3e5f5
    style Storage fill:#e8f5e9
    style Retrieval fill:#fce4ec
    style Output fill:#e0f2f1
    style Lifecycle fill:#fff8e1
```

---

## Part 2: Why Wiki-First Architecture Matters

This is the **#1 differentiator** from competitors. Most users don't need LLM for every query.

```mermaid
flowchart LR
    subgraph Traditional["❌ TRADITIONAL: memU, Mem0, Zep"]
        direction TB
        TQ["Every Query"] --> TR["Vector Retrieve"]
        TR --> TL["LLM Generate"]
        TL --> TC["$0.05 per query"]

        TN["100 queries/day<br/>= $5.00/day<br/>= $150/month"]
    end

    subgraph WikiFirst["✅ BEEVER ATLAS: Wiki-First"]
        direction TB
        WQ["Query"]
        WD{"What type?"}

        WQ --> WD
        WD -->|"80% Browse/Explore"| WW["Wiki<br/>(Cached)"]
        WD -->|"15% Search"| WS["Hybrid Search<br/>(Embedding only)"]
        WD -->|"5% Complex"| WL["LLM Generate"]

        WW --> WC1["FREE"]
        WS --> WC2["$0.001"]
        WL --> WC3["$0.05"]

        WN["100 queries/day<br/>= $1.35/day<br/>= $40/month"]
    end

    subgraph Savings["💰 SAVINGS"]
        direction TB
        S1["3.7x CHEAPER"]
        S2["Better UX<br/>(instant wiki)"]
        S3["Exploration<br/>encouraged"]
    end

    Traditional -.->|"vs"| WikiFirst
    WikiFirst --> Savings

    style Traditional fill:#ffebee
    style WikiFirst fill:#e8f5e9
    style Savings fill:#e3f2fd
```

### Wiki Content Structure

```
┌─────────────────────────────────────────────────────────────────────────┐
│  📖 WIKI: Engineering Knowledge Base                                     │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  📄 OVERVIEW (Tier 0)                                                    │
│  ├── "Our engineering team owns 12 services focused on..."              │
│  ├── Key Themes: [Auth, Payments, Data Pipeline, Infrastructure]        │
│  └── Recent: "Migrated to Kubernetes (Jan 2025)"                        │
│                                                                          │
│  📁 TOPICS (Tier 1)                                                      │
│  ├── 🔐 Authentication (23 memories)                                     │
│  │   └── "OAuth2 + JWT, migrated from sessions in Q3 2024"              │
│  ├── 💳 Payments (18 memories)                                           │
│  │   └── "Stripe integration with retry logic"                          │
│  ├── 🗄️ Database (31 memories)                                          │
│  │   └── "PostgreSQL + Redis, considering CockroachDB"                  │
│  └── 🚀 Infrastructure (15 memories)                                     │
│      └── "AWS EKS, Terraform, ArgoCD"                                   │
│                                                                          │
│  📋 DECISIONS (Extracted from Tier 2)                                    │
│  ├── 2025-01-15: "Chose Prisma over TypeORM - better DX"                │
│  ├── 2025-01-10: "Added Redis for session caching"                      │
│  └── 2025-01-05: "Delayed K8s migration by 2 weeks"                     │
│                                                                          │
│  👥 PEOPLE (Entity extraction)                                           │
│  ├── Alice: [auth, security]                                            │
│  ├── Bob: [payments, infrastructure]                                     │
│  └── Carol: [database, performance]                                      │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Part 3: True Multimodal Architecture

Unlike competitors that only support text, Beever Atlas uses **unified embedding space** for cross-modal search.

```mermaid
flowchart TB
    subgraph Input["📥 MULTIMODAL INPUT"]
        I1["📝 Text<br/>Slack, Notion, docs"]
        I2["🖼️ Images<br/>Diagrams, screenshots"]
        I3["📄 PDFs<br/>Specs, contracts"]
        I4["🎥 Videos<br/>Meetings, demos"]
        I5["🎤 Audio<br/>Voice memos"]
    end

    subgraph Processing["⚙️ MODALITY-SPECIFIC PROCESSING"]
        P1["Text Extraction"]
        P2["Vision Analysis<br/>(Gemini Vision)"]
        P3["Document Parsing<br/>(PyMuPDF)"]
        P4["Frame Extraction<br/>+ Transcription<br/>(Whisper)"]
        P5["Transcription<br/>(Whisper)"]
    end

    subgraph Unified["🎯 UNIFIED EMBEDDING SPACE"]
        direction TB
        U1["Jina v4 Multimodal<br/>2048 dimensions"]

        subgraph Vectors["Named Vectors in Weaviate"]
            V1["text_vector"]
            V2["image_vector"]
            V3["doc_vector"]
        end

        U1 --> Vectors

        Note1["Same query can match<br/>across ALL modalities"]
    end

    subgraph Search["🔍 CROSS-MODAL SEARCH"]
        S1["Query: 'auth flow diagram'"]
        S2["Results:"]
        S3["• 🖼️ OAuth2 diagram (0.95)"]
        S4["• 📄 Auth spec PDF (0.88)"]
        S5["• 📝 Slack discussion (0.82)"]
        S6["• 🎥 Video frame (0.78)"]
    end

    I1 --> P1
    I2 --> P2
    I3 --> P3
    I4 --> P4
    I5 --> P5

    P1 & P2 & P3 & P4 & P5 --> Unified
    Unified --> Search

    style Input fill:#e3f2fd
    style Processing fill:#f3e5f5
    style Unified fill:#e8f5e9
    style Search fill:#fff3e0
```

### Competitor Comparison: Multimodal Support

| Capability | memU | Mem0 | MemOS | Zep/Graphiti | **Beever Atlas** |
|------------|------|------|-------|--------------|------------------|
| Text | ✅ | ✅ | ✅ | ✅ | ✅ |
| Images | ✅ | ❌ | ❌ | ❌ | ✅ |
| PDFs | ✅ | ❌ | ✅ | ❌ | ✅ |
| Video | ✅ | ❌ | ❌ | ❌ | ✅ |
| Audio | ✅ | ❌ | ❌ | ❌ | ✅ |
| Cross-modal search | ❌ | ❌ | ❌ | ❌ | **✅** |
| Unified embeddings | ❌ | ❌ | ❌ | ❌ | **✅** |

---

## Part 4: Memory Lifecycle Management

Beever Atlas doesn't just store memories - it **evolves** them intelligently.

```mermaid
flowchart TB
    subgraph Ingest["📥 NEW CONTENT ARRIVES"]
        I1["New message<br/>from Slack"]
    end

    subgraph Novelty["🔍 NOVELTY DETECTION"]
        N1{"Check similarity<br/>to existing"}
        N2["≥ 95%: SKIP<br/>(exact duplicate)"]
        N3["≥ 85%: REINFORCE<br/>(similar exists)"]
        N4["≥ 70%: LINK<br/>(related content)"]
        N5["< 70%: ADD<br/>(novel content)"]
    end

    subgraph Evolution["🔄 SELF-EVOLUTION"]
        E1["Memory Added/Updated/Deleted"]
        E2["Find affected clusters"]
        E3["LLM patches summaries"]
        E4["Update Tier 0 if significant"]
        E5["Wiki auto-refreshes"]
    end

    subgraph Forgetting["⏳ INTELLIGENT FORGETTING (Ebbinghaus)"]
        direction TB
        F1["Retention Formula:<br/>R(t) = e^(-t/S) × source_multiplier"]

        subgraph Multipliers["Source Credibility"]
            M1["📚 Docs: 2.0x<br/>(authoritative)"]
            M2["💬 Internal: 1.5x<br/>(important)"]
            M3["🌐 Web: 0.5x<br/>(ephemeral)"]
            M4["📱 Social: 0.3x<br/>(very ephemeral)"]
        end

        F2["Low retention → Archive/Prune"]
    end

    subgraph Reinforce["💪 SPACED REPETITION"]
        R1["Memory Retrieved"]
        R2["Stability increases"]
        R3["Decay slows down"]
        R4["Important memories persist"]
    end

    subgraph Conflict["⚠️ CONTRADICTION DETECTION"]
        C1["Find similar facts"]
        C2["LLM checks contradiction"]
        C3{"Contradicts?"}
        C4["Set old.invalid_at = new.valid_at"]
        C5["Track supersession chain"]
        C6["Keep both for history"]
    end

    Ingest --> Novelty
    N1 --> N2 & N3 & N4 & N5
    N5 --> Evolution
    N3 --> Reinforce

    Evolution --> E1 --> E2 --> E3 --> E4 --> E5

    Forgetting
    Reinforce --> R1 --> R2 --> R3 --> R4

    N5 --> Conflict
    C1 --> C2 --> C3
    C3 -->|Yes| C4 --> C5
    C3 -->|No| C6

    style Ingest fill:#e3f2fd
    style Novelty fill:#fff3e0
    style Evolution fill:#e8f5e9
    style Forgetting fill:#ffebee
    style Reinforce fill:#f3e5f5
    style Conflict fill:#fce4ec
```

---

## Part 5: Dual Retrieval System

Beever Atlas uses **two retrieval modes** that automatically select based on query complexity.

```mermaid
flowchart TB
    subgraph Query["🔍 QUERY ARRIVES"]
        Q1["'What is our auth system?'<br/>or<br/>'Why did we choose JWT over sessions<br/>and how does it relate to the<br/>mobile app security requirements?'"]
    end

    subgraph Classifier["🧠 QUERY CLASSIFIER"]
        C1{"Analyze Query"}
        C2["SIMPLE:<br/>• Factual lookup<br/>• Keyword search<br/>• Overview request"]
        C3["COMPLEX:<br/>• 'Why' questions<br/>• Comparison<br/>• Multi-hop reasoning<br/>• Temporal analysis"]
    end

    subgraph RAG["⚡ RAG PATH (Fast)"]
        direction TB
        R1["Hierarchical Routing"]
        R2["Query Depth:<br/>OVERVIEW → Tier 0<br/>TOPIC → Tier 1<br/>DETAIL → Tier 2"]
        R3["Hybrid Search<br/>BM25 + Vector"]
        R4["RRF Fusion<br/>(k=60)"]
        R5["< 100ms latency"]
    end

    subgraph LLM["🧠 LLM PATH (Deep)"]
        direction TB
        L1["CoT Decomposition"]
        L2["Break into sub-questions:<br/>1. What is our auth system?<br/>2. Why JWT vs sessions?<br/>3. Mobile security reqs?"]
        L3["Parallel retrieval"]
        L4["LLM synthesizes"]
        L5["< 3s latency"]
    end

    subgraph Sufficiency["✅ SUFFICIENCY CHECK"]
        S1{"Enough info?"}
        S2["RETURN results"]
        S3["EXPAND search<br/>• Drill down tier<br/>• Broaden query<br/>• Include related"]
    end

    subgraph Response["📤 GROUNDED RESPONSE"]
        Resp1["Answer with citations"]
        Resp2["Source permalinks"]
        Resp3["Confidence score"]
    end

    Query --> Classifier
    C1 --> C2 & C3
    C2 --> RAG
    C3 --> LLM

    RAG --> R1 --> R2 --> R3 --> R4 --> R5 --> Sufficiency
    LLM --> L1 --> L2 --> L3 --> L4 --> L5 --> Sufficiency

    S1 -->|Yes| S2 --> Response
    S1 -->|No| S3 --> R3

    style Query fill:#e3f2fd
    style Classifier fill:#fff3e0
    style RAG fill:#e8f5e9
    style LLM fill:#f3e5f5
    style Sufficiency fill:#fce4ec
    style Response fill:#e0f2f1
```

---

## Part 6: Technical Stack Mapping

How each technology serves the user journey.

```mermaid
flowchart TB
    subgraph UserLayer["👤 USER INTERFACE LAYER"]
        UI1["Web UI<br/>(React + Vite)"]
        UI2["REST API<br/>(FastAPI)"]
        UI3["MCP Server<br/>(Claude/Agent integration)"]
    end

    subgraph AppLayer["⚙️ APPLICATION LAYER"]
        A1["Ingestion Service<br/>Source adapters, pipeline"]
        A2["Retrieval Service<br/>Dual retrieval, sufficiency"]
        A3["Wiki Service<br/>Generation, caching"]
        A4["Lifecycle Service<br/>Decay, evolution, conflicts"]
    end

    subgraph MLLayer["🧠 ML/AI LAYER"]
        ML1["Gemini Flash Lite<br/>Metadata extraction<br/>$0.30/1M tokens"]
        ML2["Gemini Flash<br/>Response generation<br/>$0.60/1M tokens"]
        ML3["Jina v4<br/>Multimodal embeddings<br/>2048-dim unified"]
        ML4["Whisper API<br/>Audio transcription"]
    end

    subgraph DataLayer["💾 DATA LAYER"]
        D1["Weaviate Cloud<br/>• Vector storage (HNSW)<br/>• Named vectors<br/>• BM25 index<br/>• Cross-references"]
        D2["MongoDB<br/>• State management<br/>• Sync status<br/>• Relationships<br/>• Conflict log"]
        D3["Redis (optional)<br/>• Wiki cache<br/>• Session state"]
    end

    subgraph InfraLayer["🏗️ INFRASTRUCTURE"]
        I1["Docker Compose<br/>(local dev)"]
        I2["Kubernetes<br/>(production)"]
        I3["Celery/Dramatiq<br/>(background jobs)"]
    end

    UserLayer --> AppLayer
    AppLayer --> MLLayer
    AppLayer --> DataLayer
    DataLayer --> InfraLayer

    style UserLayer fill:#e3f2fd
    style AppLayer fill:#f3e5f5
    style MLLayer fill:#fff3e0
    style DataLayer fill:#e8f5e9
    style InfraLayer fill:#eceff1
```

### Technology Decision Matrix

| Component | Choice | Why (vs alternatives) |
|-----------|--------|----------------------|
| **Vector DB** | Weaviate | Named vectors for multimodal, built-in BM25, production-ready (vs Qdrant, Pinecone) |
| **Embeddings** | Jina v4 | 2048-dim unified multimodal space (vs OpenAI 1536-dim text-only) |
| **State DB** | MongoDB | Flexible schema for relationships, async via Motor (vs PostgreSQL rigidity) |
| **LLM (cheap)** | Gemini Flash Lite | $0.30/1M tokens, fast (vs GPT-4o-mini at $0.60) |
| **LLM (quality)** | Gemini Flash | $0.60/1M tokens, good quality (vs GPT-4o at $2.50) |
| **Backend** | FastAPI | Async-first, MCP support, Python ecosystem (vs Node.js) |
| **Frontend** | React + Vite | Fast dev, component ecosystem (vs Next.js complexity for MVP) |

---

## Part 7: Competitive Feature Matrix

### Feature-by-Feature Comparison

```mermaid
flowchart TB
    subgraph Features["📊 FEATURE COMPARISON"]
        direction TB

        subgraph Cost["💰 COST MODEL"]
            C1["memU/Mem0/Zep:<br/>Every query = LLM call<br/>~$0.05/query"]
            C2["Beever Atlas:<br/>Wiki-first = FREE reads<br/>~$0.01/query average"]
        end

        subgraph Modal["🎨 MULTIMODAL"]
            M1["memU: Text, Image, Video<br/>(separate spaces)"]
            M2["Mem0/Zep: Text only"]
            M3["Beever Atlas:<br/>Unified cross-modal<br/>(text query → image results)"]
        end

        subgraph Forget["⏳ MEMORY DECAY"]
            F1["memU/Mem0: None"]
            F2["MemOS: FIFO only"]
            F3["Beever Atlas:<br/>Ebbinghaus curve +<br/>source-aware multipliers"]
        end

        subgraph Graph["🔗 RELATIONSHIPS"]
            G1["memU: Category only"]
            G2["Mem0: Basic graph"]
            G3["MemOS: Rich edges"]
            G4["Beever Atlas:<br/>Rich edges +<br/>temporal supersession"]
        end
    end

    style Features fill:#f5f5f5
```

### Summary Table

| Feature | memU | Mem0 | MemOS | Zep/Graphiti | **Beever Atlas** |
|---------|------|------|-------|--------------|------------------|
| **Wiki-First (FREE reads)** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **True Cross-Modal Search** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Unified Embedding Space** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Ebbinghaus Forgetting** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Source Credibility Decay** | ❌ | ❌ | ❌ | ❌ | ✅ |
| **Bi-Temporal Model** | ❌ | ❌ | ❌ | ✅ | ✅ |
| **Contradiction Detection** | ❌ | ❌ | ✅ | ✅ | ✅ |
| **Self-Evolving Summaries** | ✅ | ❌ | Partial | ❌ | ✅ |
| **Dual Retrieval (RAG+LLM)** | ✅ | ❌ | ✅ | ✅ | ✅ |
| **CoT Query Decomposition** | ❌ | ❌ | ✅ | ❌ | ✅ |
| **Graph Relationships** | Category | Basic | Rich | Rich | Rich |
| **Training Data Export** | ❌ | ❌ | ❌ | ❌ | ✅ |

---

## Part 8: Data Flow - Complete Picture

```mermaid
flowchart TB
    subgraph Sources["📥 DATA SOURCES"]
        Slack["Slack"]
        Notion["Notion"]
        GitHub["GitHub"]
        Files["Local Files"]
        Web["Web/URLs"]
        Calendar["Calendar"]
    end

    subgraph Adapters["🔌 SOURCE ADAPTERS"]
        A1["SlackAdapter"]
        A2["NotionAdapter"]
        A3["GitHubAdapter"]
        A4["FileSystemAdapter"]
        A5["WebScraperAdapter"]
        A6["CalendarAdapter"]
    end

    subgraph Normalize["📋 NORMALIZED INPUT"]
        N1["RawContent<br/>• content: str<br/>• source_type: str<br/>• source_id: str<br/>• source_url: str<br/>• timestamp: datetime"]
    end

    subgraph Pipeline["⚙️ PROCESSING PIPELINE"]
        P1["1. PREPROCESS<br/>Detect modality<br/>Parse PDF/Image/Video"]
        P2["2. EXTRACT<br/>facts[] + narrative<br/>(Gemini Flash Lite)"]
        P3["3. CLASSIFY<br/>domain_tags<br/>entity_tags<br/>action_tags<br/>knowledge_type"]
        P4["4. EMBED<br/>Jina v4 (2048-dim)<br/>text/image/doc vectors"]
        P5["5. DEDUPE<br/>Novelty detection<br/>Skip/Reinforce/Link"]
        P6["6. CLUSTER<br/>Topic assignment<br/>Label propagation"]
        P7["7. PERSIST<br/>Weaviate + MongoDB"]
    end

    subgraph Memory["💾 ATOMIC MEMORY"]
        M1["AtomicMemory<br/>├── id<br/>├── facts: list[str]<br/>├── narrative: str<br/>├── source_*<br/>├── *_tags<br/>├── knowledge_type<br/>├── text_vector<br/>├── image_vector<br/>├── stability<br/>├── valid_at<br/>├── invalid_at<br/>├── cluster_id<br/>└── collection_id"]
    end

    subgraph Hierarchy["📚 HIERARCHICAL STORAGE"]
        H0["Tier 0: Collection Summary"]
        H1["Tier 1: Topic Clusters"]
        H2["Tier 2: Atomic Memories"]

        H0 --> H1 --> H2
    end

    subgraph Lifecycle["🔄 LIFECYCLE SERVICES"]
        L1["Self-Evolution<br/>Auto-update summaries"]
        L2["Forgetting<br/>Ebbinghaus decay"]
        L3["Conflict Detection<br/>Temporal supersession"]
    end

    subgraph Output["📤 OUTPUT INTERFACES"]
        O1["Wiki API<br/>FREE reads"]
        O2["Search API<br/>Hybrid RAG"]
        O3["Ask API<br/>Grounded responses"]
        O4["Export API<br/>Training data"]
        O5["MCP Server<br/>Agent tools"]
    end

    Sources --> Adapters
    Adapters --> Normalize
    Normalize --> Pipeline
    P1 --> P2 --> P3 --> P4 --> P5 --> P6 --> P7
    P7 --> Memory
    Memory --> Hierarchy
    Hierarchy <--> Lifecycle
    Hierarchy --> Output

    style Sources fill:#e3f2fd
    style Adapters fill:#fff3e0
    style Pipeline fill:#f3e5f5
    style Memory fill:#e8f5e9
    style Hierarchy fill:#e0f2f1
    style Lifecycle fill:#fff8e1
    style Output fill:#fce4ec
```

---

## Part 9: Key Innovations Explained

### Innovation 1: Wiki-First Pattern

```
PROBLEM: Every query costs money (LLM calls)
SOLUTION: Pre-generate browsable wiki from memories

HOW IT WORKS:
1. Tier 0/1 summaries are generated on content change
2. Wiki markdown is cached and served statically
3. 80% of user interactions are just browsing
4. Only "Ask" queries hit the LLM

RESULT: 5x cost reduction vs competitors
```

### Innovation 2: Unified Multimodal Space

```
PROBLEM: Can't search for "auth diagram" and find images
SOLUTION: Jina v4 embeds all modalities in same 2048-dim space

HOW IT WORKS:
1. Text, images, PDFs → same vector space
2. Semantic similarity works across modalities
3. Named vectors in Weaviate allow modality-specific indexes
4. Query can match any modality

RESULT: "Find deployment architecture" returns diagrams, docs, and discussions
```

### Innovation 3: Intelligent Forgetting

```
PROBLEM: Memory grows forever, old info clutters results
SOLUTION: Ebbinghaus curve + source-aware decay

HOW IT WORKS:
1. Retention = e^(-time/Stability) × source_multiplier
2. Documentation decays slowly (2.0x multiplier)
3. Social media decays fast (0.3x multiplier)
4. Frequently accessed memories gain stability
5. Low-retention memories are archived

RESULT: Fresh, relevant memories; authoritative sources persist
```

### Innovation 4: Temporal Supersession

```
PROBLEM: Facts change over time, old info misleads
SOLUTION: Bi-temporal model with contradiction detection

HOW IT WORKS:
1. Each memory has valid_at, invalid_at, created_at, expired_at
2. On new fact, find similar existing facts
3. LLM detects contradictions
4. Old fact gets invalid_at = new fact's valid_at
5. Supersession chain tracked for history

RESULT: "What was true on Jan 15?" queries work correctly
```

---

## Part 10: Implementation Phases

```mermaid
gantt
    title Beever Atlas Implementation Roadmap
    dateFormat  YYYY-MM-DD

    section Phase 1: MVP
    Foundation (FastAPI, Weaviate, Models)    :p1a, 2025-01-20, 7d
    Local File + GitHub Adapters              :p1b, after p1a, 5d
    Memory Extraction + Embedding             :p1c, after p1a, 7d
    Hierarchical Storage (3-tier)             :p1d, after p1c, 5d
    Hybrid Search (BM25 + Vector)             :p1e, after p1d, 3d
    Wiki Generation + Caching                 :p1f, after p1e, 5d
    Ask with Citations                        :p1g, after p1f, 4d
    Web UI (Browse, Search, Ask)              :p1h, after p1f, 10d

    section Phase 2: Core Enhancements
    Dual Retrieval (RAG + LLM)                :p2a, after p1h, 7d
    Self-Evolving Summaries                   :p2b, after p2a, 7d
    Sufficiency Checking                      :p2c, after p2a, 4d
    Novelty Detection                         :p2d, after p2b, 5d
    Forgetting (Ebbinghaus)                   :p2e, after p2d, 5d
    Slack + Notion Adapters                   :p2f, after p1h, 10d

    section Phase 3: Advanced Features
    Graph Relationships                       :p3a, after p2e, 10d
    Contradiction Detection                   :p3b, after p3a, 7d
    Bi-Temporal Model                         :p3c, after p3b, 7d
    CoT Query Decomposition                   :p3d, after p2c, 7d
    Memory Scheduling                         :p3e, after p3c, 10d
    MCP Server                                :p3f, after p2b, 7d

    section Phase 4: Extended Use Cases
    Meeting Minutes Processing                :p4a, after p3a, 10d
    Video/Audio Processing                    :p4b, after p4a, 14d
    Training Data Export                      :p4c, after p3c, 7d
    Multi-Tenancy                             :p4d, after p3e, 14d
```

---

## Quick Reference: When to Use What

| User Intent | Path | Cost | Latency |
|-------------|------|------|---------|
| "Show me the overview" | Wiki → Tier 0 | FREE | ~50ms |
| "What topics do we have?" | Wiki → Tier 1 list | FREE | ~50ms |
| "Tell me about authentication" | Wiki → Tier 1 detail | FREE | ~50ms |
| "Find messages about Redis" | Search → Hybrid RAG | ~$0.001 | ~100ms |
| "What did Alice say about caching?" | Search → Hybrid RAG | ~$0.001 | ~100ms |
| "Why did we choose PostgreSQL?" | Ask → LLM Path | ~$0.02 | ~2s |
| "Compare our auth approaches over time" | Ask → CoT + LLM | ~$0.05 | ~3s |

---

## Appendix: Glossary

| Term | Definition |
|------|------------|
| **Atomic Memory** | Single unit of knowledge with facts[], narrative, and metadata |
| **Topic Cluster** | Group of related memories with summary (Tier 1) |
| **Collection Summary** | High-level overview of all knowledge (Tier 0) |
| **Wiki-First** | Pattern where reads are cached, LLM only for complex queries |
| **Dual Retrieval** | Automatic selection between RAG (fast) and LLM (deep) |
| **Sufficiency Check** | Stop retrieval early when enough context found |
| **RRF** | Reciprocal Rank Fusion - combining multiple search results |
| **Ebbinghaus Curve** | Memory decay formula: R(t) = e^(-t/S) |
| **Temporal Supersession** | When new fact invalidates old fact |
| **Cross-Modal Search** | Text query finding images/videos/docs |
| **Named Vectors** | Weaviate feature for modality-specific indexes |

---

*This document provides a comprehensive overview of Beever Atlas architecture. For implementation details, see PIVOT_PLAN.md and MVP_PLAN.md.*
