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
    refs = {fn: load_resource(fn) for fn in resource_files}
    return Skill(
        frontmatter=Frontmatter(
            name=name,
            description=description,
            allowed_tools=allowed_tools,
        ),
        instructions=instructions,
        resources=Resources(references=refs) if refs else Resources(),
    )


def _build_skills() -> list[Skill]:
    return [
        _skill(
            name="decision-trace",
            description=(
                "Decision trace, decision history, evolution of a choice over time: "
                "renders a vertical timeline of who proposed/pushed-back/decided what "
                "and when, with one pinned step per event and a final outcome arrow."
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
                "People profile card, expertise lookup, who-is-@handle: summarizes a "
                "person's inferred role, top 3 topics, recent activity, and 3 cited "
                "evidence bullets from channel facts."
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
                "Visual graph, flowchart, timeline diagram, relationship diagram: emits a "
                "Mermaid fenced block (flowchart / timeline / graph LR) when a diagram "
                "clarifies the answer better than prose."
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
                "Media gallery, images, screenshots, files, diagrams, attachments: renders "
                "image/file hits from search_media_references as a markdown gallery with "
                "inline thumbnails, captions, and citations."
            ),
            allowed_tools="search_media_references",
            resource_files=("gallery_template.md",),
            instructions=(
                "Use when the user asks about images, screenshots, diagrams, files, or any "
                "attached media (e.g. 'show me screenshots of X', 'files about Y').\n"
                "1. Call `search_media_references(query=...)` once.\n"
                "2. Load `gallery_template.md` via `load_resource` and follow it EXACTLY: "
                "markdown image bullets, caption, one-line context, `[src:src_xxx inline]` "
                "so the UI renders the attachment next to the citation.\n"
                "If no results, say 'No media attachments found for this query.' — do not fabricate."
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
                "External context / Synthesis)."
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
