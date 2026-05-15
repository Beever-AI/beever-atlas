"""Tests for the ``<untrusted>`` wrapper stripping in
``render_key_facts_table``. Without this, the rendered table cells
display the prompt-safety wrapper tags + ``<br>`` markers verbatim,
which leaked into the production wiki export.
"""

from __future__ import annotations

from beever_atlas.wiki.render import render_key_facts_table


def test_strip_untrusted_basic_wrapper() -> None:
    """``<untrusted>...</untrusted>`` is stripped; inner text remains."""
    facts = [
        {
            "memory_text": "<untrusted>\nAdopt JWT for service auth.\n</untrusted>",
            "fact_type": "decision",
            "importance": 9,
            "author_name": "Alice",
        }
    ]
    out = render_key_facts_table(facts)
    assert "<untrusted>" not in out
    assert "</untrusted>" not in out
    assert "Adopt JWT for service auth." in out


def test_strip_untrusted_with_br_separator() -> None:
    """The wrapper that ``wrap_untrusted`` produces uses ``<br>`` as
    a newline separator inside the tags. Both leading and trailing
    ``<br>`` are stripped."""
    facts = [
        {
            "memory_text": "<untrusted><br>JWT replaces SAML<br></untrusted>",
            "fact_type": "decision",
            "importance": 8,
            "author_name": "Bob",
        }
    ]
    out = render_key_facts_table(facts)
    assert "<untrusted>" not in out
    assert "<br>" not in out  # also stripped to keep cells clean
    assert "JWT replaces SAML" in out


def test_strip_untrusted_idempotent_on_unwrapped_text() -> None:
    """Text without the wrapper passes through unchanged — must NOT
    introduce double-strips or partial matches."""
    facts = [
        {
            "memory_text": "Plain fact without wrappers.",
            "fact_type": "event",
            "importance": 5,
            "author_name": "Charlie",
        }
    ]
    out = render_key_facts_table(facts)
    assert "Plain fact without wrappers." in out


def test_strip_untrusted_multiple_facts_independent() -> None:
    """Each cell strips independently — one wrapped + one unwrapped
    fact in the same table both render correctly."""
    facts = [
        {
            "memory_text": "<untrusted>\nWrapped fact A.\n</untrusted>",
            "fact_type": "decision",
            "importance": 9,
            "author_name": "A",
        },
        {
            "memory_text": "Plain fact B.",
            "fact_type": "event",
            "importance": 7,
            "author_name": "B",
        },
    ]
    out = render_key_facts_table(facts)
    assert "Wrapped fact A." in out
    assert "Plain fact B." in out
    assert "<untrusted>" not in out
