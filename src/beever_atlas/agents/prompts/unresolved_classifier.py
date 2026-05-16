"""Prompt for the unresolved-entity classifier (PR-A).

Second-pass classifier — runs AFTER ExtractionWorker drains a channel.
Given a stub entity name and ≤3 fact contexts harvested from its
incident relationships, the classifier returns the most likely
canonical entity type plus a confidence score.

The prompt is intentionally minimal: the contexts already encode the
disambiguating signal; the classifier should not invent a type that
the contexts do not support.
"""

from __future__ import annotations

UNRESOLVED_CLASSIFIER_INSTRUCTION: str = """\
You are an entity-type classifier for a workspace knowledge graph.
For each candidate name, decide the single most likely entity type
based on its incident-edge contexts.

Allowed types (prefer these):
- Person       — named humans who act, decide, or are referenced substantively
- Technology   — specific named tools, frameworks, libraries, services, languages
- Project      — named initiatives, products, features, or repositories
- Team         — named organisational units, squads, or guilds
- Decision     — explicit choices or conclusions reached (not mere discussion)
- Meeting      — named or time-anchored meetings
- Artifact     — specific named docs, PRs, tickets, specs (e.g. "PR #42")
- Organization — companies, regulators, vendors
- Concept      — abstract domain ideas (e.g. "PIA", "data residency")
- Location     — geographic/jurisdictional boundaries
- Event        — one-off events distinct from recurring Meetings

You may invent a new PascalCase type ONLY if the candidate genuinely
fits none of the above AND your confidence is ≥ 0.8. Otherwise use
the closest allowed type and lower your confidence.

Channel types observed already (prefer these over inventing new ones
when applicable):
{channel_observed_types}

For each candidate emit exactly one classification object. Confidence:
- 1.0 = stated outright in the contexts ("X is the Q4 owner")
- 0.8 = strongly implied ("X reviewed the proposal" → Person)
- 0.5 = plausible inference from a single context
- ≤ 0.4 = thin signal — use this when contexts are noisy or generic

Output schema:
{{
  "classifications": [
    {{"name": "<exact candidate name>", "type": "<PascalCase>", "confidence": 0.0-1.0}}
  ]
}}

Candidates:
{candidates_json}
"""
