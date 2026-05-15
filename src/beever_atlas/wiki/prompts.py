"""Prompt templates for each wiki page type.

Design philosophy: STRUCTURE FIRST — diagrams, tables, bullet points, then supporting text.
Domain-agnostic — works for tech, community, research, personal, and enterprise channels.

Phase 4 (wiki_compiler_v2): the Key Facts table becomes a deterministic,
compiler-rendered block. The v2 prompts emit the literal marker
`<<KEY_FACTS_TABLE>>` on its own line; the compiler splices the rendered
table after `_postprocess_content`. The legacy (pre-v2) prompts are kept
intact below so `wiki_compiler_v2=OFF` behavior stays byte-identical.
"""

from __future__ import annotations

# Instruction fragment that replaces the Key Facts table instruction in v2.
_KEY_FACTS_MARKER_INSTRUCTION = (
    "3. **Key Facts** — leave the single literal token `<<KEY_FACTS_TABLE>>` "
    "on its own line. The compiler will replace it with a deterministic "
    "table. Do NOT write the table yourself."
)

OVERVIEW_PROMPT = """You are a knowledge wiki compiler. Create an **Overview** page for this channel.

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary"}}

## Content structure (follow this order strictly — INTRO FIRST, then visuals)
1. **Brief intro** — 2-3 sentences describing what this channel is about, its purpose, and the key knowledge areas it covers. THIS MUST BE THE VERY FIRST CONTENT. Set the context for the reader before showing any visuals.
2. **Concept map** — ```mermaid flowchart showing how the main themes relate. Use the topic relationships data to build accurate connections. Prefer a small number of high-signal edges (≤ 12) over an exhaustive web — the goal is a glanceable overview, not a citation graph.
3. **Key Highlights table** — GFM table summarizing: total topics, decisions made, key contributors, resources shared, active period. **CRITICAL**: use the EXACT numbers from the channel-data block below — `{decisions_count}` for "Decisions Made", `{people_count}` for "Key Contributors", `{media_count}` for "Resources Shared". Do NOT recompute these from topic descriptions; the provided counts are authoritative.
4. **Key contributors** — bullet list of most active people and their roles/expertise. When folder structure exists, GROUP contributors by the folder/area they're most active in (e.g. `**Security & Code Quality** — Jacky Chan, Alan Yang`). Otherwise list flat.
5. **Tools & resources** — if technologies or tools data exists, show as bullet list or GFM table. Skip this section entirely if no tools/technologies are relevant.

DROPPED SECTIONS (do NOT emit):
- "Topics at a glance" — the renderer shows folder cards + topic cards directly with previews; a duplicate bullet list is noise.
- "Recent momentum" — the renderer shows freshness chips in the header; a vague prose summary adds no signal.

AUTO-INJECTED SECTIONS (the wiki engine appends these after your output — do NOT duplicate them under any name):
- "Recent Updates"
- "Project Status" (a.k.a. "Project Status & Progress")
- "Core Discussions"

## Writing style
- **Synthesize, don't narrate.** Transform raw facts into insights. Write "The team identified context graphs as a key architecture pattern for agent safety [1]" — NOT "Jacky Chan shared a link about context graphs [1]".
- FORBIDDEN phrases: "shared a link", "shared an article", "posted about", "mentioned that", "noted that", "presented a". These produce activity-log narration, not knowledge.
- Avoid filler phrases that add no information: "crucial for", "under discussion", "actively testing", "plays a key role in", "is a subject of ongoing", "highlights the importance of". Replace with concrete verbs naming the specific outcome or blocker.
- Lead with the INSIGHT or CONCLUSION, then cite the source. The reader wants to know what matters, not who posted what.
- Use active voice describing the knowledge itself: "Context graphs prevent agents from using expired data [3]" — not "It was shared that context graphs prevent..."
- When multiple people contributed to a theme, synthesize their collective input rather than listing each person's individual share.

## Adaptive instructions
- Adapt your language to match the channel's domain. If the data is technical, use technical terms. If it's a community or personal channel, use appropriate casual language.
- The concept map should reflect the actual relationships in the data — don't force a technical "architecture" if the channel discusses non-technical topics.
- Only include sections where data exists. If there are no technologies, skip "Tools & resources". If there are no decisions, don't mention them prominently.

## Rules
- Do NOT start with a # heading (title rendered separately)
- Each numbered section above MUST be a ## heading (e.g. `## Concept Map`, `## Key Highlights`). Use ### for sub-sections within them. This creates a navigable table of contents.
- Use ```mermaid for diagrams. Keep syntax SIMPLE — use ONLY `graph TD` with `ID[Label] --> ID[Label]` edges. Every node MUST use a short ID with a descriptive label: `DS[Data Sources]` not just `Data Sources`. FORBIDDEN: subgraph, end, style, classDef, parentheses inside brackets, quotes inside labels, `-- text -->` dash-space style labels, semicolons, chained arrows like `A --> B --> C` (use separate lines: `A --> B` then `B --> C`). USE `-->|label|` pipe-style to label edges (e.g., `A -->|uses| B`). Example: `graph TD\n    DS[Data Sources] -->|feeds| PR[Processing]\n    PR -->|outputs| ST[Storage]`. **Every ```mermaid block MUST end with a line containing only ``` (three backticks) — never leave a mermaid block unclosed.**
- Use ```chart for data charts with exact JSON: {{"type":"donut","title":"...","data":[{{"name":"X","value":N}}],"xKey":"name","series":["value"]}}
- Use GFM tables for structured data. ALWAYS include the header separator row. Example:\n  | Column A | Column B |\n  |----------|----------|\n  | value 1  | value 2  |
- Use bullet points over paragraphs when listing facts
- Add [N] citation markers on factual claims. **Maximum 3 citations per sentence** — never list long chains. (use actual numbers: [1], [2], [3]). **Maximum 3 citation markers per sentence.** If a claim has many sources, pick the 2-3 most relevant — do NOT list long chains like [1, 3, 4, 5, 6, 7, 8, 9, 10] or [1][2][5][6][8][9][10]. Long citation chains clutter the text and are unreadable. **CRITICAL: Key contributors bullets may have at most 3 [N] markers total — pick the most representative citations and stop.**
- Do NOT use @, #, or $ prefixes for entity names — just write names normally
- If media (images/PDFs/links) exist, embed important ones with a brief description line BEFORE each embed explaining what it shows: `**Dashboard Overview** — Key metrics for the project.` then `![Dashboard](url)` on the next line. Do NOT use bare bullet points with just a link.
- Use ONLY inline [N] markers for citations. Do NOT generate any source list, reference section, citation block, or numbered bibliography — the UI renders citations separately. FORBIDDEN at end of content: `## Sources`, `### Sources`, `- [1] @Author...`, `[1]: Author...`.
- Keep output concise and readable: avoid repeated sections, avoid repeating the same link/title in multiple sections, and keep each bullet to one idea.
- Preserve canonical naming from provided data (do not invent alternate spellings like different bot/product names).
- **Maximum 15 edges** in the concept map mermaid diagram. Show only the strongest topic relationships — do NOT connect every topic to every other topic.
- **Maximum 10 tools** in the Tools & Resources section. Only list tools mentioned by 2+ people or in 3+ facts. Exclude generic tools like messaging apps (Slack, WhatsApp, iMessage), operating systems (macOS, Linux, Windows), and text editors (VS Code).

## Channel data
Channel: {channel_name}
Description: {description}
Summary: {text}
Themes: {themes}
Momentum: {momentum}
Contributor dynamics: {team_dynamics}
Decisions: {decisions_count} | Contributors: {people_count} | Projects: {projects_count} | Tools/Tech: {tech_count} | Media: {media_count}

Topics: {clusters_json}
Topic relationships: {topic_graph_edges_json}
Recent activity: {recent_activity_json}
Key contributors: {top_people_json}
Key decisions: {top_decisions_json}
Technologies/Tools: {technologies_json}
Projects/Initiatives: {projects_json}
Key entities from knowledge graph: {key_entities_json}
Entity relationships from knowledge graph: {key_relationships_json}
Media: {media_json}
Glossary preview (first 5 terms): {glossary_preview_json}
FAQ count across topics: {faq_count}
"""

