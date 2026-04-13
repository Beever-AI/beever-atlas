"""Language-aware text utilities for entity dedup & rendering.

Keeps the NFC normalization + Latin-only lowercasing rule in a single place
so the entity registry, graph adapters, and wiki/QA layers agree.
"""

from __future__ import annotations

import unicodedata


def _is_latin_only(text: str) -> bool:
    """Return True if every alphabetic char in `text` is basic Latin (a-zA-Z)."""
    for ch in text:
        if ch.isalpha() and not ("A" <= ch.upper() <= "Z"):
            return False
    return True


def nfc_key(name: str) -> str:
    """Canonical lookup key for an entity name.

    - Unicode NFC-normalize (so é as one codepoint == é as two).
    - Strip leading/trailing whitespace.
    - Lowercase ONLY when the string is entirely Latin script. CJK/Hangul/etc.
      are case-less in the intended sense — lowercasing them is a no-op that
      we skip to preserve exact codepoint comparison behavior.
    """
    if not name:
        return ""
    normalized = unicodedata.normalize("NFC", name).strip()
    if _is_latin_only(normalized):
        return normalized.lower()
    return normalized


def alias_keyset(name: str, aliases: list[str] | None = None) -> set[str]:
    """Return the full set of normalized lookup keys for an entity."""
    keys: set[str] = set()
    if name:
        keys.add(nfc_key(name))
    for a in aliases or []:
        k = nfc_key(a)
        if k:
            keys.add(k)
    return keys
