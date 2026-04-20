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
2. **Concept map** — ```mermaid flowchart showing how the main topics/themes relate to each other. Use the topic relationships data to build accurate connections.
3. **Key Highlights table** — GFM table summarizing: total topics, decisions made, key contributors, resources shared, active period. **CRITICAL**: use the EXACT numbers from the channel-data block below — `{decisions_count}` for "Decisions Made", `{people_count}` for "Key Contributors", `{media_count}` for "Resources Shared". Do NOT recompute these from topic descriptions; the provided counts are authoritative.
4. **Topics at a glance** — bullet list of each topic with 1-line description and memory count. Topics with `"brief": true` in the data are minor/off-topic and were not given full pages — append "(brief mention)" after their entry.
5. **Key contributors** — bullet list of most active people and their roles/expertise
6. **Tools & resources** — if technologies or tools data exists, show as bullet list or GFM table. Skip this section entirely if no tools/technologies are relevant.
7. **Recent momentum** — 2-3 sentences on what's currently active or changing. Reference the activity summary data.

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
7. **Tools & resources** — if technologies/tools exist, bullet list. Skip if none.
8. **Current state & open questions** — what's resolved vs. still open. Use bullet points. Each open question MUST include when it was first raised (e.g., "(raised Jan 2026)") so readers can assess staleness.
9. **Media & Resources** — if media exists, each item MUST have a brief description line explaining what it shows/contains BEFORE the embed/link. Format: `**Dashboard Screenshot** — Shows the Insights dashboard with memory health metrics.` followed by `![Dashboard](url)`. Do NOT use bare bullet points with just a link. Skip if none.
10. **See Also** — if related topics exist, list them as bullet points with their titles. Format: `- **[Related Topic Title]**` — one line each. Skip if no related topics.

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
4. **Contributors table** — GFM table with columns: Name, Role/Expertise, Topics Active In, Key Contributions, Decisions Made
5. **Collaboration patterns** — bullet points on notable collaboration patterns, expertise clusters, and knowledge areas

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

## Data
FAQ candidates (grouped by topic): {faq_candidates_json}
Topic names for reference: {topic_names_json}
"""

GLOSSARY_PROMPT = """You are a knowledge wiki compiler. Create a **Glossary** page.

Return JSON: {{"content": "markdown string", "summary": "1-2 sentence summary"}}

## Content structure (follow this order strictly)
1. **Relationship diagram** — if 5+ terms exist, include a ```mermaid diagram showing how terms relate to each other (which terms are used together, which are sub-concepts of others). THIS MUST BE THE VERY FIRST CONTENT ELEMENT (skip if fewer than 5 terms — start with Introduction instead).
2. **Introduction** — 1 sentence: "Key terms, acronyms, and concepts used in this channel."
3. **Terms table** — GFM table with columns: Term, Definition, First Mentioned By, Related Topics. Sort alphabetically. In the "First Mentioned By" column, write "—" when the source is unknown; NEVER write placeholder markers like `(Implicit)`, `(Inferred)`, `(Unknown)`, `(N/A)`, or any parenthesized guess.
4. **Category breakdown** — if terms naturally group into categories (e.g., technical terms, process terms, domain terms), add a brief categorized list after the table

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

## Content structure (follow this order strictly — TL;DR FIRST, then DIAGRAM, then text)
1. **TL;DR** — A single bold sentence summarizing the key insight of this sub-topic. THIS MUST BE THE VERY FIRST LINE.
2. **Concept diagram** — ```mermaid diagram showing key entities and relationships within this sub-topic.
3. **Key Facts** — GFM table with columns: Fact, Source, Type, Importance — the most important facts with [N] citations
4. **Overview** — 2-3 sentences on what this sub-topic covers and how it relates to the parent topic (AFTER diagram and table)
5. **Details** — bullet points expanding on the key facts, decisions, and context
6. **Contributors** — bullet list of people involved, if relevant. Skip if not meaningful.

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
"""

# Phase 4 v2 variant of SUBTOPIC_PROMPT.
SUBTOPIC_PROMPT_V2 = (
    SUBTOPIC_PROMPT.replace(
        "3. **Key Facts** — GFM table with columns: Fact, Source, Type, Importance — the most important facts with [N] citations",
        _KEY_FACTS_MARKER_INSTRUCTION,
    )
    + "\n\n"
    + _NO_MARKER_ECHO_INSTRUCTION
    + "\n"
)