TOPIC_PROMPT = """You are a knowledge wiki compiler. Create a **Topic** page for the cluster below.

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary"}}

## Content structure (follow this order strictly — TL;DR FIRST, then DIAGRAM, then text)
1. **TL;DR** — A single bold sentence summarizing the key insight of this topic. THIS MUST BE THE VERY FIRST LINE. Example: `**Multi-agent systems fail primarily due to inadequate memory engineering, not limited context windows.**`
2. **Concept diagram** — ```mermaid diagram showing how the key entities (people, decisions, concepts) relate within this topic. Use the entity relationships data to build accurate connections.
3. **Key Facts** — GFM table with columns: Fact, Source, Type, Importance — showing the most important facts with [N] citations
4. **Overview** — 2-3 sentences summarizing this topic: what it covers, why it matters, and its current state (AFTER the diagram and table)
5. **Decisions & outcomes** — if decisions exist, show as GFM table with columns: Decision, Status, Made By, Date. Use status badges: ✅ active, ❌ superseded, ⏳ pending. Skip if no decisions.
6. **Contributors** — bullet list of people involved with their roles (decision maker, contributor, expert, mentioned)
7. **Key Systems** — if non-people entities of type system / service / product / tool drive this topic, list them as a GFM table with columns: Name, Channel-context definition, Role in this topic. Use the `key_entities_json` data block to source these — only include entities the channel actually discusses with substance, NOT every mention. Skip when ≤2 such entities exist.
8. **Tools & resources** — if technologies/tools exist, bullet list. Skip if none.
9. **Current state & open questions** — what's resolved vs. still open. Use bullet points. Each open question MUST include when it was first raised (e.g., "(raised Jan 2026)") so readers can assess staleness.
10. **Media & Resources** — if media exists, each item MUST have a brief description line explaining what it shows/contains BEFORE the embed/link. **For IMAGES** (URLs ending in .png/.jpg/.jpeg/.gif/.webp/.svg, OR URLs from `files.mattermost.com` / `files.slack.com` / image-hosting domains), use IMAGE syntax `![Alt text](url)` — NOT link syntax `[Title](url)` — so the wiki renders an inline preview. **For PDFs and links**, use link syntax `[Title](url)`. **For VIDEOS** (URLs from youtube.com / vimeo.com or ending in .mp4/.webm), use link syntax `[Video title](url)` and the renderer will detect + embed. Do NOT use bare bullet points with just a link. Skip if none.
11. **See Also / Cross-references** — if related topics exist (use the `related_topics` data block — each entry has `title` and `id`), list them as MARKDOWN LINKS so the UI can route to the destination. **MAX 5 entries.** Each line: `- **[Title](/wiki/<slug>)** — one-line reason this topic is related (shared people, shared entities, downstream dependency, etc.)`. The `<slug>` is the part of the related topic's id AFTER the `topic-` prefix (e.g., id `topic-auth-migration` → `[Title](/wiki/auth-migration)`). When you reference a related Topic, People, or Glossary page in any prose section above, ALSO inline a `[[Page Title]]` wikilink so the cross-reference resolver picks it up for the wiki graph. **ONLY emit `[[Page Title]]` wikilinks for titles in the `compiled_topic_titles_json` list below — every other topic was dropped by the threshold gate and has no page; pointing at it produces a red broken link.** Pick the 5 most strongly related — DO NOT dump every other topic in the wiki. Skip the section entirely when no truly related topics exist (a wall of weakly-related links is worse than no links).

## Writing style
- **Synthesize, don't narrate.** Write "The team adopted a wiki-first architecture for 10x cost reduction [1]" — NOT "Thomas Chong shared that the wiki-first architecture offers cost reduction [1]".
- FORBIDDEN phrases: "shared a link", "shared an article", "posted about", "mentioned that", "noted that", "presented a", "highlighted that". These produce activity-log narration.
- AVOID filler phrases: "crucial for", "under discussion", "actively testing", "plays a key role in", "is a subject of ongoing", "highlights the importance of", "paves the way for", "underscores the need". Replace with concrete verbs naming the specific outcome, blocker, or unresolved question.
- Lead with the INSIGHT, then cite. The reader wants knowledge, not a timeline of who said what.
- In the Key Facts table, state the fact itself — not "Person X observed that [fact]". Write the fact directly.
- When listing open questions, include when the question was raised (e.g., "(raised Jan 2026)") so readers can assess staleness.

## Adaptive instructions
- The concept diagram should reflect actual entities and relationships from this topic — not a generic template
- If this topic is about a technical system, show system relationships. If about a community event, show event logistics. If about research, show methodology flow. Adapt to the content.
- Prioritize showing high-quality, high-importance facts in the Key Facts table
- Every factual claim MUST have a [N] citation marker

## Rules
- Do NOT start with a # heading (title rendered separately)
- Each numbered section above MUST be a ## heading (e.g. `## Concept Diagram`, `## Key Facts`). Use ### for sub-sections. This creates a navigable table of contents.
- ALWAYS include at least one ```mermaid diagram. Keep syntax SIMPLE — use ONLY `graph TD` with `ID[Label] --> ID[Label]` edges. Every node MUST use a short ID with a descriptive label: `AG[AI Agent]` not just `AI Agent`. FORBIDDEN: subgraph, end, style, classDef, parentheses inside brackets, quotes inside labels, `-- text -->` dash-space style labels, semicolons, chained arrows like `A --> B --> C` (use separate lines). USE `-->|label|` pipe-style to label edges with relationship type (e.g., `A -->|uses| B`, `A -->|decided| B`, `A -->|depends on| B`).
- Use ```chart for quantitative data with JSON: {{"type":"bar","title":"...","data":[...],"xKey":"name","series":["value"]}}
- Prefer tables and bullet points over long paragraphs
- Add [N] citation markers (actual numbers) on every factual claim. **Maximum 3 citations per sentence** — pick the most relevant, never list long chains like [1, 3, 4, 5, 6, 7].
- Do NOT use @, #, or $ prefixes — write entity names normally
- If media exists, embed: ![desc](url) for images, [name](url) for docs/links
- Use ONLY inline [N] markers for citations. Do NOT generate any source list, reference section, citation block, or numbered bibliography — the UI renders citations separately. FORBIDDEN at end of content: `## Sources`, `### Sources`, `- [1] @Author...`, `[1]: Author...`.
- Keep the page focused: maximum 8 rows in Key Facts; avoid duplicate facts across Key Facts and Details.
- Preserve canonical naming from provided data; do not alternate spellings for the same entity.
- **Maximum 12 edges** in the concept diagram. Focus on the most important entity relationships.
- **Thin-data rule**: If `fact_count` ≤ 8, use CONDENSED format instead of the full structure above: (1) TL;DR bold sentence, (2) Key Facts table (max 5 rows), (3) Summary paragraph. SKIP concept diagram, Decisions table, Open Questions, See Also, and Contributors. Do NOT produce placeholder text like "No decisions have been recorded" or "No open questions at this time" — just omit those sections entirely.

## Topic data
Title: {title}
Summary: {summary}
Current state: {current_state}
Open questions: {open_questions}
Impact: {impact_note}
Tags: {topic_tags}
Period: {date_range_start} – {date_range_end}
Authors: {authors}
Fact count: {fact_count}

Key facts: {key_facts_json}
Decisions: {decisions_json}
People: {people_json}
Technologies: {technologies_json}
Projects: {projects_json}

Knowledge graph entities in this topic: {key_entities_json}
Knowledge graph relationships in this topic: {key_relationships_json}

All facts (for citation sourcing): {member_facts_json}
Media: {media_json}
Related topics (for "See Also" section): {related_topics_json}

Compiled topic titles (the ONLY valid targets for `[[Page Title]]` wikilinks — any other title was dropped by the threshold gate and has no page; pointing at it produces a red broken link): {compiled_topic_titles_json}
"""

# ---------------------------------------------------------------------------
# Phase 4 v2 variants (used when wiki_compiler_v2=ON). Byte-identical to the
# legacy prompts above EXCEPT the Key Facts section instruction (3.) is
# replaced with the `<<KEY_FACTS_TABLE>>` marker directive and the
# "maximum 8 rows in Key Facts" rule is removed.
# ---------------------------------------------------------------------------

# Phase 5: when delimited response mode is active, the prompt suffix uses
# ###CONTENT### as a structural marker. Forbid the LLM from echoing it inside
# the body so the parser's rsplit semantics still recover the intended content.
_NO_MARKER_ECHO_INSTRUCTION = (
    "- Do NOT write the literal token `###CONTENT###`, `###SUMMARY###`, or `###END###` "
    "inside the body or summary text — those tokens are reserved structural markers."
)

TOPIC_PROMPT_V2 = TOPIC_PROMPT.replace(
    "3. **Key Facts** — GFM table with columns: Fact, Source, Type, Importance — showing the most important facts with [N] citations",
    _KEY_FACTS_MARKER_INSTRUCTION,
).replace(
    "- Keep the page focused: maximum 8 rows in Key Facts; avoid duplicate facts across Key Facts and Details.",
    "- Avoid duplicate facts across Key Facts and Details.\n" + _NO_MARKER_ECHO_INSTRUCTION,
)

# Thin-topic prompt: TL;DR + 3-sentence summary only. No diagram, no
# Open Questions, no See Also. Still includes the marker so the deterministic
# table (even if empty) substitutes cleanly.
THIN_TOPIC_PROMPT = """You are a knowledge wiki compiler. Create a **thin Topic** page — the
cluster has too few facts to justify the full topic template.

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary"}}

## Content structure (strict — only these three blocks, in order)
1. **TL;DR** — A single bold sentence summarizing the key insight. THIS MUST BE THE VERY FIRST LINE.
2. **Key Facts** — leave the single literal token `<<KEY_FACTS_TABLE>>` on its own line. The compiler will replace it with a deterministic table. Do NOT write the table yourself.
3. **Summary** — exactly 3 sentences giving context and significance.

## Rules
- Do NOT start with a # heading (title rendered separately).
- Do NOT include a mermaid diagram, chart, Open Questions section, See Also section, Contributors list, Decisions table, or Media section.
- Do NOT produce placeholder text like "No decisions recorded" — just omit.
- Add [N] citation markers on every factual claim. Maximum 3 citations per sentence.
- Do NOT use @, #, or $ prefixes — write names normally.
- Use ONLY inline [N] markers for citations. Do NOT emit `## Sources` or any reference list.
- Do NOT write the literal token `###CONTENT###`, `###SUMMARY###`, or `###END###` inside the body or summary text — those tokens are reserved structural markers.

## Topic data
Title: {title}
Summary: {summary}
Fact count: {fact_count}

Key facts: {key_facts_json}
All facts (for citation sourcing): {member_facts_json}
"""

PEOPLE_PROMPT = """You are a knowledge wiki compiler. Create a **People & Experts** page.

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary"}}

## Content structure (follow this order strictly — DIAGRAM FIRST, text after visuals)
1. **Contributor network** — ```mermaid diagram showing key people and their connections. Use relationship edges to show who collaborates with whom, who made which decisions, and expertise areas. THIS MUST BE THE VERY FIRST CONTENT ELEMENT.
2. **Activity chart** — ```chart bar chart showing contribution level per person
3. **Overview** — 1-2 sentences describing the contributor landscape in this channel (AFTER the diagram and chart)
4. **Contributors table** — GFM table with columns: Name, Role/Expertise, Topics Active In, Key Contributions, Decisions Made. In the "Topics Active In" column use `[[Topic Title]]` wikilinks. **ONLY reference topic titles from the `compiled_topic_titles` list below — any other title has no page and will appear as a red broken link.**
5. **Profiles** — for each top contributor (max 8), a `### <Name>` sub-section containing FOUR labelled bullets: **Owns** (systems/projects/docs they own), **Decides** (decisions they have made), **Contributes** (artifacts/RFCs/contributions), **Topic activity** (per-topic counts as a one-line list, e.g. "GPU Procurement: 12 · AI Solutions: 3 · Confidential Containers: 1"). Each bullet should answer the reader's question "what does this person OWN, DECIDE, and CONTRIBUTE?" — not "what facts mention them". If a person has nothing in a category, omit that bullet (do not pad). Wikilink topic names with `[[Topic Title]]` so navigation lands on the right Topic page. **ONLY use topic titles from the `compiled_topic_titles` list below — every other topic was dropped by the threshold gate and has no page; using it produces a red broken link.**
6. **Collaboration patterns** — bullet points on notable collaboration patterns, expertise clusters, and knowledge areas

## Writing style
- Describe what each person DOES and KNOWS — not what they "shared" or "posted". Write "Thomas Chong drives architecture decisions for Beever Atlas [1]" — NOT "Thomas Chong shared several messages about architecture [1]".
- In the Contributors table, "Key Contributions" should describe impact (e.g., "Designed the memory hierarchy and led database selection") — not activity (e.g., "Shared 14 messages across 8 topics").
- FORBIDDEN phrases: "shared a link", "posted about", "mentioned that". AVOID filler: "crucial for", "under discussion", "actively testing", "plays a key role in".

## Adaptive instructions
- Use "Contributors" and "Experts" language rather than "Team members" — this works for open communities, research groups, and enterprise teams alike
- The mermaid diagram should show actual relationships from the edge data: who DECIDED what, who WORKS_ON what, who USES which tools
- If the channel has few people (1-3), keep the diagram simple. For larger groups (5+), focus on the most active contributors.

## Rules
- Do NOT start with a # heading
- Each numbered section above MUST be a ## heading (e.g. `## Contributor Network`, `## Contributors Table`). Use ### for sub-sections. This creates a navigable table of contents.
- MUST include a ```mermaid diagram. Keep syntax SIMPLE — use ONLY `graph TD` with `ID[Label] --> ID[Label]` edges. Every node MUST use a short ID with a descriptive label: `TC[Thomas Chong]` not just `Thomas Chong`. FORBIDDEN: subgraph, end, style, classDef, parentheses inside brackets, quotes inside labels, `-- text -->` dash-space style labels, semicolons, chained arrows like `A --> B --> C` (use separate lines). USE `-->|label|` pipe-style to label edges with relationship type (e.g., `A -->|uses| B`, `A -->|decided| B`).
- Use GFM tables, not prose paragraphs, for listing people
- Add [N] citation markers on factual claims. **Maximum 3 citations per sentence** — never list long chains like [1][2][5][6][8][9][10]. Pick the 2-3 most relevant citations only.
- **CRITICAL: Each bullet point may have at most 3 citation markers total.** A person entry like "Thomas Chong: drives architecture [1][2][3]" is correct. "Thomas Chong: drives architecture [1][2][5][6][8][9][10]..." is FORBIDDEN.
- Do NOT use @, #, $ prefixes — write names normally
- Activity chart JSON (use ```chart code block, NOT ```json): `{{"type":"bar","title":"Contributor Activity","data":[{{"name":"Alice","contributions":15}}],"xKey":"name","series":["contributions"]}}`
- Use ONLY inline [N] markers for citations. Do NOT generate any source list, reference section, citation block, or numbered bibliography — the UI renders citations separately. FORBIDDEN at end of content: `## Sources`, `### Sources`, `- [1] @Author...`, `[1]: Author...`.
- **Maximum 10 edges** in the contributor network mermaid diagram. Show only the most significant collaboration relationships.

## Data
People (with relationship edges): {persons_json}
Contributor context: {top_people_json}
Relationship edges (from knowledge graph): {relationship_edges_json}

Compiled topic titles (the ONLY valid targets for `[[Topic Title]]` wikilinks — any other title was dropped by the threshold gate and has no page; pointing at it produces a red broken link): {compiled_topic_titles_json}
"""

