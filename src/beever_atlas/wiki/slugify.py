"""Shared slug derivation for the wiki-llm-native-redesign change.

Two paths produce slugs:
  * the maintainer's first-touch + the curation API's split endpoint
    (operator-driven, runtime);
  * the offline migration script that backfills slugs onto legacy
    ``wiki_pages`` rows.

Both must produce identical slugs for identical inputs — otherwise a
split-created page could collide with a migrated page in surprising
ways. This module owns the canonical kebab-case derivation; both
call sites import from here.
"""

from __future__ import annotations

import re

# Replace any run of non-alphanumeric characters with a single hyphen.
# Lowercase ASCII only — the curation API's URL routes are case-
# insensitive in practice, and Mongo's compound unique index treats
# slugs as exact strings, so collapsing case is the safest choice.
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def slugify(title: str, fallback_page_id: str = "") -> str:
    """Convert ``title`` (or ``fallback_page_id`` when title is blank)
    into a kebab-case slug.

    The result:
      * lowercased;
      * non-alphanumeric runs collapsed to a single ``-``;
      * stripped of leading/trailing ``-``;
      * never empty (returns ``"untitled"`` when both inputs collapse
        to nothing).
    """
    raw = (title or "").strip().lower()
    if not raw:
        raw = (fallback_page_id or "").replace(":", "-").lower()
    cleaned = _SLUG_PATTERN.sub("-", raw).strip("-")
    return cleaned or "untitled"


__all__ = ["slugify"]
