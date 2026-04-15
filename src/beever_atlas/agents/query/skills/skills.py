"""Inline QA agent skill pack (ADK progressive disclosure).

Each `Skill` is defined inline with keyword-frontloaded descriptions and
short L2 `instructions`. Heavy L3 formatting templates live as `.md`
files in `resources/` and are referenced by filename — the ADK
`load_resource` tool fetches them on demand.

The pack is built once at module load and cached.
"""

from __future__ import annotations

from functools import lru_cache

from google.adk.skills.models import Frontmatter, Resources, Skill

from ._loader import load_resource


# Kebab-case skill names (each ≤ 64 chars, matches ^[a-z][a-z0-9-]*$).
QA_SKILL_NAMES: tuple[str, ...] = (
    "decision-trace",
    "people-profile",
    "comparison",
    "visual-graph",
    "media-gallery",
    "channel-digest",
    "source-braid",
    "typed-followups",
)


def _skill(
    *,
    name: str,
    description: str,
    allowed_tools: str | None,
    instructions: str,
    resource_files: tuple[str, ...] = (),
) -> Skill:
    assets = {fn: load_resource(fn) for fn in resource_files}
    return Skill(
        frontmatter=Frontmatter(
            name=name,
            description=description,
            allowed_tools=allowed_tools,
        ),
        instructions=instructions,
        resources=Resources(assets=assets) if assets else Resources(),
    )