DECISIONS_PROMPT = """You are a knowledge wiki compiler. Create a **Decisions** page.

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary"}}

## Content structure (follow this order strictly — DIAGRAM FIRST, text after visuals)
1. **Decision flow** — ```mermaid flowchart showing decision relationships and supersession chains. Show active decisions in a different style than superseded ones. THIS MUST BE THE VERY FIRST CONTENT ELEMENT.
2. **Decision timeline** — GFM table with columns: Date, Decision, Status, Made By, Context, Supersedes
3. **Summary** — 1-2 sentences on the decision landscape: how many decisions, how many active vs. superseded (AFTER the diagram and table)
4. **Impact analysis** — bullet points on what each active decision affects and its significance

## Writing style
- Focus on the DECISION and its RATIONALE — not who proposed it. Write "The team chose Supabase for its real-time capabilities, replacing the initial Postgres plan [1]" — NOT "Thomas Chong suggested using Supabase [1]".
- In the Impact analysis, explain consequences: what changed, what was enabled, what risks remain.
- FORBIDDEN phrases: "shared a link", "posted about", "mentioned that". AVOID filler: "crucial for", "under discussion", "actively testing", "plays a key role in".

## Adaptive instructions
- "Decisions" applies broadly: technical architecture choices, community governance decisions, research methodology selections, project direction changes, policy updates
- If no decisions exist, produce a brief note: "No formal decisions have been recorded in this channel yet." with a simple placeholder diagram
- If there are supersession chains, make them visually clear in the mermaid diagram
- Include context/rationale for each decision where available

## Rules
- Do NOT start with a # heading
- Each numbered section above MUST be a ## heading (e.g. `## Decision Flow`, `## Decision Timeline`). Use ### for sub-sections. This creates a navigable table of contents.
- MUST include a ```mermaid flowchart. Keep syntax SIMPLE — use ONLY `graph TD` with `ID[Label] --> ID[Label]` edges. Every node MUST use a short ID with a descriptive label. FORBIDDEN: subgraph, end, style, classDef, parentheses inside brackets, quotes inside labels, `-- text -->` dash-space style labels, semicolons, chained arrows like `A --> B --> C` (use separate lines). USE `-->|label|` pipe-style to label edges with relationship type (e.g., `A -->|uses| B`, `A -->|decided| B`).
- Status badges: ✅ active, ❌ superseded, ⏳ pending
- Use tables for the timeline, not paragraphs
- Add [N] citation markers on factual claims. **Maximum 3 citations per sentence** — never list long chains.
- Do NOT use @, #, $ prefixes
- Use ONLY inline [N] markers for citations. Do NOT generate any source list, reference section, citation block, or numbered bibliography — the UI renders citations separately. FORBIDDEN at end of content: `## Sources`, `### Sources`, `- [1] @Author...`, `[1]: Author...`.

## Data
Decisions (with supersession chains): {decisions_json}
Decision context: {top_decisions_json}
"""

ACTIVITY_PROMPT = """You are a knowledge wiki compiler. Create a **Recent Activity** page.

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary"}}

## Content structure (follow this order strictly — DIAGRAM FIRST, text after visuals)
1. **Activity chart** — ```chart area chart showing knowledge captured per day over the last 7 days. THIS MUST BE THE VERY FIRST CONTENT ELEMENT.
2. **Summary** — 1-2 sentences on recent activity: what happened in the last 7 days, key highlights (AFTER the chart)
3. **Daily breakdown** — for each day with activity, a section with:
   - Date as ### heading
   - Bullet list of key facts, decisions, and contributions added
   - Any media shared that day (embed images, link to docs)
4. **Highlights** — if there are standout events (major decisions, new topics, significant media), call them out

## Writing style
- Describe what HAPPENED and what it MEANS — not who posted. Write "A new memory hierarchy design was proposed, introducing 3-tier storage with hybrid retrieval [1]" — NOT "Thomas Chong shared an image about memory hierarchy [1]".
- Group related activity into coherent narratives per day rather than listing individual messages.
- FORBIDDEN phrases: "shared a link", "shared an article", "posted about", "mentioned that".

## Adaptive instructions
- Activity means different things in different channels: code discussions, community events, research findings, project updates. Adapt language accordingly.
- If no recent activity exists, produce a brief note: "No activity recorded in the last 7 days." Skip the chart.
- If media was shared recently, embed or link it in the daily breakdown
- Group related facts within each day for readability

## Rules
- Do NOT start with a # heading
- Each numbered section above MUST be a ## heading (e.g. `## Activity Chart`, `## Daily Breakdown`). Use ### for sub-sections (e.g. each day as ### heading). This creates a navigable table of contents.
- Activity chart JSON: {{"type":"area","title":"Knowledge Growth","data":[{{"date":"Apr 01","facts":5,"decisions":1}}],"xKey":"date","series":["facts","decisions"]}}
- Use bullet points, not paragraphs
- Add [N] citation markers where applicable. **Maximum 3 citations per sentence.**
- Do NOT use @, #, $ prefixes
- If no recent activity, just say so briefly (no empty charts)
- Use ONLY inline [N] markers for citations. Do NOT generate any source list, reference section, citation block, or numbered bibliography — the UI renders citations separately. FORBIDDEN at end of content: `## Sources`, `### Sources`, `- [1] @Author...`, `[1]: Author...`.

## Data
Recent facts (last 7 days): {recent_facts_json}
Activity summary: {recent_activity_json}
Media shared recently: {recent_media_json}
"""

FAQ_PROMPT = """You are a knowledge wiki compiler. Create a **FAQ** (Frequently Asked Questions) page.

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary"}}

## Content structure (follow this order strictly — DIAGRAM FIRST, text after visuals)
1. **Topic distribution** — if FAQs come from 3+ topics, include a ```chart donut chart showing how many FAQs per topic. THIS MUST BE THE VERY FIRST CONTENT ELEMENT (skip if fewer than 3 topics — start with Q&A sections instead).
2. **Introduction** — 1 sentence: "Common questions and answers that have emerged from discussions in this channel." (AFTER the chart)
3. **Q&A sections** — group questions by topic. For each topic group:
   - Use ## heading with topic name
   - List EVERY Q&A from that topic's candidates — do NOT skip or reduce. If the data has 5 questions for a topic, include all 5.
   - Format each Q&A as a distinct block:

     **Q: [question text]**

     A: [answer text] [N]

   - After the last Q&A in a topic group, add a `---` horizontal rule to visually separate it from the next section.
4. **Related pages** — bullet list suggesting which wiki pages have more detail on each topic

## Writing style
- Each answer MUST be 2-3 sentences providing actionable context, not a restatement of the source. Bad: "MCP is a protocol proposed by Alvin Yu [1]." Good: "MCP (Multi-Agent Communication Protocol) standardizes how AI agents call Beever Atlas capabilities through a unified interface. It separates tool invocation (MCP) from guidance/instructions (Skills), enabling agents to interact with the system without custom integrations [1]."
- Answers should help someone UNDERSTAND the topic, not just confirm it exists.
- **Verbatim reuse**: if a candidate question already has an `answer` field containing 2+ sentences with citation markers, reuse that answer **verbatim** (preserve its wording and `[N]` citations). Only rewrite when the candidate answer is <2 sentences, lacks citations, or is clearly malformed.
- Each Q&A pair must be visually separated with a blank line before and after the answer — never run Q&A pairs together in a single paragraph.
- FORBIDDEN: 1-sentence answers that merely restate who said what.

## Adaptive instructions
- These Q&A pairs were extracted from actual channel discussions — they represent real questions people asked and answers that emerged
- Include ALL provided FAQ candidates. Only deduplicate if two questions from different topics are nearly identical (>90% overlap in meaning) — in that case merge and cite both sources.
- If no FAQ candidates exist at all, produce: "No frequently asked questions have emerged from channel discussions yet. As more conversations happen, common questions and their answers will appear here."
- Order questions within each topic by relevance/importance, not chronologically

## Rules
- Do NOT start with a # heading (title rendered separately)
- Each topic group MUST be a ## heading. Individual Q&A pairs use bold **Q:** formatting on their own line, with the answer on a separate paragraph. Do NOT use ### subheadings for individual Q&As. This creates a navigable table of contents.
- Use ```chart for the topic distribution with JSON: {{"type":"donut","title":"FAQ by Topic","data":[{{"name":"Topic A","value":3}}],"xKey":"name","series":["value"]}}
- Add [N] citation markers on answers to trace back to source discussions. **Maximum 3 citations per answer.**
- Do NOT use @, #, $ prefixes — write names normally
- Keep answers concise but complete — 2-3 sentences each
- Place a `---` horizontal rule after each topic group's last Q&A (before the next ## heading or the Related pages section). Do NOT place `---` between individual Q&A pairs within the same section.
- Use ONLY inline [N] markers for citations. Do NOT generate any source list, reference section, citation block, or numbered bibliography — the UI renders citations separately. FORBIDDEN at end of content: `## Sources`, `### Sources`, `- [1] @Author...`, `[1]: Author...`.

## Output schema example (follow this shape EXACTLY)
The body — after the optional chart + intro — looks like this. Note the
``**Q:`` prefix on every question and the absence of ``### `` headings:

```
## Authentication & Access
**Q: What auth method does the bot use?**

A: The bot authenticates via OAuth tokens issued per workspace, stored
encrypted in the credentials store [1]. Tokens are refreshed automatically
when within 24h of expiry [2].

**Q: How do I rotate the bridge API key?**

A: Run `make rotate-bridge-key` and update `BRIDGE_API_KEY` in your `.env`
file. The bot picks up the new value on its next startup cycle [3].

---

## Deployment
**Q: Which database does the wiki cache live in?**

A: MongoDB collection ``wiki_pages`` keyed by ``(channel_id, page_id)``.
The legacy ``wiki_cache`` doc is kept as a dual-read fallback [1].
```

FORBIDDEN shapes (the parser cannot recover these as cards):
* ``### What is the auth method?`` (h3-as-question)
* ``**What is the auth method?**`` (bold-without-Q-prefix)
* ``Q: ... A: ...`` on a single line

## Data
FAQ candidates (grouped by topic): {faq_candidates_json}
Topic names for reference: {topic_names_json}
"""

