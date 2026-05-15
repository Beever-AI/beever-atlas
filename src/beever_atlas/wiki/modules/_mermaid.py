"""Tiny mermaid helpers shared by flow_chart + entity_diagram modules.

Mermaid is picky about ID syntax (no spaces, no special chars except
underscore + alphanumeric) and label content (parentheses, pipes,
brackets, quotes break the parser). Centralising the sanitizer here
keeps both modules emitting renderable diagrams.
"""

from __future__ import annotations

import re

_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_]+")
# Characters that break mermaid label parsing inside [] or () nodes.
_LABEL_FORBIDDEN = ("[", "]", "(", ")", "|", '"', "`", "#", ";", "\n", "\r")


def safe_label(raw: str, max_len: int = 60) -> str:
    """Strip characters mermaid mis-parses inside `[label]`. Truncate
    long labels with ASCII ellipsis since mermaid 11.x with
    ``htmlLabels: false`` does not reliably parse non-ASCII characters."""
    if not raw:
        return ""
    out = raw
    for ch in _LABEL_FORBIDDEN:
        out = out.replace(ch, " ")
    out = " ".join(out.split())
    if len(out) > max_len:
        out = out[: max_len - 1].rstrip() + "..."
    return out


def safe_id(raw: str, fallback: str = "N", seen: set[str] | None = None) -> str:
    """Squash a string into a mermaid-safe ID. Empty input returns
    the fallback so callers can iterate counter-suffixed IDs without
    branching on the empty case.

    Pass ``seen`` (a caller-owned set that is mutated in place) to
    detect collisions: when the generated ID is already in ``seen``
    a numeric suffix ``_2``, ``_3``, … is appended until the ID is
    unique. Without ``seen``, two distinct node names that reduce to
    the same ID will silently collide — the second declaration is
    dropped by mermaid and its edges become dangling.
    """
    if not raw:
        candidate = fallback
    else:
        candidate = _ID_SAFE_RE.sub("_", raw).strip("_") or fallback

    if seen is None:
        return candidate

    if candidate not in seen:
        seen.add(candidate)
        return candidate

    # Collision: append _2, _3, … until unique. The cap is a defensive
    # bound — pathological inputs (e.g., 10k+ entities all sanitizing to
    # the same string) would otherwise loop unboundedly. In practice
    # entity counts per diagram are < 100.
    counter = 2
    while counter <= 10_000:
        suffixed = f"{candidate}_{counter}"
        if suffixed not in seen:
            seen.add(suffixed)
            return suffixed
        counter += 1
    # Last resort: return the over-cap candidate. Caller may emit a
    # duplicate node declaration, which mermaid handles by silently
    # dropping the second one. Better than hanging the request.
    return f"{candidate}_{counter}"
