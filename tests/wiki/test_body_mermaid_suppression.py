"""Tests for ``_strip_noisy_body_mermaid`` (P3).

The Round-7 module-level suppression drops the ``entity_diagram``
MODULE when the graph is one-pair-with-synonymous-verbs noise, but the
v3 prompt also instructs the LLM to write a Mermaid block directly
into the BODY markdown — which bypasses module suppression. This
post-substitution pass scans the body for ``mermaid`` blocks and drops
any block matching the same noise heuristic.
"""

from __future__ import annotations

import logging

import pytest

from beever_atlas.wiki.modules.orchestrator import (
    _analyze_mermaid_block,
    _strip_noisy_body_mermaid,
)


class _ListHandler(logging.Handler):
    """Capture log records to a local list — caplog can miss records when
    the logger's propagation chain is intercepted by app-level structured
    handlers. Attaching directly to the orchestrator logger sidesteps
    that. ``records`` exposes the formatted message string for each
    captured record.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        self.records.append(record.getMessage())


@pytest.fixture
def orch_logs():
    """Yield a captured-message list bound to the orchestrator logger."""
    handler = _ListHandler()
    logger = logging.getLogger("beever_atlas.wiki.modules.orchestrator")
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)


def test_clean_mermaid_block_with_distinct_verbs_is_kept():
    body = (
        "Some prose.\n\n"
        "```mermaid\n"
        "graph TD\n"
        "  A -->|uses| B\n"
        "  B -->|investigates| C\n"
        "  C -->|owns| D\n"
        "```\n\n"
        "More prose.\n"
    )
    out = _strip_noisy_body_mermaid(body, page_id="test")
    assert "```mermaid" in out
    assert "uses" in out
    assert "investigates" in out
    assert "Some prose." in out
    assert "More prose." in out


def test_noisy_mermaid_one_pair_one_verb_is_stripped(orch_logs):
    """Same source/target pair across many edges + only one verb =
    noise. The whole fenced block must be dropped + telemetry logged."""
    edges = "\n".join("  A -->|owns| B" for _ in range(12))
    body = f"Prose before.\n\n```mermaid\ngraph TD\n{edges}\n```\n\nProse after.\n"
    out = _strip_noisy_body_mermaid(body, page_id="test-page")

    # Mermaid block gone, surrounding prose kept.
    assert "```mermaid" not in out
    assert "Prose before." in out
    assert "Prose after." in out
    # Structured telemetry emitted.
    assert any(
        "body_mermaid_suppressed" in m and "dominant_pair_one_verb" in m and "test-page" in m
        for m in orch_logs
    ), f"no matching log among {orch_logs}"


def test_empty_mermaid_block_is_stripped(orch_logs):
    """A mermaid block with no ``-->`` edges is structureless and gets
    stripped just like noisy blocks."""
    body = "Header.\n\n```mermaid\ngraph TD\n  A\n  B\n```\n\nFooter.\n"
    out = _strip_noisy_body_mermaid(body, page_id="empty-test")
    assert "```mermaid" not in out
    assert "Header." in out
    assert "Footer." in out
    assert any("body_mermaid_suppressed" in m and "no_edges" in m for m in orch_logs), (
        f"no matching log among {orch_logs}"
    )


def test_non_mermaid_code_blocks_are_left_alone():
    """Code blocks fenced as e.g. ``python`` must be preserved verbatim
    — the rule only applies to ``mermaid``-tagged blocks."""
    body = "Pre.\n\n```python\n# this is a code block, not mermaid\nx = 1\n```\n\nPost.\n"
    out = _strip_noisy_body_mermaid(body, page_id="code-test")
    # Original ```python block must survive untouched.
    assert "```python" in out
    assert "x = 1" in out
    assert "Pre." in out
    assert "Post." in out


def test_clean_block_with_no_labels_kept_when_pair_count_low():
    """Mermaid block with unlabelled edges between many distinct pairs
    is structurally clean (not the dominant-pair noise pattern) so it
    must be kept even though distinct_verbs == 0."""
    body = "```mermaid\ngraph TD\n  A --> B\n  C --> D\n  E --> F\n```\n"
    out = _strip_noisy_body_mermaid(body, page_id="t")
    assert "```mermaid" in out
    assert "A --> B" in out


def test_analyze_helper_counts_pairs_correctly():
    inner = "graph TD\n  A -->|uses| B\n  A -->|investigates| B\n  A -->|owns| B\n"
    total, distinct, max_pair = _analyze_mermaid_block(inner)
    assert total == 3
    assert distinct == 3
    assert max_pair == 3


def test_analyze_helper_detects_zero_edges():
    inner = "graph TD\n  A\n  B\n"
    total, distinct, max_pair = _analyze_mermaid_block(inner)
    assert total == 0
    assert distinct == 0
    assert max_pair == 0
