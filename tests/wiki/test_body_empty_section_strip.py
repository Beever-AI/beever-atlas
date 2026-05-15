"""Tests for ``_strip_empty_frontend_section_headers`` (P4).

The v3 prompt's body-authoring rules tell the LLM to leave headers
like ``### Terms Used`` or ``### Source Messages`` in the body —
those would normally be replaced by the matching frontend module's
content. But the React dispatcher renders those modules itself; the
header in the markdown body has no corresponding content, leaving an
empty trailing section.

This pass strips those empty section headers post-substitution so the
exported markdown does not show empty frontend-module headers.
"""

from __future__ import annotations

import logging

import pytest

from beever_atlas.wiki.modules.orchestrator import (
    _strip_empty_frontend_section_headers,
)


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        self.records.append(record.getMessage())


@pytest.fixture
def orch_logs():
    """Capture orchestrator logger records — caplog can miss records
    when the app's structured handler intercepts them."""
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


def test_empty_terms_used_header_is_stripped(orch_logs):
    body = "## Page\n\nBody content.\n\n### Terms Used\n\n\n### Sources\n\n- Some source\n"
    out = _strip_empty_frontend_section_headers(body, page_id="test-page")

    assert "### Terms Used" not in out
    # ``### Sources`` is NOT a frontend-only header → must survive.
    assert "### Sources" in out
    assert "- Some source" in out
    # Structured telemetry emitted.
    assert any(
        "body_empty_section_stripped" in m and "Terms Used" in m and "test-page" in m
        for m in orch_logs
    ), f"no matching log among {orch_logs}"


def test_section_with_actual_content_is_kept():
    """If the frontend-only section header has content, leave it
    alone — the prompt may have authored a body for it. Only EMPTY
    sections get stripped."""
    body = (
        "## Page\n\n"
        "### Terms Used\n\n"
        "API: application programming interface.\n\n"
        "### Next section\n\n"
        "More content.\n"
    )
    out = _strip_empty_frontend_section_headers(body, page_id="t")
    assert "### Terms Used" in out
    assert "API: application programming interface." in out


def test_non_frontend_section_followed_by_content_is_kept():
    body = "## Page\n\n### Random User Header\n\nWhatever.\n"
    out = _strip_empty_frontend_section_headers(body, page_id="t")
    assert "### Random User Header" in out
    assert "Whatever." in out


def test_non_frontend_empty_section_is_kept():
    """A header NOT in the frontend-only allowlist must NOT be touched
    even when its body is empty — we don't know what the LLM intended."""
    body = "## Page\n\n### Random User Header\n\n\n### Next\n\nContent.\n"
    out = _strip_empty_frontend_section_headers(body, page_id="t")
    assert "### Random User Header" in out


def test_empty_source_messages_header_stripped(orch_logs):
    body = "## Page\n\nBody.\n\n### Source Messages\n\n\n\n### Other\n\nOther content.\n"
    out = _strip_empty_frontend_section_headers(body, page_id="src-test")
    assert "### Source Messages" not in out
    assert "### Other" in out
    assert "Other content." in out
    assert any("body_empty_section_stripped" in m and "Source Messages" in m for m in orch_logs), (
        f"no matching log among {orch_logs}"
    )


def test_empty_section_at_end_of_content_stripped():
    """The lookahead handles end-of-content too — a frontend-only
    header at the tail with nothing after it is considered empty."""
    body = "## Page\n\nBody.\n\n### Terms Used\n\n\n"
    out = _strip_empty_frontend_section_headers(body, page_id="t")
    assert "### Terms Used" not in out
    assert "Body." in out


def test_no_change_for_body_without_h3_headers():
    body = "## Page\n\nNo h3 headings here.\n"
    out = _strip_empty_frontend_section_headers(body, page_id="t")
    assert out == body
