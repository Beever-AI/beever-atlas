"""``top_contributors`` module — frontend renderer.

Surfaces the top contributors across a folder's descendant pages as
a horizontal strip of chips. Each chip shows: name, contribution
count, and the page title(s) the contributor was most active in.
Replaces the "key contributors active across this folder" paragraph
the legacy ``FOLDER_INDEX_PROMPT`` produced.

Pure builder — the React renderer lives in
``web/src/components/wiki/modules/TopContributorsModule.tsx``.

Eligibility (catalog predicate):
  - ``signals["archetype"] == "folder"``
  - ``signals["distinct_contributor_count"] >= 2``

Sort: contribution_count DESC, then name ASC for tie-break
determinism. Cap at 5 contributors per strip — beyond that the
visual reads as a list, not a strip.
"""

from __future__ import annotations

from typing import Any


def _author_name(fact: Any) -> str:
    if not isinstance(fact, dict):
        return ""
    return str(fact.get("author_name") or fact.get("user_name") or fact.get("author") or "").strip()


def build_top_contributors_data(
    descendants: list[dict] | None,
    cap: int = 5,
) -> dict[str, Any]:
    """Aggregate descendant pages' authors into a top-N contributor list.

    ``descendants`` is a list of page-shaped dicts: each carries a
    ``title`` (for source-page attribution) and a ``facts`` list.
    Each fact's author contributes one to the contributor's count;
    distinct (name, page) pairs roll up into the per-contributor
    ``top_pages`` list (truncated to 2 entries).

    Returns:
        {
          "label": "Top contributors",
          "renderer_kind": "frontend",
          "items": [
            {
              "name": "Alan Yang",
              "contribution_count": 14,
              "top_pages": [
                {"title": "JWT Migration", "count": 8},
                {"title": "Auth Roadmap", "count": 6},
              ],
            },
            ...
          ]
        }
    """
    if not isinstance(descendants, list):
        descendants = []

    counter: dict[str, dict[str, Any]] = {}
    for d in descendants:
        if not isinstance(d, dict):
            continue
        page_title = str(d.get("title") or "").strip()
        facts = d.get("facts") or []
        if not isinstance(facts, list):
            continue
        for f in facts:
            name = _author_name(f)
            if not name:
                continue
            entry = counter.setdefault(
                name,
                {
                    "name": name,
                    "contribution_count": 0,
                    "top_pages": {},
                },
            )
            entry["contribution_count"] += 1
            if page_title:
                pages = entry["top_pages"]
                pages[page_title] = pages.get(page_title, 0) + 1

    # Sort by contribution_count DESC; tie-break alphabetically by name.
    sorted_contributors = sorted(
        counter.values(),
        key=lambda x: (-int(x["contribution_count"]), str(x["name"])),
    )[: max(int(cap), 0)]

    # Convert each contributor's top_pages dict to a list of the top 2
    # page-title entries (count DESC, then title ASC for stability).
    for c in sorted_contributors:
        pages_dict: dict[str, int] = c["top_pages"]
        ranked = sorted(pages_dict.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
        c["top_pages"] = [{"title": t, "count": n} for t, n in ranked]

    return {
        "label": "Top contributors",
        "renderer_kind": "frontend",
        "items": sorted_contributors,
    }
