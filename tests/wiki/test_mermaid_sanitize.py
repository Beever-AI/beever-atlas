"""Tests for mermaid undefined-node-reference pruning.

A real compile produced ``EEACBA -->|drives| ELLMS`` where ``EEACBA`` was a
typo of ``EACBA`` and never defined as ``ID[Label]`` in the same block. The
post-processor should drop such edges (gated on ``wiki_parse_hardening``).
"""

from __future__ import annotations

from beever_atlas.wiki.compiler import WikiCompiler


def _block(body: str) -> str:
    return f"```mermaid\n{body}\n```\n"


def test_drops_edge_with_undefined_source() -> None:
    src = _block("graph TD\nEACBA[Foo]\nELLMS[Bar]\nEEACBA -->|drives| ELLMS")
    out = WikiCompiler._postprocess_content(src)
    assert "EEACBA" not in out
    assert "EACBA[Foo]" in out
    assert "ELLMS[Bar]" in out


def test_drops_edge_with_undefined_target() -> None:
    src = _block("graph TD\nEACBA[Foo]\nELLMS[Bar]\nEACBA -->|drives| EXYZQ")
    out = WikiCompiler._postprocess_content(src)
    assert "EXYZQ" not in out
    assert "EACBA[Foo]" in out
    assert "ELLMS[Bar]" in out


def test_keeps_valid_edges() -> None:
    src = _block("graph TD\nA[Alpha]\nB[Beta]\nC[Gamma]\nA -->|x| B\nB --> C")
    out = WikiCompiler._postprocess_content(src)
    assert "A -->|x| B" in out
    assert "B --> C" in out


def test_multiple_blocks_independent() -> None:
    src = (
        _block("graph TD\nA[Alpha]\nB[Beta]\nA --> B\nA --> Z")
        + "\nSome prose.\n\n"
        + _block("graph TD\nX[Ex]\nY[Why]\nX --> Y\nQ --> Y")
    )
    out = WikiCompiler._postprocess_content(src)
    # Block 1: Z is undefined → dropped. A --> B kept.
    assert "A --> B" in out
    assert "A --> Z" not in out
    # Block 2: Q is undefined → dropped. X --> Y kept.
    assert "X --> Y" in out
    assert "Q --> Y" not in out


def test_no_mermaid_block_noop() -> None:
    src = "# Heading\n\nSome text with A --> B that is not fenced.\n"
    out = WikiCompiler._postprocess_content(src)
    # Non-mermaid content passes through (trailing newline normalization aside).
    assert "A --> B" in out
    assert "# Heading" in out


def test_edge_with_inline_bracket_labels_kept() -> None:
    # Edge where endpoints carry bracketed labels inline — they count as
    # definitions, so the edge should survive.
    src = _block("graph TD\nA[Foo] -->|x| B[Bar]")
    out = WikiCompiler._postprocess_content(src)
    assert "A[Foo]" in out
    assert "B[Bar]" in out


def test_block_with_only_undefined_edges_keeps_definitions() -> None:
    # After pruning edges, node definitions remain (block is not removed).
    src = _block("graph TD\nA[Foo]\nB[Bar]\nZ --> Q")
    out = WikiCompiler._postprocess_content(src)
    assert "A[Foo]" in out
    assert "B[Bar]" in out
    assert "Z --> Q" not in out
