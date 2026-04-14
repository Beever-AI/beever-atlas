"""Deterministic renderers for wiki page structured blocks (Phase 4).

`render_key_facts_table` produces a GFM table from cluster `key_facts`, replacing
the previous LLM-generated Key Facts table. `escape_gfm_cell` guarantees each
cell body parses as a single GFM table cell regardless of Unicode input.
"""

from __future__ import annotations

import re

# Zero-width characters that can sneak into LLM- or user-derived text and
# corrupt GFM table column counts in some renderers.
_ZERO_WIDTH_CHARS = ("\u200b", "\u200c", "\u200d", "\ufeff")


def escape_gfm_cell(text: str) -> str:
    """Escape `text` so it can be embedded inside a GFM table cell.

    Rules (each numbered to match the plan):
    """
    if text is None:
        return " "
    s = str(text)

    # 1. Normalize line endings: \r\n and \r -> single space.
    s = s.replace("\r\n", " ").replace("\r", " ")

    # 2. Replace \n with <br> (GFM-portable in-cell line break).
    s = s.replace("\n", "<br>")

    # 3. Replace \t with a single space (tabs can break column alignment).
    s = s.replace("\t", " ")

    # 4. Escape pipe separators as \|.
    s = s.replace("|", "\\|")

    # 5. Backslash-pipe collision: after escaping, runs like "\\\\|" (two
    # unescaped backslashes before the \| we just added) can be re-read as
    # "\\" + "|", re-exposing an unescaped pipe. Walk every occurrence of
    # "\|" and collapse any odd-length run of preceding backslashes to an
    # even-length one so the pipe is unambiguously escaped.
    out_chars: list[str] = []
    i = 0
    while i < len(s):
        # Look ahead for "\|" produced in step 4 (backslash followed by pipe).
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            # Count preceding backslashes already in out_chars (before this
            # escape backslash itself).
            j = len(out_chars) - 1
            run = 0
            while j >= 0 and out_chars[j] == "\\":
                run += 1
                j -= 1
            # The current backslash will be appended too — combined with the
            # preceding run, the backslashes immediately before the pipe will
            # number (run + 1). We need that total to be odd (so one backslash
            # escapes the pipe, and all other backslashes are themselves
            # escaped pairwise). If (run + 1) is even, insert one extra
            # backslash to restore odd parity.
            if (run + 1) % 2 == 0:
                out_chars.append("\\")
            out_chars.append("\\")
            out_chars.append("|")
            i += 2
            continue
        out_chars.append(s[i])
        i += 1
    s = "".join(out_chars)

    # 6. Strip zero-width characters.
    for zw in _ZERO_WIDTH_CHARS:
        s = s.replace(zw, "")

    # 7. Trim surrounding whitespace produced by prior normalizations.
    s = s.strip()

    # 8. Empty cell -> single space (empty cells collapse in GFM renderers
    # and break column counts).
    if not s:
        return " "
    return s


def render_key_facts_table(facts: list[dict], max_rows: int = 8) -> str:
    """Render a deterministic GFM Key Facts table.

    Columns: Fact | Source | Type | Importance
    Returns empty string if `facts` is empty.
    Sorted by `importance` desc, then `quality_score` desc.
    """
    if not facts:
        return ""

    def _imp(f: dict) -> float:
        v = f.get("importance", 0)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _qs(f: dict) -> float:
        v = f.get("quality_score", 0)
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    ranked = sorted(facts, key=lambda f: (_imp(f), _qs(f)), reverse=True)
    rows = ranked[:max_rows]

    header = "| Fact | Source | Type | Importance |"
    sep = "|------|--------|------|------------|"
    lines = [header, sep]
    for f in rows:
        fact_text = f.get("memory_text") or f.get("fact") or f.get("text") or ""
        source = f.get("author_name") or f.get("source") or ""
        ftype = f.get("fact_type") or f.get("type") or ""
        importance = f.get("importance", "")
        # Render importance numerically when possible.
        if isinstance(importance, (int, float)):
            importance_str = f"{importance:g}"
        else:
            importance_str = str(importance) if importance is not None else ""
        lines.append(
            "| "
            + escape_gfm_cell(fact_text)
            + " | "
            + escape_gfm_cell(source)
            + " | "
            + escape_gfm_cell(ftype)
            + " | "
            + escape_gfm_cell(importance_str)
            + " |"
        )
    return "\n".join(lines)


__all__ = ["render_key_facts_table", "escape_gfm_cell"]
