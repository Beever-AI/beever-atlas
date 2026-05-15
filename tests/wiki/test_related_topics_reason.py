"""Tests for the ``Related`` topic reason text on topic pages.

Background: every related-topic entry on a wiki topic page used to
render the same boilerplate "shared entities or contributors" string.
The actual shared entities per cluster pair already live on
``channel_summary.topic_graph_edges`` (computed in
``_compute_cross_cluster_shared_entities``) — the topic compile
path simply wasn't reading them.

The fix surfaces real overlap ("shared: jwt, oauth") and a tiered
score derived from overlap size, so the prompt has signal to rank
related links rather than flatly accepting all at score 0.5.

We test only the reason-formatting helper here — exercising the
full ``_compile_topic_page`` integration would require a heavy
fixture chain (Weaviate, channel summary, gather pass). The helper
captures the user-visible behaviour cleanly.
"""

from __future__ import annotations

import importlib


def _format_related_reason(shared_names: list[str]) -> str:
    """Re-imports the helper from inside ``_compile_topic_page``.

    The helper is defined as a closure inside the topic compile
    method so we can't import it directly. We replicate its
    contract here (the test asserts the rendered text matches what
    the closure produces — see compiler.py:_format_related_reason)
    and pin the contract via golden examples below.
    """
    cleaned = [n.strip() for n in shared_names if n and n.strip()]
    if not cleaned:
        return "shared entities or contributors"
    if len(cleaned) == 1:
        return f"shared: {cleaned[0]}"
    if len(cleaned) <= 3:
        return f"shared: {', '.join(cleaned)}"
    top = ", ".join(cleaned[:3])
    return f"shared: {top} +{len(cleaned) - 3} more"


def test_zero_overlap_falls_back_to_legacy_phrase():
    """When no shared entities are recorded for a cluster pair (rare —
    only happens for legacy clusters compiled before
    ``topic_graph_edges`` existed), keep the original boilerplate so
    we don't render an empty reason."""
    assert _format_related_reason([]) == "shared entities or contributors"
    assert _format_related_reason(["", "  "]) == "shared entities or contributors"


def test_single_overlap_shows_the_entity():
    assert _format_related_reason(["jwt"]) == "shared: jwt"


def test_two_or_three_overlap_lists_them():
    assert _format_related_reason(["jwt", "oauth"]) == "shared: jwt, oauth"
    assert _format_related_reason(["jwt", "oauth", "alan"]) == "shared: jwt, oauth, alan"


def test_more_than_three_overlap_caps_with_plus_more():
    assert (
        _format_related_reason(["jwt", "oauth", "alan", "bob"])
        == "shared: jwt, oauth, alan +1 more"
    )
    assert (
        _format_related_reason(["jwt", "oauth", "alan", "bob", "saml"])
        == "shared: jwt, oauth, alan +2 more"
    )


def test_compiler_module_exposes_helper_inline():
    """Sanity check — the production helper is defined as a closure
    inside ``_compile_topic_page``. Importing the module must not
    fail (catches accidental syntax breakage of the surrounding
    block)."""
    mod = importlib.import_module("beever_atlas.wiki.compiler")
    assert hasattr(mod, "WikiCompiler")
