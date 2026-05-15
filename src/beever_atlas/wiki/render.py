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


# Pattern stripping the prompt-safety ``<untrusted>...</untrusted>``
# wrapper that ``wrap_untrusted`` adds around fact text. The wrapper
# is meant for LLM-context defense (so the model treats the wrapped
# content as data, not instructions); when the SAME text lands in a
# rendered Key Facts cell for human consumption, the tags + the
# ``<br>`` newlines are visible noise. Strip everything from the
# opening tag through the trailing newline before display.
_UNTRUSTED_WRAPPER_RE = re.compile(
    r"<untrusted>\s*(?:<br\s*/?>\s*)?(.*?)(?:<br\s*/?>\s*)?</untrusted>",
    re.IGNORECASE | re.DOTALL,
)


def _strip_untrusted_wrapper(text: str) -> str:
    """Replace ``<untrusted>...</untrusted>`` wrappers with the inner
    content. Idempotent — text without the wrapper passes through."""
    if not text or "<untrusted>" not in text.lower():
        return text
    return _UNTRUSTED_WRAPPER_RE.sub(lambda m: m.group(1).strip(), text)


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
        # Strip the prompt-safety ``<untrusted>`` wrapper before
        # display so cells don't show the raw tags + <br> markers.
        fact_text = _strip_untrusted_wrapper(
            f.get("memory_text") or f.get("fact") or f.get("text") or ""
        )
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


# ---------------------------------------------------------------------------
# llm-wiki-folder-structure — Children TOC marker rendering
# ---------------------------------------------------------------------------

CHILDREN_TOC_MARKER = "<<CHILDREN_TOC>>"


def render_children_toc(children: list[dict]) -> str:
    """Render a deterministic Markdown list of folder children.

    Each child dict needs at least ``title`` and ``slug``; ``summary``
    is optional (when present, rendered as a 1-line description after
    the link). Returns an empty string when ``children`` is empty so
    the marker substitution is safe to apply unconditionally.

    Output is plain Markdown (``- [Title](/wiki/slug) — summary``) —
    no HTML, no GFM-only features. Survives copy-out to any Markdown
    renderer.
    """
    if not children:
        return ""
    lines: list[str] = []
    for child in children:
        title = (child.get("title") or "").strip() or "Untitled"
        slug = (child.get("slug") or "").strip()
        summary = (child.get("summary") or "").strip()
        if slug:
            line = f"- [{title}](/wiki/{slug})"
        else:
            line = f"- {title}"
        if summary:
            # Trim to 200 chars at a word boundary so the rendered card
            # never shows mid-word fragments like "...architectural
            # discussions, platfo". Prefer cutting at sentence end if
            # one falls within the budget; otherwise cut at a space and
            # append an ellipsis.
            short = summary.strip()
            if len(short) > 200:
                budget = short[:200]
                # Sentence-end cut wins if there's one in the second half
                last_dot = max(
                    budget.rfind(". "),
                    budget.rfind("? "),
                    budget.rfind("! "),
                )
                if last_dot >= 100:
                    short = budget[: last_dot + 1]
                else:
                    last_space = budget.rfind(" ")
                    if last_space >= 100:
                        short = budget[:last_space].rstrip(" ,;:") + "…"
                    else:
                        short = budget.rstrip() + "…"
            line += f" — {short}"
        lines.append(line)
    return "\n".join(lines)


def apply_children_toc_marker(content: str, children: list[dict]) -> str:
    """Replace ``<<CHILDREN_TOC>>`` in ``content`` with the rendered TOC.

    If the marker is missing (LLM forgot to emit it), the rendered TOC
    is appended at the END of the content under a default heading so
    the operator can still navigate to children — robust to prompt
    drift. When ``children`` is empty, the marker is removed silently
    (no useless empty heading).
    """
    toc = render_children_toc(children)
    if CHILDREN_TOC_MARKER in content:
        if not toc:
            # Children list is empty — drop the marker line entirely.
            return _strip_marker_line(content)
        return content.replace(CHILDREN_TOC_MARKER, toc)
    if not toc:
        return content
    # Marker missing — append a fallback section at the end.
    return content.rstrip() + "\n\n## Pages in this folder\n\n" + toc + "\n"


