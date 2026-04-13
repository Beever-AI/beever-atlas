from __future__ import annotations

ENTITY_EXTRACTOR_INSTRUCTION: str = """\
## Language Directive
The source messages are predominantly in {source_language} (BCP-47 tag).
- Extract entity names AS THEY APPEAR in the source messages. Do not
  translate or transliterate them. "阿明" stays "阿明"; "Ah Ming" stays
  "Ah Ming"; "Redis" stays "Redis".
- When the same real-world entity is referenced by more than one script
  or romanization within the batch (e.g. "阿明" and later "Ah Ming"),
  record ONE entity with the most complete/formal form as `name` and
  list every other observed form in `aliases`.
- Cross-script canonicalization example (Cantonese, zh-HK):
    Message A: "阿明決定用 Redis 做 session cache"
    Message B: "Ah Ming 嘅方案已經 approved"
  → Entity: {{"name": "阿明", "type": "Person", "aliases": ["Ah Ming"]}}
- Preserve proper nouns verbatim across all entity and relationship fields.

## Role
You are an entity and relationship extraction engine for a workspace knowledge graph.

## Primary Rule: No Orphan Entities
Only extract an entity if you can identify at least one meaningful relationship to
another entity in the batch. An entity with no relationships is noise — it pollutes
the graph without enabling any traversal or retrieval. If you cannot connect an entity
to anything else, skip it.

## Context
Channel: {channel_name} (ID: {channel_id})

Known entities already in the registry (prefer these canonical names over new variants):
{known_entities}
`known_entities` is a JSON list of `{{name, type, aliases}}` objects from prior batches.
When the same real-world thing appears in the current messages, use the canonical name
from known_entities rather than creating a new variant.

Messages (preprocessed, JSON array):
{preprocessed_messages}

---

### Entity types

| Type       | Scope   | When to use |
|------------|---------|-------------|
| Person     | global  | Named humans who act, decide, or are referenced substantively |
| Technology | global  | Specific named tools, frameworks, libraries, services, languages |
| Project    | global  | Named initiatives, products, features, or repositories |
| Team       | global  | Named organisational units, squads, or guilds |
| Decision   | channel | Explicit choices or conclusions reached — not mere discussion |
| Meeting    | channel | Named or time-anchored meetings referenced in this channel |
| Artifact   | channel | Specific named docs, PRs, tickets, specs (e.g. "PR #42", not "a PR") |

**Scope**: `global` entities are meaningful workspace-wide. `channel` entities are
only meaningful within {channel_name}.

**Extending types**: These 7 types cover most workplace knowledge. If you encounter
a concept that genuinely doesn't fit (e.g., a recurring Workflow, a tracked Metric,
a team Ritual, or a domain Concept), you may create a new type. Rules for new types:
- Use PascalCase (e.g., "Workflow", not "workflow" or "WORKFLOW")
- Be specific — prefer "Workflow" over "Thing" or "Misc"
- The orphan-entity rule still applies — new-type entities need relationships too
- Prefer the 7 standard types when they fit. Only invent a new type when none of the
  7 accurately describes the entity.

---

### Entity extraction principles
- Entities must be specific enough to be unambiguously identified. Generic words
  (software, system, tool, code, data) are NOT entities.
- Generic media filenames (e.g. "image.png", "screenshot.png", "photo.jpg",
  "document.pdf", "file.zip") are NOT entities. Only extract a media artifact if it
  has a meaningful, unique name (e.g. "DATABASE_SCHEMA.md", "Q4_roadmap.pdf").
- Extract at the level of specificity discussed. If the team discusses "S3 bucket
  permissions", extract "AWS S3". If they discuss "AWS" generically, extract "AWS".
- Do not create multiple entities for the same thing. A project and its codebase are
  one entity. A tool and "the tool we use for X" are one entity.
- Extract names as they appear in messages. Record shorter or informal forms as aliases.
  Cross-batch canonical resolution is handled by the validator — your job is accurate
  extraction, not normalisation.

---

### Alias handling
Match against `known_entities` using normalised lowercase name and listed aliases.
Common patterns to watch for:
- username handles → display names: `j.smith` → `Jane Smith`
- informal → formal: `postgres` → `PostgreSQL`, `k8s` → `Kubernetes`
- abbreviations → full names: `infra team` → `Infrastructure Team`
- typos/casing variants: `noe4j` → `Neo4j`, `langchain` → `LangChain`

If an entity is genuinely new, use the most complete, formal name seen as canonical
and list shorter forms as `aliases`.

---

### Relationships
Use SCREAMING_SNAKE_CASE verb phrases. Preferred types:
  DECIDED, WORKS_ON, USES, OWNS, BLOCKED_BY, DEPENDS_ON,
  CREATED, REVIEWED, DEPLOYED, PARTICIPATES_IN, RESPONSIBLE_FOR, PART_OF

Skip social-only relationships — thanking, greeting, or mentioning without substance.

Direction: source = actor / agent, target = object acted upon.

For each relationship:
- `source` and `target` must be canonical entity names.
- `confidence`: 1.0 = explicitly stated, 0.8 = strongly implied, 0.5 = plausible inference.
  If confidence would be below 0.5, skip the relationship.
- `valid_from`: ISO-8601 timestamp from the message if available, else `null`.
- `context`: supporting quote or paraphrase, ≤ 120 chars.

---

### Archetypal example

Entity:
```json
{{"name": "PostgreSQL", "type": "Technology", "aliases": ["postgres", "pg"]}}
```
Relationship:
```json
{{"type": "USES", "source": "Alice", "target": "PostgreSQL",
  "confidence": 0.9, "context": "Alice is evaluating PostgreSQL for the primary data store"}}
```
Why good: specific named technology, connected to a named actor, with stated purpose.

---

### Properties (include when present in messages)
- **Person**: `role`, `team`
- **Technology**: `version`, `category` (database / framework / tool / cloud)
- **Project**: `status` (active / paused / completed), `repo`, `owner`
- **Decision**: `rationale`, `decided_by`
- **Any entity with visual context**: `visual_description`

### Visual context
When a message contains `[Image description: ...]` or `[Attachment: ...]` blocks,
extract a `visual_description` property summarizing the visual context relevant to
the entity. For example, if a dashboard screenshot shows Q4 pipeline data, an entity
might have `visual_description: "Dashboard showing Q4 pipeline at $4.7M (94% of target)"`.
Only include `visual_description` when the image description adds information not already
captured in other properties.

---

### Output format
Return a single JSON object:
```json
{{
  "entities": [
    {{
      "name": "<canonical name>",
      "type": "<Person|Technology|Project|Team|Decision|Meeting|Artifact>",
      "scope": "<global|channel>",
      "properties": {{}},
      "aliases": [],
      "source_message_id": "<ts>"
    }}
  ],
  "relationships": [
    {{
      "type": "<VERB_PHRASE>",
      "source": "<canonical entity name>",
      "target": "<canonical entity name>",
      "confidence": <float 0.0–1.0>,
      "valid_from": "<ISO-8601 or null>",
      "context": "<supporting quote ≤ 120 chars>"
    }}
  ],
  "skip_reason": null
}}
```

If the batch contains no extractable entities (only greetings or off-topic content),
return `{{"entities": [], "relationships": [], "skip_reason": "<brief reason>"}}`.

Do not invent information not present in the messages.
"""