def _build_skills() -> list[Skill]:
    return [
        _skill(
            name="decision-trace",
            description=(
                "Decision trace, decision history, evolution of a choice over time: "
                "renders a vertical timeline of who proposed/pushed-back/decided what "
                "and when, with one pinned step per event and a final outcome arrow. "
                "If `trace_decision_history` returns no SUPERSEDES chain, STILL use the "
                "timeline template — fill it with search_channel_facts events sorted by "
                "timestamp. Never degrade to plain prose."
            ),
            allowed_tools="trace_decision_history search_channel_facts",
            resource_files=("timeline_template.md",),
            instructions=(
                "Use when the user asks 'why decide X', 'how did Y evolve',"
                "'decision history of Z', or any question about how a team arrived "
                "at a choice.\n"
                "1. Call `trace_decision_history(topic=...)` for the chronological spine.\n"
                "2. Call `search_channel_facts` to fill rationale gaps per step.\n"
                "3. Load `timeline_template.md` via `load_resource` and follow it EXACTLY: "
                "oldest step first, one bullet per event, pinned emoji, date, actor, "
                "one-line rationale, `[src:...]` tag. End with the `→ Outcome:` arrow.\n"
                "If no recorded history, emit one bullet noting that and stop — do not fabricate."
            ),
        ),
        _skill(
            name="people-profile",
            description=(
                "People profile card, expertise lookup, who-is-@handle, 'who works on', "
                "'key people', 'team members', 'contributors', 'who are the': summarizes "
                "a person's inferred role, top 3 topics, recent activity, and 3 cited "
                "evidence bullets. When the question asks about 3+ people, render a "
                "markdown table with columns Handle / Role / Top topics / Evidence (cited). "
                "Single-person queries render a card."
            ),
            allowed_tools="find_experts search_channel_facts",
            resource_files=("profile_template.md",),
            instructions=(
                "Use when the user asks 'who works on X', 'expert in Y', "
                "'who is @handle', or any person-shaped question.\n"
                "1. Call `find_experts(topic=...)` when topic-centric; otherwise start with "
                "`search_channel_facts(query=handle or name)`.\n"
                "2. Gather at least 3 distinct evidence facts for the person.\n"
                "3. Load `profile_template.md` via `load_resource` and follow it EXACTLY: "
                "handle, inferred role (≤40 chars), 3 top topics, recent activity line, "
                "3 evidence bullets each with `[src:...]`.\n"
                "Do not guess employer, timezone, or email."
            ),
        ),
        _skill(
            name="comparison",
            description=(
                "Comparison table, A vs B, pros and cons, differences between options: "
                "renders a markdown table with dimensions as rows and entities as columns, "
                "every cell cited, plus a short synthesis summary."
            ),
            allowed_tools="search_channel_facts search_external_knowledge",
            resource_files=("comparison_table_template.md",),
            instructions=(
                "Use when the user asks 'A vs B', 'differences between', 'pros and cons', "
                "or any question comparing 2+ entities across 2+ attributes.\n"
                "1. Call `search_channel_facts` once per entity to collect internal evidence.\n"
                "2. Call `search_external_knowledge` ONLY if internal evidence is thin or the "
                "question invokes industry/benchmark framing.\n"
                "3. Load `comparison_table_template.md` via `load_resource` and follow it "
                "EXACTLY: markdown table, rows=dimensions, columns=entities, `[src:...]` on "
                "every non-empty cell, `—` for unknown, 2-3 sentence Summary line below."
            ),
        ),
        _skill(
            name="visual-graph",
            description=(
                "Visual graph, flowchart, timeline diagram, relationship diagram, architecture, "
                "pipeline, workflow, data flow, org chart, dependency graph, decision tree, "
                "supersedes chain, process, sequence, hierarchy: emits a Mermaid fenced block "
                "(flowchart / timeline / graph LR / graph TD) whenever the answer involves 3+ "
                "related entities with directional relationships. PREFER Mermaid over prose for "
                "any question touching 'pipeline', 'architecture', 'flow', 'workflow', 'process', "
                "'how X works', 'how data moves', 'who reports to', 'what happens before/after', "
                "'timeline of events', 'org structure', 'dependencies between'."
            ),
            allowed_tools=None,
            resource_files=("mermaid_cheatsheet.md",),
            instructions=(
                "Use when the answer involves a process flow, time-ordered sequence, or "
                "entity-relationship structure that is clearer as a diagram than prose.\n"
                "This skill ADDS a diagram to an answer; it does not replace retrieval. "
                "Retrieve facts via other tools first, then:\n"
                "1. Load `mermaid_cheatsheet.md` via `load_resource` for syntax.\n"
                "2. Pick the right diagram type: `flowchart` for process, `timeline` for "
                "evolution, `graph LR` for relationships.\n"
                "3. Emit ONE fenced ```mermaid block with ≤12 nodes, every edge labelled. "
                "Place `[src:...]` tags in the surrounding prose, never inside the block."
            ),
        ),
        _skill(
            name="media-gallery",
            description=(
                "Media gallery, images, screenshots, files, diagrams, attachments, architecture diagram, "
                "flowchart of, image of, photos, pictures, visuals, uploads: renders "
                "image/file hits from search_media_references as a markdown gallery with "
                "inline thumbnails, captions, and citations. Use for any question that mentions "
                "'show me', 'visual', 'pictures', 'photos', 'files', 'uploads', 'diagrams', "
                "'screenshots', 'architecture diagram', 'flowchart of', 'image of'."
            ),
            allowed_tools="search_media_references",
            resource_files=("gallery_template.md",),
            instructions=(
                "Use when the user asks about images, screenshots, diagrams, files, or any "
                "attached media (e.g. 'show me screenshots of X', 'files about Y'), OR any "
                "time `search_media_references` was called and returned hits.\n"
                "1. Call `search_media_references(query=...)` once.\n"
                "2. If the tool returned AT LEAST ONE hit with a non-empty `media_urls` OR "
                "`link_urls`, you MUST load `gallery_template.md` via `load_resource` and emit "
                "a dedicated `## Media` section at the END of the answer. The section MUST:\n"
                "   - Start with the heading `## Media`.\n"
                "   - Contain one bullet per hit.\n"
                "   - Each bullet MUST begin with `![<caption>](<first url from media_urls or "
                "link_urls>)` on its own line — this is a HARD REQUIREMENT, not optional.\n"
                "   - Follow the image line with `**<caption>** — <one-line context> "
                "[src:src_xxx inline]` on the next line, using the `_src_id` from the tool "
                "result for src_xxx.\n"
                "   - NEVER fold media into prose with only a `[src:...]` chip — the image "
                "syntax is what makes the attachment visible to the user.\n"
                "3. You may still reference the same sources in the body with inline citations, "
                "but the `## Media` gallery section is MANDATORY whenever hits exist.\n"
                "4. If the tool returned ZERO hits, say "
                "'No media attachments matching the query were found.' in one sentence and "
                "STOP. Do not fabricate."
            ),
        ),
        _skill(
            name="channel-digest",
            description=(
                "Channel digest, summarize this channel, what's happening, channel overview: "
                "renders Topics / Decisions / People / Open threads sections from topic "
                "overview and recent activity tools."
            ),
            allowed_tools="get_topic_overview get_recent_activity",
            resource_files=("digest_template.md",),
            instructions=(
                "Use when the user asks 'summarize this channel', 'what's happening', "
                "'give me an overview', or any channel-wide digest request.\n"
                "1. Call `get_topic_overview(channel_id)` with no topic_name for the spine.\n"
                "2. Call `get_recent_activity(channel_id)` for Open threads.\n"
                "3. Load `digest_template.md` via `load_resource` and emit ALL FOUR sections: "
                "`### Topics`, `### Decisions`, `### People`, `### Open threads`. If a "
                "section has no evidence, write 'No items found.' — never omit the heading.\n"
                "Cap each section at 5 bullets; prefer recency when trimming."
            ),
        ),
        _skill(
            name="source-braid",
            description=(
                "Source braid, internal plus external synthesis: braids team knowledge with "
                "external context across three labelled lines (From your knowledge base / "
                "External context / Synthesis). USE this whenever the user's question touches "
                "industry benchmarks, best practices, standards, comparisons to the outside world, "
                "public documentation, or when the internal knowledge base has <3 relevant facts. "
                "Examples: 'how do we compare to industry', 'what are best practices', "
                "'is this standard', 'what do others do'."
            ),
            allowed_tools="search_channel_facts search_external_knowledge",
            resource_files=("braid_pattern.md",),
            instructions=(
                "Use when answering benefits from BOTH internal team knowledge AND external "
                "context (industry benchmarks, public docs, best practices).\n"
                "1. Call `search_channel_facts` for the internal side.\n"
                "2. Call `search_external_knowledge` for the external side.\n"
                "3. Load `braid_pattern.md` via `load_resource` and emit EXACTLY three "
                "bold-labelled one-liners: **From your knowledge base:** (internal cites), "
                "**External context:** (external cite), **Synthesis:** (no cites, the bridge).\n"
                "If external returned nothing, emit only the internal line."
            ),
        ),
        _skill(
            name="typed-followups",
            description=(
                "Typed follow-ups, per-query-type follow-up suggestions: classifies the just-"
                "answered question (people / decision / definition / comparison / general) "
                "and emits 2-3 context-aware follow-up strings via suggest_follow_ups."
            ),
            allowed_tools="suggest_follow_ups",
            resource_files=("followup_templates_by_type.md",),
            instructions=(
                "Use at the end of ANY response that should offer follow-up questions.\n"
                "1. Classify the just-answered question as one of: people / decision / "
                "definition / comparison / general.\n"
                "2. Load `followup_templates_by_type.md` via `load_resource` and pick the "
                "matching template block.\n"
                "3. Substitute concrete values (`<handle>`, `<topic>`, `<decision>`) from the "
                "question into 2-3 suggestions.\n"
                "4. Call `suggest_follow_ups` ONCE with the suggestion list. Strings only — "
                "no bullets, no numbering. Match the user's language."
            ),
        ),
    ]


@lru_cache(maxsize=1)
def build_qa_skill_pack() -> list[Skill]:
    """Return the cached QA skill pack (8 skills). Parsed once."""
    return _build_skills()