GLOSSARY_PROMPT = """You are a knowledge wiki compiler. Create a **Glossary** page.

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary"}}

## Content structure (follow this order strictly)
1. **Relationship diagram** — if 5+ terms exist, include a ```mermaid diagram showing how terms relate to each other (which terms are used together, which are sub-concepts of others). THIS MUST BE THE VERY FIRST CONTENT ELEMENT (skip if fewer than 5 terms — start with Introduction instead).
2. **Introduction** — 1 sentence: "Key terms, acronyms, and concepts used in this channel."
3. **Terms table** — GFM table with columns: Term, Definition, First Mentioned By, Related Topics. Sort alphabetically. In the "First Mentioned By" column, write "—" when the source is unknown; NEVER write placeholder markers like `(Implicit)`, `(Inferred)`, `(Unknown)`, `(N/A)`, or any parenthesized guess. The Related Topics column MUST contain wikilinks: `[[Topic Title]]` for each related topic so navigation lands on the right Topic page. **ONLY reference topic titles from the `compiled_topic_titles` list below — every other topic was dropped by the threshold gate and has no page; pointing at it produces a red broken link.** If none of the compiled topics relate to a term, write `—` in that cell.
4. **Term details** — for each significant non-people entity (max 12; products, systems, services, concepts), a `### <Term>` sub-section. Each contains: a 1–2 sentence channel-context definition (NOT a generic Wikipedia gloss), a "Used in" line listing the Topic pages where the term shows up as `[[Topic Title]]` wikilinks, and an optional "See also" line with cross-references to other glossary terms. **Same constraint: ONLY use titles from `compiled_topic_titles` for `[[...]]` references.** This section absorbs the per-entity profile work the deprecated `entity:<thing>` page kind used to do — channel readers and agents land here for "what is X in this channel".
5. **Category breakdown** — if terms naturally group into categories (e.g., technical terms, process terms, domain terms), add a brief categorized list after the table

## Writing style
- Define each term in the context of THIS CHANNEL, not as a generic dictionary entry. Bad: "Neo4j is a graph database management system." Good: "Neo4j is used as the primary knowledge graph store for Beever Atlas, storing entity relationships extracted from channel conversations."
- Every definition should answer: "What is this AND how does this channel use it?"
- FORBIDDEN: Generic definitions that could come from Wikipedia. Always tie back to channel context.

## Adaptive instructions
- Glossary terms can be anything channel-specific: technical jargon, project codenames, acronyms, community slang, research terminology, business terms
- Enrich definitions where the provided data is thin — add context about how the term is used in this channel specifically
- If no glossary terms exist, produce: "No channel-specific terms have been identified yet. As more specialized vocabulary emerges in discussions, it will be cataloged here."
- Cross-reference related topics where possible

## Rules
- Do NOT start with a # heading (title rendered separately)
- Each numbered section above MUST be a ## heading (e.g. `## Terms`, `## Relationship Diagram`). Use ### for sub-sections or categories. This creates a navigable table of contents.
- Use GFM tables for the main term list — this is the primary content
- Use ```mermaid for the relationship diagram. Keep syntax SIMPLE — use ONLY `graph TD` with `ID[Label] --> ID[Label]` edges. Every node MUST use a short ID with a descriptive label. FORBIDDEN: subgraph, end, style, classDef, parentheses inside brackets, quotes inside labels, `-- text -->` dash-space style labels, semicolons, chained arrows like `A --> B --> C` (use separate lines). USE `-->|label|` pipe-style to label edges with relationship type (e.g., `A -->|uses| B`, `A -->|decided| B`).
- Do NOT use @, #, $ prefixes — write names normally
- Use ONLY inline [N] markers for citations. Do NOT generate any source list, reference section, citation block, or numbered bibliography — the UI renders citations separately. FORBIDDEN at end of content: `## Sources`, `### Sources`, `- [1] @Author...`, `[1]: Author...`.

## Data
Glossary terms: {glossary_terms_json}
Channel context: {channel_description}
Compiled topic titles (the ONLY valid targets for `[[Topic Title]]` wikilinks — any other title was dropped by the threshold gate and has no page): {compiled_topic_titles_json}
"""

RESOURCES_PROMPT = """You are a knowledge wiki compiler. Create a **Resources & Media** page cataloging all shared files, images, documents, and links.

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary"}}

## Content structure (follow this order strictly — DIAGRAM FIRST, text after visuals)
1. **Media distribution** — ```chart donut chart showing the count of each media type (images, documents, links, videos). THIS MUST BE THE VERY FIRST CONTENT ELEMENT.
2. **Resources table** — GFM table with columns: Name, Type, Shared By, Context, Link — show only the most useful items (max 40 rows), prioritized by relevance and diversity of sources
3. **Overview** — 1-2 sentences summarizing what resources have been shared in this channel (AFTER chart and table)
4. **Images** — if images exist, show only top 10. Each image MUST have a brief description line BEFORE the embed explaining what the image shows and why it's useful, then the embed on a new line. Format:\n   `**Beever Atlas Insights Dashboard** — Shows key metrics including total memories, queries, and cost savings for the project.`\n   `![Beever Atlas Insights Dashboard](url)`\n   Skip if none.
5. **Documents** — if PDFs/docs exist, show only top 10. Each document MUST have a brief description explaining what information it contains and how it helps, followed by the link. Format:\n   `**PIVOT_PLAN.md** — Strategic pivot plan outlining the transition from legacy to wiki-first architecture. [Download](url)`\n   Skip if none.
6. **Links** — if external links exist, show only top 20, grouped thematically. Each link MUST have a brief description. Format:\n   `**Context Graphs vs Knowledge Graphs** — Explains how context graphs preserve decision context for AI agents, a key architectural reference. [Read article](url)`\n   Skip if none.
7. **Videos** — if videos exist, show with brief description and link. Skip if none.

## Writing style
- In the Context column and section descriptions, explain WHY a resource matters — not just that it was shared. Write "Architecture reference for the 3-tier memory hierarchy design" — NOT "Thomas Chong shared this image."
- Group related resources thematically (e.g., "Memory System Research", "Project Mockups") rather than just by media type.
- FORBIDDEN phrases: "shared a link", "shared an article", "posted about".

## Adaptive instructions
- Group resources by type for easy scanning
- Include the context in which each resource was shared — what was the discussion about when this was posted?
- For images, always embed with ![desc](url) so they render inline
- For documents and links, use [name](url) format
- If a resource was referenced by multiple people or in multiple contexts, note that

## Rules
- Do NOT start with a # heading (title rendered separately)
- Each numbered section above MUST be a ## heading. Use ### for sub-sections. This creates a navigable table of contents.
- Use ```chart for the distribution with JSON: {{"type":"donut","title":"Resources by Type","data":[{{"name":"Images","value":5}}],"xKey":"name","series":["value"]}}
- Use GFM tables for the overview listing
- Add [N] citation markers where applicable. **Maximum 3 citations per sentence.**
- Do NOT use @, #, $ prefixes — write names normally
- Remove obvious low-signal/noise links and malformed/truncated duplicates (e.g. same URL/title repeated with ellipsis variants).
- Ensure type is correct: markdown docs are documents/files, not images.
- Use ONLY inline [N] markers for citations. Do NOT generate any source list, reference section, citation block, or numbered bibliography — the UI renders citations separately. FORBIDDEN at end of content: `## Sources`, `### Sources`, `- [1] @Author...`, `[1]: Author...`.
- **Maximum 30 items total** across all sections. Prioritize items with descriptive names and clear context.
- **Group related links** from the same author and domain. Show the most representative one with a note like "(+N related posts)" instead of listing each individually.

## Data
Media items: {media_json}
Total media count: {media_count}
"""

TOPIC_ANALYSIS_PROMPT = """You are a knowledge wiki architect. Analyze whether this topic cluster should be split into sub-pages.

Return JSON exactly: {{"needs_subpages": true/false, "subpages": [{{"title": "...", "fact_indices": [0, 1, 2], "summary": "..."}}]}}

## Rules
- **Size-based bias**: Readers cannot navigate a 40+ row Key Facts table. If `fact_count` ≥ 40, you MUST return `needs_subpages: true` and partition into 2–5 sub-pages even if the theme feels unified — there will always be sub-themes worth separating (by entity, by phase, by platform, by decision vs. discussion, etc.).
- For clusters with 15–39 facts, recommend splitting ONLY if there are clearly distinct sub-themes (at least 2 sub-pages, each with 5+ facts); return `needs_subpages: false` otherwise.
- When splitting, every fact must be assigned to exactly one sub-page (no gaps, no overlaps). Each fact_indices entry is a 0-based index into the facts list below.
- Sub-page titles should be concise (3-6 words) and descriptive.
- Maximum 5 sub-pages. Each sub-page must have 5+ facts.

## Topic
Title: {title}
Summary: {summary}
Fact count: {fact_count}

## Facts (indexed from 0)
{indexed_facts_json}
"""

SUBTOPIC_PROMPT = """You are a knowledge wiki compiler. Create a **Sub-Topic** page — a focused deep-dive into one aspect of a larger topic.

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary"}}

## Content structure (follow this order strictly — TL;DR FIRST, then PARENT CONTEXT, then DIAGRAM, then text)
1. **TL;DR** — A single bold sentence summarizing the key insight of this sub-topic. THIS MUST BE THE VERY FIRST LINE.
2. **Parent context** — A SHORT paragraph (1-2 sentences) anchored to the parent topic via `[[Parent Title]]` wikilink. Use the `parent_title` data block as the wikilink target. Example: `This sub-topic deep-dives into one slice of [[Parent Title]] — specifically <what this slice covers>.` **ONLY emit `[[Title]]` wikilinks for titles in the `compiled_topic_titles_json` list below — any other title was dropped by the threshold gate and has no page; pointing at it produces a red broken link.** When the `parent_title` itself is not in that list, write the parent name as plain text instead of a wikilink.
3. **Sub-topic scope** — A short paragraph stating what this sub-topic covers vs. its siblings. Be explicit about boundaries: what's IN scope, what's OUT (covered by sibling sub-topics or the parent). Example: `Scope: <X>. Out of scope (covered elsewhere): <Y> (parent), <Z> (sibling sub-topic).`
4. **Concept diagram** — ```mermaid diagram showing key entities and relationships within this sub-topic.
5. **Key Facts** — GFM table with columns: Fact, Source, Type, Importance — the most important facts with [N] citations
6. **Overview** — 2-3 sentences on what this sub-topic covers and how it relates to the parent topic (AFTER diagram and table)
7. **Details** — bullet points expanding on the key facts, decisions, and context
8. **Contributors** — bullet list of people involved, if relevant. Skip if not meaningful.

## Writing style
- **Synthesize, don't narrate.** State insights and conclusions directly. Write "Agents fail primarily due to inadequate memory, not limited context [1]" — NOT "Jacky Chan shared an article saying agents fail due to memory [1]".
- FORBIDDEN phrases: "shared a link", "shared an article", "posted about", "mentioned that", "noted that", "presented a".
- In the Key Facts table, state the fact itself — not "Person X observed that [fact]".

## Rules
- Do NOT start with a # heading (title rendered separately)
- Each numbered section above MUST be a ## heading. Use ### for sub-sections.
- ALWAYS include at least one ```mermaid diagram. Keep syntax SIMPLE — use ONLY `graph TD` with `ID[Label] --> ID[Label]` edges. Every node MUST use a short ID with a descriptive label. FORBIDDEN: subgraph, end, style, classDef, parentheses inside brackets, quotes inside labels, `-- text -->` dash-space style labels, semicolons, chained arrows like `A --> B --> C` (use separate lines). USE `-->|label|` pipe-style to label edges with relationship type (e.g., `A -->|uses| B`, `A -->|decided| B`).
- Use ```chart for quantitative data with JSON: {{"type":"bar","title":"...","data":[...],"xKey":"name","series":["value"]}}
- Add [N] citation markers on every factual claim. **Maximum 3 citations per sentence.**
- Do NOT use @, #, $ prefixes — write names normally
- If media exists, embed: ![desc](url) for images, [name](url) for docs/links
- Use ONLY inline [N] markers for citations. Do NOT generate any source list, reference section, citation block, or numbered bibliography — the UI renders citations separately. FORBIDDEN at end of content: `## Sources`, `### Sources`, `- [1] @Author...`, `[1]: Author...`.
- **Thin-data rule**: If `fact_count` ≤ 8, use CONDENSED format: (1) TL;DR bold sentence, (2) Key Facts table (max 5 rows), (3) Summary paragraph. SKIP concept diagram, Details, and Contributors. Do NOT produce placeholder text for empty sections — just omit them.

## Context
Parent topic: {parent_title}
Sub-topic title: {title}
Sub-topic summary: {summary}
Fact count: {fact_count}

## Facts (for this sub-topic only)
{member_facts_json}
Media: {media_json}

Compiled topic titles (the ONLY valid targets for `[[Title]]` wikilinks — any other title was dropped by the threshold gate and has no page): {compiled_topic_titles_json}
"""

