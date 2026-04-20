"""Tests for out-of-range inline citation stripping.

The LLM occasionally emits `[N]` markers beyond the supplied citation list
(e.g. references `[36]` when only 12 facts were provided). Those dangling
markers pollute body text even after the citations list is trimmed.
"""

from beever_atlas.wiki.compiler import WikiCompiler


def test_strips_single_out_of_range():
    out = WikiCompiler._strip_out_of_range_inline_citations(
        "Fact A [3]. Fact B [99]. Fact C [2].", max_index=12
    )
    assert "[99]" not in out
    assert "[3]" in out
    assert "[2]" in out


def test_reduces_grouped_citations():
    out = WikiCompiler._strip_out_of_range_inline_citations("Claim [1, 36, 7, 99].", max_index=12)
    assert "[1, 7]" in out
    assert "36" not in out and "99" not in out


def test_removes_group_entirely_when_all_invalid():
    out = WikiCompiler._strip_out_of_range_inline_citations(
        "Unfounded claim [36, 99] here.", max_index=12
    )
    assert "[36" not in out
    assert "[99" not in out
    assert "Unfounded claim here." in out


def test_noop_when_max_index_zero_or_empty():
    assert WikiCompiler._strip_out_of_range_inline_citations("hi [3]", 0) == "hi [3]"
    assert WikiCompiler._strip_out_of_range_inline_citations("", 12) == ""


def test_preserves_non_numeric_brackets():
    out = WikiCompiler._strip_out_of_range_inline_citations(
        "See [linked text](url) and [1].", max_index=2
    )
    assert "[linked text]" in out
    assert "[1]" in out


def test_boundary_index():
    out = WikiCompiler._strip_out_of_range_inline_citations("[1][12][13]", max_index=12)
    assert "[1]" in out
    assert "[12]" in out
    assert "[13]" not in out
