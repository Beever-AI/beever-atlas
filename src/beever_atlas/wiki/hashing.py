"""Canonical hashing for wiki page recompile-skip optimisation.

The Builder's per-page LLM call dominates cost on Regenerate. Most regenerates
find that the canonical structured input for a given page is byte-identical to
the prior build — so the prose can be reused. ``compute_kind_schema_hash``
produces a stable SHA-256 over the canonical input so the Builder can decide
to skip the LLM call when the hash matches the page's stored
``wiki_pages.kind_schema_hash``.

Canonicalisation rules (per design D5):

* Sort dict keys recursively.
* Strip leading/trailing whitespace from string values.
* Sort list items where order is semantically irrelevant (the per-kind config
  declares which list fields are unordered; everything else is preserved).
* Exclude derived / timestamp fields whose changes do not represent a
  semantically different input (counts, summaries, generated_at).
* Mix in the ``prompt_version`` so a prompt edit busts the cache.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


# Per-kind config: which list fields are order-irrelevant (sorted before
# hashing) vs. order-significant (preserved). Default is order-significant.
_UNORDERED_LIST_FIELDS: dict[str, set[str]] = {
    "people": {"contributors", "people", "roles"},
    "glossary": {"terms"},
    "topic": {"entity_tags", "people", "key_systems"},
    "subtopic": {"entity_tags", "people", "key_systems"},
    "decisions": {"contributors"},
    "faq": {"contributors"},
    "activity": set(),  # chronological — preserve order
    "resources": {"links", "media"},
    "projects": {"contributors"},
    "architecture": {"systems"},
    "open-questions": {"contributors"},
    "timeline": set(),  # chronological — preserve order
    "stakeholders": {"contributors", "people"},
}

# Fields excluded from the canonical input — derived counts / timestamps.
_EXCLUDED_FIELDS: frozenset[str] = frozenset(
    {
        "generated_at",
        "updated_at",
        "computed_summary",
        "fact_count",
        "version",
    }
)


def _is_unordered(kind: str, field_path: tuple[str, ...]) -> bool:
    """Return True if the list at ``field_path`` is unordered for ``kind``."""
    if not field_path:
        return False
    leaf = field_path[-1]
    return leaf in _UNORDERED_LIST_FIELDS.get(kind, set())


def _canonicalize(value: Any, kind: str, path: tuple[str, ...] = ()) -> Any:
    """Recursively normalise ``value`` for stable hashing."""
    if isinstance(value, dict):
        canonical: dict[str, Any] = {}
        for k in sorted(value.keys()):
            if k in _EXCLUDED_FIELDS:
                continue
            canonical[k] = _canonicalize(value[k], kind, path + (k,))
        return canonical
    if isinstance(value, list):
        items = [_canonicalize(v, kind, path) for v in value]
        if _is_unordered(kind, path):
            # Sort by canonical JSON serialisation so order varies don't
            # show up as semantic differences. Items must be JSON-serialisable
            # at this point (they've been recursively canonicalised already).
            try:
                items = sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
            except TypeError:
                # Fall back to string repr when items are not JSON-clean.
                items = sorted(items, key=str)
        return items
    if isinstance(value, str):
        return value.strip()
    return value


def compute_kind_schema_hash(
    kind: str,
    payload: dict[str, Any] | None,
    prompt_version: str | None = None,
) -> str:
    """Return a stable SHA-256 hex digest for ``payload``.

    ``kind`` selects the per-kind canonicalisation rules (which list fields
    are unordered). ``prompt_version`` is mixed into the digest so a prompt
    file edit invalidates the cache for every page of that kind.

    Returns an empty string for ``payload=None`` so the caller can use
    truthy checks to decide whether to skip the comparison.
    """
    if payload is None:
        return ""
    canonical = _canonicalize(payload, kind)
    serialised = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256()
    h.update(kind.encode("utf-8"))
    h.update(b"\x00")
    if prompt_version:
        h.update(prompt_version.encode("utf-8"))
    h.update(b"\x00")
    h.update(serialised.encode("utf-8"))
    return h.hexdigest()


def compute_prompt_version(prompt_text: str) -> str:
    """Short hash of a prompt's text content — used as ``prompt_version``.

    Mixed into ``compute_kind_schema_hash`` so editing the prompt invalidates
    the cache without an explicit version bump.
    """
    if not prompt_text:
        return ""
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]