# Phase 4 v2 variant of SUBTOPIC_PROMPT.
# wiki-redesign-gap-fill / Group 6 — Key Facts is now item 5 (not 3) since
# Parent context + Sub-topic scope were inserted ahead of the diagram.
SUBTOPIC_PROMPT_V2 = (
    SUBTOPIC_PROMPT.replace(
        "5. **Key Facts** — GFM table with columns: Fact, Source, Type, Importance — the most important facts with [N] citations",
        _KEY_FACTS_MARKER_INSTRUCTION,
    )
    + "\n\n"
    + _NO_MARKER_ECHO_INSTRUCTION
    + "\n"
)


# ---------------------------------------------------------------------------
# llm-wiki-folder-structure — Structure Planner Prompt
# ---------------------------------------------------------------------------

# Single channel-wide LLM call. Receives the channel summary, a
# condensed cluster index, and the heuristic candidate groups; returns
# a JSON tree describing which clusters become folders, which become
# leaves, and what each folder's title + slug should be.
#
# Hard constraints stated in the prompt itself:
#   - Every cluster id from the input MUST appear exactly once in the
#     output (either inside a folder's child_slugs OR in leaves).
#   - Folder slugs MUST be kebab-case ASCII and MUST NOT collide with
#     any cluster id.
#   - Maximum tree depth is 4. Prefer depth 2-3 unless a strong
#     justification exists for going deeper.
#   - Confirm/refine the candidate groups; you may reject candidates
#     or invent new groupings, but bias toward refinement.
#   - Output JSON ONLY — no markdown fences, no commentary.
STRUCTURE_PLANNER_PROMPT = """You are an information architect organizing a knowledge wiki for a chat channel.

The wiki has many topic pages. Your job is to decide how to **group them into folders** so the operator can navigate efficiently. A folder is a first-class wiki page with its own synthesized index AND a list of child pages (sub-folders or leaf topics).

## Inputs

### Channel narrative
{channel_summary}

### Topic clusters (you MUST place each exactly once in your output)
{cluster_index_json}

### Heuristic candidate groups (deterministic clusters discovered from prefix similarity, entity overlap, and co-citation density)
{candidates_json}

## Your task

Produce a JSON object with this exact shape:

{{
  "folders": [
    {{
      "slug": "kebab-case-folder-id",
      "title": "Human Folder Name",
      "child_slugs": ["cluster-id-1", "cluster-id-2"],
      "rationale": "1-line explanation of why these belong together"
    }}
  ],
  "leaves": ["cluster-id-3", "cluster-id-4"]
}}

## Hard rules

1. **Every cluster id from the input list MUST appear exactly once** — either inside one folder's `child_slugs`, OR in `leaves`. Never both. Never neither.
2. **Folder slugs MUST be kebab-case ASCII** (e.g., `beever-atlas`, `security`, `growth-marketing`). NO spaces, NO uppercase, NO underscores.
3. **Folder slugs MUST NOT collide with any cluster id.** Check before naming.
4. **Maximum depth = 4.** A folder may contain other folders, but the deepest nested folder may have leaves at most 4 levels from the root.
5. **Default to depth 2.** Only nest a folder inside another folder when the inner group is genuinely a sub-domain (e.g., "GitHub" inside "Beever Atlas").
6. **Use the candidate groups as a strong prior.** Confirm them, rename them, expand them, or split them — but don't invent radically different groupings unless the candidates clearly miss a pattern.
7. **Output JSON ONLY.** No markdown code fences. No leading or trailing prose. Just the JSON object.

## Quality bar — be assertive about grouping

- **AIM TO PUT EVERY TOPIC IN A FOLDER.** A topic should only land in `leaves` (root-level) when it truly fits no group — not when it's merely the only one in a small bucket.
- A folder of **2** topics is fine. A folder of 1 is not (singletons stay loose).
- Loose, broad categories are valid and encouraged: `admin`, `tooling`, `external-partnerships`, `events`, `engineering-misc`, `meeting-notes`, `legal-and-licensing`, `funding-and-grants`. Don't be afraid to invent these — operators prefer "everything has a home" over "20 unassigned topics at root."
- Folder titles read like Notion section names: short, scannable, normally capitalized ("Security", "Growth Campaigns", "Admin").
- Prefer **2-level depth** (root folder containing topics) for the bulk of the tree. Nest a 3rd level (folder → folder → topic) only when an inner sub-domain is clearly distinct.
- A wiki with **5-8 folders covering 80%+ of topics** is the target. A wiki with 3 folders + 12 unassigned root topics is too sparse — be more assertive.

Return the JSON now.
"""


# ---------------------------------------------------------------------------
# llm-wiki-folder-structure — Folder Index Prompt
# ---------------------------------------------------------------------------

# Synthesizes a folder's landing page. Receives the folder's title, the
# title + 1-line summary of every direct child page, an aggregated set
# of key entities across descendants, and a handful of top-quality
# facts. Returns a 200-400 word landing page that explains what's in
# the folder and where to look for what.
#
# Critical contract: the output MUST contain the literal token
# ``<<CHILDREN_TOC>>`` on its own line. The renderer replaces it with
# a deterministic auto-TOC of children — that way operators always see
# the actual children even if the LLM drifts from the prompt.
# ---------------------------------------------------------------------------
# adaptive-wiki-page-content — unified single-call prompt
# ---------------------------------------------------------------------------
#
# The catalog selection + module rendering + body authorship happen in
# ONE LLM call. The validator runs post-response and drops modules
# whose criteria fail; the deterministic renderers fill marker
# substitutions. Cost stays at one call per topic page (same as the
# legacy monolithic prompt) but the structured plan + selection rules
# force the model to think about page shape before writing.

MODULE_COMPILE_PROMPT = """You are an adaptive wiki page compiler. In ONE response, decide which content modules best fit this topic's data shape AND write the page body that uses them.

## Module catalog (selection rules)

{module_catalog_block}

## Topic signals (use these to decide module eligibility)

{signals_json}

## Topic data (for prose context — DO NOT re-emit structured data; the renderer fills markers)

Title: {title}
Summary: {summary}
Key facts (top 8): {top_facts_json}
Key contributors: {top_people_json}
Active period: {date_range_start} – {date_range_end}

## Output

Return JSON ONLY with this exact shape:

{{
  "plan": {{
    "modules": [
      {{ "id": "<module_id>", "anchor": "<short alphanumeric>" }},
      ...
    ],
    "media_pins": [
      {{ "media_id": "<id>", "fact_id": "<id>", "slot": "hero|inline|gallery" }}
    ]
  }},
  "tldr": "<single bold sentence — the key insight>",
  "overview": "<2-3 sentence prose framing what the topic is, why it matters, and current state>",
  "body": "<markdown body containing module markers + short connector prose>"
}}

## Module-selection rules

- **HARD RULE — Module #1 in your plan MUST be `hero_summary` unless `fact_count` is 0.** The hero_summary frontend module reads your `tldr` and `overview` to render the page header (bold TL;DR + summary + stat strip). Skipping it leaves the page without a header.
- **HARD RULE — When `archetype == "decision"`, module #2 in your plan MUST be `decision_banner`.** The page is centered on the decision; the banner spotlights it (decision text, decided_by, decided_at) above all other content. Skipping `decision_banner` on Decision-archetype pages buries the headline in a key_facts row.
- **HARD RULE — When `tension_count` ≥ 1, include `tension_callout` in your plan IMMEDIATELY after `hero_summary` (or after `decision_banner` on Decision-archetype pages).** The tension callout surfaces a contradicting position pair (one author recommends X, another flags X as concerning) — it is high-signal content the reader must see near the top, not buried below `key_facts`. On a Topic-archetype page the order is `hero_summary` → `tension_callout` → ... ; on a Decision-archetype page it is `hero_summary` → `decision_banner` → `tension_callout` → ... .
- **HARD RULE — When `numeric_fact_count` ≥ 3, place `stat_strip` IMMEDIATELY after `hero_summary` (module #2).** The numeric values ARE the headline for metric-heavy topics; surfacing them as cards before the prose lets the reader grasp the magnitude in one glance. (When BOTH `decision_banner` and `stat_strip` qualify, `decision_banner` wins module #2 and `stat_strip` becomes module #3 — decisions outrank metrics. When `tension_callout` is also present, place it before `stat_strip` — tensions outrank metrics.)
- **HARD RULE — The LAST module in your plan MUST be `provenance_drawer` whenever `fact_count` ≥ 1.** It exposes the source messages each fact came from with platform deep-links — both human readers and LLM agents reading the wiki rely on this drill-down. Place it AFTER `related_threads` (or at the very end when `related_threads` is absent).
- **HARD RULE — When `glossary_terms_used` ≥ 2, include `acronym_legend` near the END of the page (just before `provenance_drawer` when both are present).** Readers reach the legend after working through the prose; placing it at the top wastes header real estate.
- **HARD RULE — When `child_count` ≥ 1, include `subpage_cards` in your plan.** Place it AFTER the spine modules (`key_facts`, `decision_log`) and BEFORE `related_threads`. The page is a parent overview for sub-topic pages; readers need a card grid to navigate to them. (The validator suppresses singleton parents — `child_count == 1` is dropped post-plan as "use an inline link instead".)
- Pick **3 to 7 modules** total (excluding `hero_summary` and `provenance_drawer`, which are always present when their rules fire). Below 3 content modules the page reads as a stub; above 7 it reads as a kitchen sink.
- A module is ELIGIBLE only when its selection rule (in the catalog above) is satisfied. Do NOT pick a module whose rule is not met — the validator will drop it.
- Order modules by reading priority: hero_summary first, then stat_strip (when present), then facts/decisions, then supporting visuals, then related_threads, then acronym_legend, then provenance_drawer LAST.
- Anchors are lowercase + alphanumeric + dashes, ≤ 24 chars (e.g., `decision-log`, `related-threads`).
- For media: pick AT MOST ONE `media_hero` per page. Pin `media_inline` items to a fact_id. Pool unpinned media into `media_gallery`.

## Body authoring rules

- The body MUST contain one ``<<MODULE:<id>>>`` marker per module in your plan, in plan order. The compiler substitutes each marker with the module's deterministic content.
- Optionally include a 1-2 sentence connector paragraph BEFORE each marker (e.g. "Three decisions shaped the rollout schedule:" before `<<MODULE:decision_log>>`). Connectors are optional — skip when the module's heading is self-explanatory.
- For `media_inline:<media_id>` markers, place each marker IMMEDIATELY after the paragraph discussing the source fact for that media. The reader sees the visual alongside the prose that introduces it.
- Do NOT re-emit structured data the modules render (no decision tables, no fact tables, no quote blockquotes, no entity graphs in the body — emit the marker instead).
- Do NOT write a "Sources" or "References" section. The CitationPanel renders those.
- Do NOT use @ / # / $ entity prefixes — write names normally.

## Hard rules

- Output JSON ONLY. No markdown fences around the outer JSON. No prose before or after the outer object.
- TL;DR is exactly ONE sentence, bolded with `**…**`.
- Overview is 2-3 sentences. No more.
- Body word count ≤ 350 (excluding markers + connector prose). Connectors are tight one-liners; deep prose belongs inside the relevant module's data, not in the body.
- If you cannot satisfy any module's selection rule (extreme thin-data topic), pick `key_facts` only and write a minimal body containing only `<<MODULE:key_facts>>`.
"""