def _strip_marker_line(content: str) -> str:
    """Remove the line containing the marker (and only that line)."""
    out: list[str] = []
    for line in content.splitlines():
        if CHILDREN_TOC_MARKER in line:
            continue
        out.append(line)
    return "\n".join(out)


__all__ = [
    "render_key_facts_table",
    "escape_gfm_cell",
    # llm-wiki-folder-structure exports
    "render_children_toc",
    "apply_children_toc_marker",
    "CHILDREN_TOC_MARKER",
    # adaptive-wiki-page-content exports
    "MODULE_MARKER_RE",
    "substitute_module_markers",
    "ModuleSubstitutionError",
]


# ---------------------------------------------------------------------------
# adaptive-wiki-page-content — module marker substitution
# ---------------------------------------------------------------------------

# Matches ``<<MODULE:id>>`` or ``<<MODULE:id:ref>>`` on its own (or
# embedded in a line). The ``id`` segment matches any module id in
# the catalog (lowercase + underscores). The optional ``ref`` suffix
# carries a per-module identifier (used by media modules to pin a
# specific media item to a marker).
MODULE_MARKER_RE = re.compile(r"<<MODULE:([a-z][a-z0-9_]*)(?::([A-Za-z0-9_-]+))?>>")


class ModuleSubstitutionError(Exception):
    """Raised when module-marker substitution finishes but the output
    still contains an unsubstituted ``<<MODULE:`` token. Callers
    catch this and fall back to the legacy renderer rather than
    shipping a half-rendered page."""


def substitute_module_markers(
    body: str,
    rendered_modules: dict[str, str],
) -> str:
    """Replace every ``<<MODULE:id>>`` / ``<<MODULE:id:ref>>`` marker
    in ``body`` with the matching entry from ``rendered_modules``.

    Lookup keys:
    - For a marker without a ref (``<<MODULE:key_facts>>``), look up
      ``rendered_modules["key_facts"]``.
    - For a marker with a ref (``<<MODULE:media_inline:m_42>>``), look
      up the composite key ``"media_inline:m_42"`` first; if missing,
      fall back to ``"media_inline"`` (the renderer is responsible for
      knowing it should switch on the ref).

    Markers that match a known catalog ID but have no rendered entry
    are stripped silently (the planner picked the module but the
    renderer returned empty content — surfacing a blank-line gap is
    visually better than emitting an unrendered marker).

    Markers whose ID is unknown to the catalog are left in place,
    triggering ``ModuleSubstitutionError`` after the scan; the caller
    falls back to the legacy renderer.
    """
    from beever_atlas.wiki.modules import is_known_module
    import logging

    _sub_logger = logging.getLogger(__name__)

    def _replace(match: re.Match[str]) -> str:
        module_id = match.group(1)
        ref = match.group(2)
        if not is_known_module(module_id):
            # Leave in place so the post-pass sees it and raises.
            return match.group(0)
        if ref:
            composite = f"{module_id}:{ref}"
            if composite in rendered_modules:
                return rendered_modules[composite]
        rendered = rendered_modules.get(module_id, "")
        if not rendered:
            # Known module ID with no rendered content. Asymmetric vs
            # the unknown-id path (which raises) — silent strip is the
            # right behavior because the planner picked a real module
            # whose data turned out empty, but log at info level so
            # soak telemetry can spot a systematic data-contract gap
            # (e.g., a renderer always returning empty due to wrong
            # input keys).
            _sub_logger.info(
                "module_substitution_empty module=%s ref=%s — marker stripped",
                module_id,
                ref or "",
            )
        return rendered

    out = MODULE_MARKER_RE.sub(_replace, body)
    if "<<MODULE:" in out:
        raise ModuleSubstitutionError(
            f"Unsubstituted module marker(s) remain after substitution pass: "
            f"{[m.group(0) for m in MODULE_MARKER_RE.finditer(out)]}"
        )
    return out
