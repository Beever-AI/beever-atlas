"""Shared text utilities for module data builders.

The orchestrator wraps fact text in ``<untrusted>...</untrusted>``
delimiters before feeding it to the planner/writer LLM (defense-in-
depth against prompt injection — see
``beever_atlas.agents.prompt_safety``). When the SAME text flows
through to the frontend (module ``data`` payloads consumed by React
components), those wrappers must be stripped — otherwise readers see
literal ``<untrusted>`` prefixes on every card.

``_strip_safety_markers`` removes every safety wrapper used in this
codebase. It is:
  - Idempotent — calling twice produces the same output as once.
  - Cheap — pure ``str.replace`` calls, no regex backtracking, O(n).
  - Defensive — never raises on non-string input; coerces to str.

Apply at every place fact text crosses from "LLM context" to
"frontend display": ``build_key_facts_data``, ``build_hero_summary_data``,
``build_decision_banner_data``, ``build_provenance_drawer_data``,
``build_stat_strip_data``, etc.
"""

from __future__ import annotations

# Tags we wrap untrusted external content in before passing to the LLM.
# Both ``wrap_untrusted`` (the canonical wrapper) and any other safety
# wrappers we add later go in this list. Order doesn't matter — each
# tag is stripped independently with ``str.replace``.
_SAFETY_TAGS: tuple[str, ...] = (
    "<untrusted>",
    "</untrusted>",
    "<sanitized>",
    "</sanitized>",
    "<external>",
    "</external>",
)


def _strip_safety_markers(text: object) -> str:
    """Strip prompt-safety wrapper tags from ``text``.

    Removes literal ``<untrusted>``/``</untrusted>``/``<sanitized>``/
    ``</sanitized>``/``<external>``/``</external>`` substrings, then
    trims leading/trailing whitespace. Non-string input is coerced to
    str (``None`` becomes ``""``).

    Idempotent: ``f(f(x)) == f(x)``. Cheap: a few ``str.replace`` calls
    per invocation; no regex.

    Examples:
        >>> _strip_safety_markers("<untrusted>\\nhello\\n</untrusted>")
        'hello'
        >>> _strip_safety_markers("plain text")
        'plain text'
        >>> _strip_safety_markers(None)
        ''
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return ""
    # Fast path — no safety markers, just return trimmed.
    if "<" not in text:
        return text.strip()
    out = text
    for tag in _SAFETY_TAGS:
        if tag in out:
            out = out.replace(tag, "")
    return out.strip()