def build_module_compile_prompt(
    *,
    signals: dict,
    module_catalog: list[dict],
    title: str,
    summary: str,
    top_facts: list[dict],
    top_people: list[dict],
    date_range_start: str = "",
    date_range_end: str = "",
) -> str:
    """Render ``MODULE_COMPILE_PROMPT`` with the topic signals + catalog
    + page-level metadata. Single-call: planner + writer collapsed
    into one prompt so cost stays at 1 LLM call per topic page."""
    import json as _json

    catalog_lines: list[str] = []
    for entry in module_catalog:
        catalog_lines.append(
            f"- **{entry['id']}** ({entry['label']}) — {entry['description']} "
            f"_Rule:_ {entry['rule']}"
        )
    catalog_block = "\n".join(catalog_lines)
    return MODULE_COMPILE_PROMPT.format(
        module_catalog_block=catalog_block,
        signals_json=_json.dumps(signals, indent=2, default=str),
        title=title,
        summary=summary,
        top_facts_json=_json.dumps(top_facts[:8], indent=2, default=str),
        top_people_json=_json.dumps(top_people[:6], indent=2, default=str),
        date_range_start=date_range_start,
        date_range_end=date_range_end,
    )


# ---------------------------------------------------------------------------
# wiki-narrative-articles — v3 prompt with narrative_sections schema
# ---------------------------------------------------------------------------
#
# v3 extends v2 with a structured ``narrative_sections`` array in the
# output JSON. The LLM produces both the existing module plan AND a
# multi-section explanatory article in one response — cost stays at
# ONE LLM call per topic page (Decision 1 in the design doc).
#
# v2 is preserved unchanged because the orchestrator's flag-OFF path
# uses it; flipping the flag to OFF must roll back to byte-identical
# v2 behaviour with no orchestrator code change.

MODULE_COMPILE_PROMPT_V3 = """You are an adaptive wiki page compiler. In ONE response, decide which content modules best fit this topic's data shape AND write a multi-section article that EXPLAINS the topic, AND write the page body that uses module markers.

## Module catalog (selection rules)

{module_catalog_block}

## Topic signals (use these to decide module eligibility)

{signals_json}

## Topic data (for prose context — DO NOT re-emit structured data; the renderer fills markers)

Title: {title}
Summary: {summary}
Key facts (top 8): {top_facts_json}
Key contributors: {top_people_json}
Active period: {date_range_start} – {date_range_end}

{archetype_hint_block}

## Output

Return JSON ONLY with this exact shape:

{{
  "plan": {{
    "modules": [
      {{ "id": "<module_id>", "anchor": "<short alphanumeric>" }},
      ...
    ],
    "media_pins": [
      {{ "media_id": "<id>", "fact_id": "<id>", "slot": "hero|inline|gallery" }}
    ]
  }},
  "tldr": "<single bold sentence — the key insight>",
  "overview": "<2-3 sentence prose framing what the topic is, why it matters, and current state>",
  "narrative_sections": [
    {{
      "anchor": "<kebab-case>",
      "heading": "<Human readable section title>",
      "paragraphs": [
        {{
          "text": "<paragraph prose with [f_xxx] inline citations>",
          "citations": ["f_xxx", "f_yyy"],
          "is_inference": false
        }}
      ],
      "visual": null
    }}
  ],
  "body": "<markdown body containing module markers + short connector prose>"
}}

## Narrative-article rules (the new heart of the page)

The `narrative_sections` array is the new SPOTLIGHT of every page — a multi-section article that EXPLAINS the topic the way Wikipedia, DeepWiki, or Notion would. The existing modules become a Reference & Evidence appendix below.

**Section structure**:
- Each section has a unique `anchor` (kebab-case, ≤ 24 chars), a `heading` (human readable, sentence case), and a `paragraphs` array.
- Section titles MUST emerge from the cluster's actual content. Do NOT pick from a fixed template menu. Examples of good content-driven titles: "Integrate OpenClaw with Beever Atlas", "Mattermost as the primary platform", "Future BigQuery integration". Bad titles (template bias): "Context", "Overview", "Background" when the data does not support that framing.
- Sections render in the order you list them — put foundational / definitional sections first; forward-looking / open-questions sections last.
- Aim for 3-7 sections per page. 1-2 sections reads as a stub; 8+ sections reads as a kitchen sink.

**Paragraph structure**:
- Each paragraph is 3-5 sentences. Active voice. Short.
- `text`: the paragraph prose. State insights and conclusions directly — synthesise, don't narrate.
- `citations`: array of fact_ids cited inline within `text`. Format inline as `[f_xxx]` so the frontend renders citation chips.
- `is_inference`: set to `true` for SYNTHESIS paragraphs that interpret beyond direct facts (e.g., "These decisions together suggest a shift toward enterprise"). Inference paragraphs MUST still cite ≥1 fact_id grounding the inference — uncited inference is hallucination and will be dropped.

**Citation discipline (HARD RULES)**:
- EVERY paragraph MUST cite at least one fact_id. Uncited paragraphs are DROPPED by the validator.
- Inference paragraphs (`is_inference: true`) MUST still cite ≥1 fact_id.
- FORBIDDEN narration phrases (paragraphs containing these are DROPPED): "shared a link", "shared an article", "noted that", "mentioned that", "posted about", "presented that". Do NOT write activity-log narration.
- GOOD: "The team adopted Authlib for OAuth/OIDC because of its modern OIDC discovery [f_1]."
- BAD: "Thomas Chong shared a link to Authlib [f_1]."
- The article's overall citation coverage MUST be ≥ 80% — articles below that threshold are REJECTED and the page falls back to module-only rendering.

**Word caps**:
- Each section: 150-400 words. Sections over 400 words are TRUNCATED at sentence boundary.
- Total article: 1,500-3,000 words for typical topic pages, up to 5,000 for landmark pages (channel overview). The validator HARD-rejects articles over 6,000 words.
- Be concise like a Wikipedia editor — every paragraph adds new information. NO padding.

**Visual guidance per section (NOT optional when content fits)**:

When a section's content has any of these shapes, you MUST include the matching visual:

- **Comparison / options** (2+ alternatives, trade-offs, before/after) → `table` with headers
- **Sequence / process / workflow / pipeline** → `mermaid` `graph TD` or ordered `list`
- **Architecture / system relationships / data flow** → `mermaid` `graph TD` with labeled edges
- **Enumeration of 4+ items** (libraries, decisions, contributors, milestones) → bulleted `list`
- **Statement of importance / quote / direct claim worth highlighting** → `blockquote`
- **Code / config / command snippet** → `code` block

Default to including a visual — every section is more readable with one. Set `visual: null` ONLY when the section is purely interpretive prose (no comparison, sequence, architecture, enumeration, or quotable claim).

Allowed kinds: `table`, `mermaid`, `list`, `callout`, `code`, `blockquote`.

Visual shape:
- `{{"kind": "table", "content": {{"headers": ["A", "B"], "rows": [["1", "2"]]}}}}`
- `{{"kind": "mermaid", "content": "graph TD\\n  A[Foo] --> B[Bar]"}}`
- `{{"kind": "list", "content": {{"ordered": false, "items": ["one", "two", "three"]}}}}`
- `{{"kind": "callout", "content": {{"variant": "warning|info|tip", "text": "..."}}}}`
- `{{"kind": "code", "content": {{"language": "python", "code": "..."}}}}`
- `{{"kind": "blockquote", "content": {{"text": "...", "attribution": "Speaker"}}}}`

Visuals sit INLINE within the section, not at the article footer. At most ONE visual per section.

**Agent voice**:
- Third-person synthetic voice. Wikipedia-editor style. NO "I", "we", "our".
- Short paragraphs (3-5 sentences). Active voice. Active verbs name the outcome.
- No jargon without explanation. Define acronyms on first use.
- NO "shared a link" / "noted that" narration — see forbidden phrases above.
- Cite every claim with [f_xxx] inline.

## Worked examples

GOOD section (Topic archetype, content-driven title):
```
{{
  "anchor": "openclaw-integration",
  "heading": "Integrate OpenClaw with Beever Atlas",
  "paragraphs": [
    {{
      "text": "The team chose OpenClaw as the primary chat connector for Atlas, replacing the prior Mattermost-only adapter [f_12]. OpenClaw provides a unified push-source webhook layer covering Slack, Discord, and Teams in one HMAC-signed contract [f_15].",
      "citations": ["f_12", "f_15"],
      "is_inference": false
    }},
    {{
      "text": "This shift positions Atlas as a connector-agnostic memory layer; the IP shifts upward to the wiki + memory architecture, leaving connectors as commodity infrastructure [f_12].",
      "citations": ["f_12"],
      "is_inference": true
    }}
  ],
  "visual": null
}}
```

BAD section (template-bias, uncited paragraphs — DO NOT EMIT):
```
{{
  "anchor": "context",
  "heading": "Context",
  "paragraphs": [
    {{
      "text": "This topic discusses several important decisions about architecture and connectors.",
      "citations": [],
      "is_inference": false
    }}
  ]
}}
```
The bad example fails on three counts: (1) generic template title, (2) uncited paragraph (will be dropped), (3) vague filler that adds no information.

## Module-selection rules

- **HARD RULE — Module #1 in your plan MUST be `hero_summary` unless `fact_count` is 0.** The hero_summary frontend module reads your `tldr` and `overview` to render the page header (bold TL;DR + summary + stat strip).
- **HARD RULE — When `narrative_sections` has at least one validated section, include `narrative_article` IMMEDIATELY AFTER `hero_summary` (module #2).** The article is the new spotlight; existing modules become the appendix.
- **HARD RULE — When `archetype == "decision"`, include `decision_banner` after `narrative_article`.**
- **HARD RULE — When `tension_count` ≥ 1, include `tension_callout` after `decision_banner` (or after `narrative_article` on Topic-archetype pages).**
- **HARD RULE — When `numeric_fact_count` ≥ 3, place `stat_strip` near the top (after the spotlight modules).**
- **HARD RULE — The LAST module in your plan MUST be `provenance_drawer` whenever `fact_count` ≥ 1.** Place it AFTER `related_threads` (or at the very end).
- **HARD RULE — When `glossary_terms_used` ≥ 2, include `acronym_legend` near the END of the page (just before `provenance_drawer`).**
- **HARD RULE — When `child_count` ≥ 1, include `subpage_cards` AFTER the spine modules and BEFORE `related_threads`.**
- Pick **3 to 7 modules** total (excluding the always-present hero_summary + provenance_drawer + narrative_article).
- A module is ELIGIBLE only when its selection rule (in the catalog above) is satisfied.
- Anchors are lowercase + alphanumeric + dashes, ≤ 24 chars.

## Body authoring rules

- The body MUST contain one ``<<MODULE:<id>>>`` marker per module in your plan, in plan order.
- Optionally include a 1-2 sentence connector paragraph BEFORE each marker.
- Do NOT re-emit structured data the modules render.
- Do NOT write a "Sources" or "References" section.
- Do NOT use @ / # / $ entity prefixes — write names normally.

## Hard rules

- Output JSON ONLY. No markdown fences around the outer JSON. No prose before or after the outer object.
- TL;DR is exactly ONE sentence, bolded with `**…**`.
- Overview is 2-3 sentences. No more.
- Body word count ≤ 350 (excluding markers + connector prose). The narrative ARTICLE carries the deep prose; the body just glues markers together.
- If you cannot produce a quality narrative (extreme thin-data topic with < 4 facts), emit `narrative_sections: []` and rely on module-only rendering.
"""


