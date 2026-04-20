"""Content validators for LLM-generated wiki sections.

Each validator is a callable ``(content: str) -> tuple[bool, str]`` that returns
``(ok, reason)``. ``reason`` is empty when ``ok`` is True; otherwise a short,
prompt-ready hint that `_call_llm` appends to the next retry prompt.

Validators are intentionally permissive: they flag clearly-broken output
(missing headings, unbalanced fences, filler-heavy prose) without over-fitting.
"""

from __future__ import annotations

import re
from collections.abc import Callable

Validator = Callable[[str], tuple[bool, str]]

# Phrases the Overview prompt already warns against; repeat enforcement here.
_BANNED_PHRASES: tuple[str, ...] = (
    "crucial for",
    "under discussion",
    "actively testing",
    "it is important to note",
    "plays a vital role",
    "in today's",
    "leverage",
    "delve into",
)


def min_length(min_chars: int) -> Validator:
    def _v(content: str) -> tuple[bool, str]:
        if len(content.strip()) < min_chars:
            return False, f"Output was too short (<{min_chars} chars). Write a substantive section."
        return True, ""

    return _v


def mermaid_balanced(content: str) -> tuple[bool, str]:
    # Count ```mermaid opens vs bare ``` closes inside the section.
    opens = len(re.findall(r"^```mermaid\b", content, flags=re.MULTILINE))
    all_fences = len(re.findall(r"^```", content, flags=re.MULTILINE))
    # Each mermaid open needs a matching bare close: 2 fences per block.
    if opens * 2 > all_fences:
        return (
            False,
            "A ```mermaid block was not closed. End every mermaid block with a line containing only ```.",
        )
    return True, ""


def required_headings(headings: tuple[str, ...]) -> Validator:
    """Each entry must appear as a `## <heading>` line (case-insensitive)."""

    def _v(content: str) -> tuple[bool, str]:
        lowered = content.lower()
        missing = [h for h in headings if f"## {h.lower()}" not in lowered]
        if missing:
            return False, (
                "Missing required section heading(s): "
                + ", ".join(f"## {h}" for h in missing)
                + ". Each must appear exactly once as an H2."
            )
        return True, ""

    return _v


def banned_phrases(content: str) -> tuple[bool, str]:
    lowered = content.lower()
    hits = [p for p in _BANNED_PHRASES if p in lowered]
    if len(hits) >= 3:
        return False, (
            "Too many filler phrases ("
            + ", ".join(repr(h) for h in hits[:5])
            + "). Rewrite with concrete verbs and specific facts."
        )
    return True, ""


def combine(*validators: Validator) -> Validator:
    """Combine validators; first failure wins."""

    def _v(content: str) -> tuple[bool, str]:
        for v in validators:
            ok, reason = v(content)
            if not ok:
                return False, reason
        return True, ""

    return _v
