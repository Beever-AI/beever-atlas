"""``subpage_cards`` module — wraps the existing ``render_children_toc``.

Same pattern as folder pages. Reuses the renderer so the trim/word-
boundary behavior stays consistent across folder index pages and
parent-topic pages with sub-topics.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.render import render_children_toc


def render(data: dict[str, Any]) -> str:
    children = data.get("children") or []
    if not isinstance(children, list):
        return ""
    return render_children_toc(children)