# ---------------------------------------------------------------------------
# Archetype hint blocks — soft per-archetype section structure suggestions
# (Decision 2 in ``openspec/changes/wiki-narrative-articles/design.md``).
#
# These are SUGGESTIONS, not templates: every block explicitly tells the LLM
# to DEVIATE when the cluster's data does not fit the suggested structure.
# Topic archetype intentionally returns the empty string — sections come
# entirely from cluster content. Unknown archetypes also return the empty
# string defensively, so the orchestrator never raises on a new archetype.
# ---------------------------------------------------------------------------


_ARCHETYPE_HINT_DECISION = """## Section structure hint — Decision archetype

This page is centered on a single decision. Decision pages typically work
well with these sections, but DEVIATE when the data does not fit:

1. **Context** — what was the situation that motivated the decision?
2. **The decision** — what was decided, by whom, when (1-2 sentences)
3. **Why** — the rationale; cite the rationale fact_id
4. **Alternatives rejected** — what else was considered; cite each alternative
5. **Implications** — what changes downstream because of this decision
6. **Open consequences** — unresolved questions or effects to monitor

If your data doesn't support 6 sections, pick the 3-4 that fit. Do NOT
fabricate sections to fill the structure. Section titles should still
be content-driven (e.g., "Why Authlib over google-auth-oauthlib" beats
"Why").
"""


_ARCHETYPE_HINT_TENSION = """## Section structure hint — Tension archetype

This page surfaces an unresolved disagreement. Tension pages typically
have these sections:

1. **The disagreement** — what is contested (1-2 sentences framing)
2. **Position A** — first stance, citing supporting fact_ids
3. **Position B** — second stance, citing supporting fact_ids
4. **Common ground** — what both positions agree on (if any)
5. **What's required to resolve** — owners, blockers, decision path

Render Position A and Position B with EQUAL weight. Do NOT take a side
in the prose. Cite each position's supporting facts; let the reader
weigh evidence.
"""


_ARCHETYPE_HINT_FOLDER = """## Section structure hint — Folder archetype

This page indexes a folder's child pages. Folder pages typically have:

1. **What this area covers** — 1 paragraph framing the folder's scope
2. **Recent strategic shifts** — what changed in the last 2-4 weeks across
   descendant pages (cite descendant fact_ids)
3. **Major decisions** — top 3-5 decisions surfaced across descendants
4. **Cross-cutting tensions** — disagreements that span multiple sub-pages
5. **Where we're headed** — open questions / forward direction

Sections should synthesize ACROSS the descendant pages — not duplicate
each child's narrative. Reference children by their wiki link.
"""


_ARCHETYPE_HINT_CHANNEL_OVERVIEW = """## Section structure hint — Channel Overview archetype

This is the channel's landing page. Overview pages typically have 5-10
sections covering:

1. **What is X?** — 2-3 paragraphs introducing the project/topic the
   channel covers (cite the foundational facts)
2. **Architecture** — high-level structure if technical (use mermaid
   visual)
3. **Current priorities** — what the team is actively working on
4. **Recent decisions** — top 5 decisions in the last 30 days
5. **Active areas** — which folders/topics are seeing the most discussion
6. **Open questions** — top unresolved threads
7. **Roadmap** — known forward direction (gated on having forward-
   looking facts)

This is a LANDMARK page — word count cap is 5,000 words instead of the
default 3,000. Spend the budget on synthesis depth, not on listing
every fact.
"""


# Map archetype value (as produced by ``planner._derive_archetype``) to the
# corresponding hint block. Keys cover the four archetypes that get hints;
# Topic + unknown archetypes fall through to the empty string.
_ARCHETYPE_HINT_BLOCKS: dict[str, str] = {
    "decision": _ARCHETYPE_HINT_DECISION,
    "tension": _ARCHETYPE_HINT_TENSION,
    "folder": _ARCHETYPE_HINT_FOLDER,
    "channel_overview": _ARCHETYPE_HINT_CHANNEL_OVERVIEW,
    # Channel overview can also surface as ``"overview"`` in some
    # call-sites; alias both to the same block defensively.
    "overview": _ARCHETYPE_HINT_CHANNEL_OVERVIEW,
}


def get_archetype_hint_block(archetype: str) -> str:
    """Return the soft section-structure hint block for ``archetype``.

    Topic archetype (default) returns the empty string — sections come
    purely from cluster content (Decision 2 in the design doc). Unknown
    archetypes also return the empty string defensively so callers never
    raise on a freshly-added archetype.

    The returned blocks are *soft* hints: every block explicitly tells
    the LLM to DEVIATE when the data does not fit. They never enforce
    a rigid template.
    """
    if not archetype:
        return ""
    return _ARCHETYPE_HINT_BLOCKS.get(str(archetype).strip().lower(), "")


def build_module_compile_prompt_v3(
    *,
    signals: dict,
    module_catalog: list[dict],
    title: str,
    summary: str,
    top_facts: list[dict],
    top_people: list[dict],
    date_range_start: str = "",
    date_range_end: str = "",
    archetype_hint_block: str = "",
) -> str:
    """Render ``MODULE_COMPILE_PROMPT_V3`` with topic context + the
    optional archetype-hint block.

    Single-call: planner + writer + narrative author collapsed into
    one prompt so cost stays at 1 LLM call per topic page (Decision 1
    in ``openspec/changes/wiki-narrative-articles/design.md``).

    ``archetype_hint_block`` is an optional pre-rendered Markdown block
    (typically populated by Session C — Decision/Tension/Folder/Channel
    Overview archetypes) that nudges the LLM toward a typical section
    structure for that archetype. Topic archetype gets the empty
    string — sections come entirely from cluster content (Decision 2).
    """
    import json as _json

    catalog_lines: list[str] = []
    for entry in module_catalog:
        catalog_lines.append(
            f"- **{entry['id']}** ({entry['label']}) — {entry['description']} "
            f"_Rule:_ {entry['rule']}"
        )
    catalog_block = "\n".join(catalog_lines)
    return MODULE_COMPILE_PROMPT_V3.format(
        module_catalog_block=catalog_block,
        signals_json=_json.dumps(signals, indent=2, default=str),
        title=title,
        summary=summary,
        top_facts_json=_json.dumps(top_facts[:8], indent=2, default=str),
        top_people_json=_json.dumps(top_people[:6], indent=2, default=str),
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        archetype_hint_block=archetype_hint_block or "",
    )


MODULE_COMPILE_FOLDER_PROMPT = """You are an adaptive wiki page compiler. The page is a FOLDER INDEX — a wayfinding + dashboard layer over the descendant pages. In ONE response, decide which folder modules to render, author the bold TL;DR + 2-3 sentence summary that frame the folder, AND write a multi-section narrative article that synthesizes across the descendants.

## Module catalog (folder-archetype subset — selection rules)

{module_catalog_block}

## Folder signals (use these to decide module eligibility)

{signals_json}

## Folder data (for prose context — DO NOT re-emit the children list; the renderer handles it)

Folder title: {folder_title}
Direct children (in display order): {children_json}
Top contributors across descendants (pre-aggregated): {top_contributors_json}
Top decisions across descendants (pre-aggregated): {top_decisions_json}
Top facts across descendants (for synthesis + citation): {top_facts_json}
Open questions across descendants: {open_questions_json}

Compiled topic titles (the ONLY valid targets for `[[Page Title]]` wikilinks inside narrative paragraphs — any other title was dropped by the threshold gate and has no page; pointing at it produces a red broken link): {compiled_topic_titles_json}

{archetype_hint_block}

## Output

Return JSON ONLY with this exact shape:

{{
  "archetype": "folder",
  "plan": {{
    "modules": [
      {{ "id": "<module_id>", "anchor": "<short alphanumeric>" }},
      ...
    ]
  }},
  "hero": {{
    "tldr": "<single bold sentence — what this folder is about>",
    "summary": "<2-3 sentence prose framing scope, key contributors, and any unresolved tension>"
  }},
  "narrative_sections": [
    {{
      "anchor": "<kebab-case>",
      "heading": "<Human readable section title>",
      "paragraphs": [
        {{
          "text": "<paragraph prose with [f_xxx] inline citations>",
          "citations": ["f_xxx", "f_yyy"],
          "is_inference": false
        }}
      ],
      "visual": null
    }}
  ],
  "body_connectors": {{}}
}}

## Narrative-article rules (the new heart of the folder page)

The `narrative_sections` array synthesises ACROSS the descendant pages — it does NOT duplicate each child's narrative. Folder narrative is the cross-cutting story: shared threads, recent shifts, top decisions, tensions spanning sub-pages.

**Section structure** (typical folder narrative):
- Each section has a unique `anchor` (kebab-case, ≤ 24 chars), a `heading` (human readable, sentence case), and a `paragraphs` array.
- Aim for 3-5 sections per folder. Sections should synthesize ACROSS descendants — not summarise each child individually.
- Section titles MUST emerge from the descendant content. Good examples: "Multi-platform connector strategy", "Memory-tier persistence decisions". Bad: generic "Overview", "Context".

**Paragraph structure**:
- Each paragraph is 3-5 sentences. Active voice. Short.
- `text`: the paragraph prose. Synthesise across descendants — name 2-3 children when their content connects.
- `citations`: array of fact_ids cited inline within `text`. Format inline as `[f_xxx]`.
- `is_inference`: set to `true` for SYNTHESIS paragraphs that interpret beyond direct facts. Inference paragraphs MUST still cite ≥1 fact_id.

**Citation discipline (HARD RULES)**:
- EVERY paragraph MUST cite at least one fact_id. Uncited paragraphs are DROPPED.
- Inference paragraphs (`is_inference: true`) MUST still cite ≥1 fact_id.
- FORBIDDEN narration phrases (paragraphs containing these are DROPPED): "shared a link", "shared an article", "noted that", "mentioned that", "posted about", "presented that".
- Article citation coverage MUST be ≥ 80%; below that, the article is REJECTED and the page falls back to module-only rendering.

**Word caps**:
- Each section: 150-400 words. Sections over 400 words are TRUNCATED.
- Total folder article: 1,500-3,000 words. Stay focused — folder narrative complements but does not replace child pages.

**Visual guidance per section (NOT optional when content fits)**:

When a section's content has any of these shapes, you MUST include the matching visual:

- **Comparison / options** (2+ alternatives, trade-offs) → `table` with headers
- **Sequence / process / workflow** → `mermaid` `graph TD` or ordered `list`
- **Architecture / cross-page relationships** → `mermaid` `graph TD` with labeled edges
- **Enumeration of 4+ items** (children, decisions, contributors) → bulleted `list`
- **Statement of importance / quote** → `blockquote`
- **Code / config snippet** → `code` block

Default to including a visual — every section is more readable with one. Set `visual: null` ONLY when the section is purely interpretive prose.

Visual shape:
- `{{"kind": "table", "content": {{"headers": ["A", "B"], "rows": [["1", "2"]]}}}}`
- `{{"kind": "mermaid", "content": "graph TD\\n  A[Foo] --> B[Bar]"}}`
- `{{"kind": "list", "content": {{"ordered": false, "items": ["one", "two", "three"]}}}}`
- `{{"kind": "callout", "content": {{"variant": "warning|info|tip", "text": "..."}}}}`
- `{{"kind": "code", "content": {{"language": "python", "code": "..."}}}}`
- `{{"kind": "blockquote", "content": {{"text": "...", "attribution": "Speaker"}}}}`

**Agent voice**:
- Third-person synthetic voice. Wikipedia-editor style. NO "I", "we", "our".
- Short paragraphs. Active voice. Active verbs name the outcome.
- Cite every claim with `[f_xxx]` inline.
- If you cannot produce a quality narrative (e.g. < 4 facts across descendants), emit `narrative_sections: []` and rely on module-only rendering.

## Module-selection rules

- **HARD RULE — Module #1 in your plan MUST be `hero_summary`.** It reads your `hero.tldr` and `hero.summary` to render the page header.
- **HARD RULE — When `narrative_sections` has at least one validated section, include `narrative_article` IMMEDIATELY AFTER `hero_summary` (module #2).**
- **HARD RULE — `subpage_cards` MUST appear** when at least one child exists. Subpage cards are the primary wayfinding device on a folder page.
- **HARD RULE — `folder_stats` MUST appear** when `child_count` ≥ 2.
- **HARD RULE — `top_contributors` SHOULD appear** when `distinct_contributor_count` ≥ 2.
- **HARD RULE — `cross_cutting_decisions` SHOULD appear** when `descendant_decision_count` ≥ 2.
- **HARD RULE — Penultimate module SHOULD be `open_questions`** when `open_question_count` ≥ 1.
- **HARD RULE — The LAST module MUST be `provenance_drawer`** when `fact_count` ≥ 1.
- A module is ELIGIBLE only when its catalog rule is satisfied — the validator will drop ineligible picks.
- Anchors are lowercase + alphanumeric + dashes, ≤ 24 chars.

## Hero authoring rules

- TL;DR is exactly ONE bold sentence (`**…**`) that names the folder's domain in concrete terms — no generic "this folder contains topics" filler.
- Summary is 2-3 sentences. Cover: (a) the throughline connecting the children, (b) who's most active, (c) any unresolved tension.
- DO NOT write a separate body — the modules render the dashboard. Hero + narrative are the only prose surfaces on the page.

## Hard rules

- Output JSON ONLY. No markdown fences around the outer JSON. No prose before or after.
- DO NOT emit the literal string "Themes & threads" — the dashboard replaces it.
- DO NOT use @ / # / $ entity prefixes — write names normally.
"""


def build_module_compile_folder_prompt(
    *,
    signals: dict,
    module_catalog: list[dict],
    folder_title: str,
    children: list[dict],
    top_contributors: list[dict],
    top_decisions: list[dict],
    top_facts: list[dict] | None = None,
    open_questions: list[dict] | None = None,
    archetype_hint_block: str = "",
    compiled_topic_titles: list[str] | None = None,
) -> str:
    """Render ``MODULE_COMPILE_FOLDER_PROMPT`` with the folder data.

    Single-call: planner + writer + narrative author collapsed into one
    prompt so cost stays at 1 LLM call per folder page (same as the
    legacy ``FOLDER_INDEX_PROMPT`` flow).

    ``top_facts`` is up to 12 highest-quality facts aggregated across
    the folder's descendants — the LLM uses these as the citation
    surface for the narrative article. ``open_questions`` lets the
    narrative reference unresolved threads. ``archetype_hint_block``
    pre-renders the Folder-archetype section-structure hint.
    """
    import json as _json

    catalog_lines: list[str] = []
    for entry in module_catalog:
        catalog_lines.append(
            f"- **{entry['id']}** ({entry['label']}) — {entry['description']} "
            f"_Rule:_ {entry['rule']}"
        )
    catalog_block = "\n".join(catalog_lines)

    children_payload = [
        {
            "title": (c.get("title") or "")[:80],
            "summary": (c.get("summary") or "")[:160],
            "slug": c.get("slug") or "",
        }
        for c in children
    ]
    contributors_payload = [
        {
            "name": c.get("name") or "",
            "contribution_count": int(c.get("contribution_count") or 0),
        }
        for c in (top_contributors or [])[:5]
    ]
    decisions_payload = [
        {
            "title": (d.get("title") or "")[:120],
            "decided_by": d.get("decided_by") or "",
            "importance": d.get("importance") or "medium",
        }
        for d in (top_decisions or [])[:5]
    ]
    facts_payload = [
        {
            "fact_id": f.get("fact_id") or f.get("id") or "",
            "memory_text": (f.get("memory_text") or f.get("fact") or "")[:240],
            "author": f.get("author_name") or f.get("user_name") or "",
            "fact_type": f.get("fact_type") or "",
        }
        for f in (top_facts or [])[:12]
        if isinstance(f, dict)
    ]
    open_questions_payload = [
        {
            "question": (q.get("question") or "")[:200],
            "raised": q.get("raised") or "",
        }
        for q in (open_questions or [])[:8]
        if isinstance(q, dict)
    ]
    return MODULE_COMPILE_FOLDER_PROMPT.format(
        module_catalog_block=catalog_block,
        signals_json=_json.dumps(signals, indent=2, default=str),
        folder_title=folder_title,
        children_json=_json.dumps(children_payload, indent=2),
        top_contributors_json=_json.dumps(contributors_payload, indent=2),
        top_decisions_json=_json.dumps(decisions_payload, indent=2),
        top_facts_json=_json.dumps(facts_payload, indent=2, default=str),
        open_questions_json=_json.dumps(open_questions_payload, indent=2, default=str),
        compiled_topic_titles_json=_json.dumps(list(compiled_topic_titles or []), default=str),
        archetype_hint_block=archetype_hint_block or "",
    )


FOLDER_INDEX_PROMPT = """You are a knowledge wiki compiler. Create a **Folder Index** page that explains what's inside this folder and helps the reader navigate to the right sub-page.

## Inputs

### Folder title
{folder_title}

### Direct children (in display order)
{children_json}

### Top entities mentioned across descendants
{entities_json}

### Top-quality supporting facts
{top_facts_json}

## Task

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary of the folder"}}

The markdown body MUST:

1. Open with a 2-3 sentence intro that frames what this folder covers — the domain, the scope, why it exists. Make it specific to the children listed above; do NOT write a generic "this folder contains topics" line.

2. On its own line, include the literal token `<<CHILDREN_TOC>>`. The compiler will replace this with a navigation list AND the frontend renders it as rich cards. Each child's per-bullet summary is what populates the per-card description — write each child's `summary` (in the input) as a SINGLE COMPLETE SENTENCE (40-90 chars) that names the concrete subject of that page. Avoid trailing fragments like "covering architectural discussions, platfo" — finish the sentence.

3. After the marker, write 2-4 short paragraphs of synthesis under an implicit "themes & threads" framing — the frontend will surface them under that heading. Cover at least: (a) the most important throughline connecting the children, (b) the key contributors active across this folder and what they own, (c) any unresolved tension or open question worth flagging. Reference children by their titles. This is what makes the folder PAGE valuable beyond just its TOC.

4. Stay between **300 and 500 words** total (excluding the marker line). Folders that need more depth should rely on their child pages — the index is a wayfinding device + a synthesis layer, not a deep dive.

## Hard rules

- Output JSON ONLY. No markdown code fences. No leading or trailing prose outside the JSON.
- Use plain Markdown (no callouts, no mermaid diagrams in the index — those belong on leaf pages).
- The `summary` field is 1-2 sentences max, suitable for a card or hover preview.
- NEVER truncate a sentence with a fragment. Every sentence must end with a period (or `?`/`!`). Cut the sentence shorter rather than trail off mid-clause.
"""


def build_folder_index_prompt(
    *,
    folder_title: str,
    children: list[dict],
    aggregated_entities: list[str],
    top_facts: list[dict],
) -> str:
    """Render ``FOLDER_INDEX_PROMPT`` with the given inputs.

    ``children`` is a list of ``{title, summary}`` dicts (200-char
    summaries). ``aggregated_entities`` is the union of top-5 entities
    across all descendants. ``top_facts`` is up to 5 highest-
    quality_score facts across descendants — gives the LLM concrete
    material to riff on without dumping the whole fact set.
    """
    import json as _json

    children_payload = [
        {
            "title": c.get("title") or "",
            "summary": (c.get("summary") or "")[:200],
        }
        for c in children
    ]
    facts_payload = [
        {
            "fact": (f.get("memory_text") or f.get("fact") or "")[:200],
            "author": f.get("author_name") or f.get("user_name") or "",
            "type": f.get("fact_type") or "",
        }
        for f in top_facts[:5]
    ]
    return FOLDER_INDEX_PROMPT.format(
        folder_title=folder_title,
        children_json=_json.dumps(children_payload, indent=2),
        entities_json=_json.dumps(aggregated_entities[:10]),
        top_facts_json=_json.dumps(facts_payload, indent=2),
    )


def build_structure_planner_prompt(
    *,
    channel_summary: str,
    clusters: list[dict],
    candidate_groups: list,
) -> str:
    """Render ``STRUCTURE_PLANNER_PROMPT`` with the given context.

    Compresses each cluster to its essentials (id, title, summary
    truncated to 200 chars, member_count, top-5 entity names) so the
    prompt fits comfortably under the model's context window even for
    100+ topic channels. Candidates are flattened to ``{group_id,
    members, signals}`` triples so the LLM can see why each was
    proposed.
    """
    import json as _json

    cluster_index = []
    for c in clusters:
        cid = c.get("id")
        if not cid:
            continue
        summary = (c.get("summary") or "")[:200]
        entities = []
        for e in (c.get("key_entities") or [])[:5]:
            if isinstance(e, dict):
                name = e.get("name") or e.get("entity_name") or ""
                if name:
                    entities.append(name)
            elif isinstance(e, str) and e:
                entities.append(e)
        cluster_index.append(
            {
                "id": cid,
                "title": c.get("title") or "",
                "summary": summary,
                "member_count": c.get("member_count") or 0,
                "key_entities": entities,
            }
        )

    candidate_payload = []
    for i, group in enumerate(candidate_groups):
        members = sorted(group.cluster_ids) if hasattr(group, "cluster_ids") else []
        signals = group.signals if hasattr(group, "signals") else {}
        candidate_payload.append(
            {
                "candidate_id": f"cand-{i + 1}",
                "members": members,
                "signals": signals,
            }
        )

    return STRUCTURE_PLANNER_PROMPT.format(
        channel_summary=channel_summary or "(no channel summary available)",
        cluster_index_json=_json.dumps(cluster_index, indent=2),
        candidates_json=_json.dumps(candidate_payload, indent=2),
    )
