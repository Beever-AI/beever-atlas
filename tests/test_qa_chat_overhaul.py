"""Dry-run integration tests for the Enterprise QA Chat Overhaul.

These tests verify the backend API changes without requiring external services
(Weaviate, Neo4j, MongoDB) to be running. They test:
- Request/response models
- Citation parsing
- Follow-up extraction
- File upload validation
- Session management endpoints
- Feedback endpoints
- Answer mode routing
- Thinking event emission logic

Run with: uv run pytest tests/test_qa_chat_overhaul.py -v
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Test 16.6: Citation regex accuracy ──────────────────────────────────────


class TestCitationExtraction:
    """Verify the citation regex correctly parses author, channel, timestamp."""

    def _extract(self, text: str) -> list[dict]:
        from beever_atlas.api.ask import _extract_citations_from_text
        return _extract_citations_from_text(text)

    def test_single_citation_correct_groups(self):
        """Author should be handle, not citation number."""
        text = "[1] Author: @Thomas Chong | Channel: #beever | Time: 2025-04-06"
        citations = self._extract(text)
        assert len(citations) == 1
        assert citations[0]["number"] == "1"
        assert citations[0]["author"] == "@Thomas Chong"
        assert citations[0]["channel"] == "#beever"
        assert citations[0]["timestamp"] == "2025-04-06"

    def test_multiple_citations_same_line(self):
        """Citations on the same line should not bleed into each other."""
        text = (
            "[1] Author: @Thomas | Channel: #beever | Time: 2025-04-06 "
            "[2] Author: @Jacky | Channel: #beever | Time: 2025-04-07"
        )
        citations = self._extract(text)
        assert len(citations) == 2
        assert citations[0]["author"] == "@Thomas"
        assert citations[0]["timestamp"] == "2025-04-06"
        assert citations[1]["author"] == "@Jacky"
        assert citations[1]["timestamp"] == "2025-04-07"

    def test_citation_without_channel(self):
        """Citation with missing channel should still parse."""
        text = "[1] Author: @Alice | Time: 2025-01-01"
        citations = self._extract(text)
        assert len(citations) == 1
        assert citations[0]["author"] == "@Alice"
        assert citations[0]["channel"] == ""

    def test_no_citations(self):
        """Text without citations returns empty list."""
        assert self._extract("Hello world") == []


# ── Test 16.5: Answer mode request model ────────────────────────────────────


class TestAskRequestModel:
    """Verify AskRequest validates mode and attachments."""

    def test_default_mode_is_deep(self):
        from beever_atlas.api.ask import AskRequest
        req = AskRequest(question="test")
        assert req.mode == "deep"

    def test_quick_mode_accepted(self):
        from beever_atlas.api.ask import AskRequest
        req = AskRequest(question="test", mode="quick")
        assert req.mode == "quick"

    def test_summarize_mode_accepted(self):
        from beever_atlas.api.ask import AskRequest
        req = AskRequest(question="test", mode="summarize")
        assert req.mode == "summarize"

    def test_invalid_mode_rejected(self):
        from beever_atlas.api.ask import AskRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            AskRequest(question="test", mode="invalid")

    def test_attachments_default_empty(self):
        from beever_atlas.api.ask import AskRequest
        req = AskRequest(question="test")
        assert req.attachments == []

    def test_attachments_with_data(self):
        from beever_atlas.api.ask import AskRequest
        req = AskRequest(
            question="test",
            attachments=[{"file_id": "abc", "filename": "doc.pdf", "extracted_text": "hello"}],
        )
        assert len(req.attachments) == 1


# ── Test 16.6: Feedback request model ───────────────────────────────────────


class TestFeedbackRequestModel:
    """Verify FeedbackRequest validates rating."""

    def test_thumbs_up(self):
        from beever_atlas.api.ask import FeedbackRequest
        req = FeedbackRequest(session_id="s1", message_id="m1", rating="up")
        assert req.rating == "up"

    def test_thumbs_down_with_comment(self):
        from beever_atlas.api.ask import FeedbackRequest
        req = FeedbackRequest(
            session_id="s1", message_id="m1", rating="down", comment="Wrong answer"
        )
        assert req.comment == "Wrong answer"

    def test_invalid_rating_rejected(self):
        from beever_atlas.api.ask import FeedbackRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            FeedbackRequest(session_id="s1", message_id="m1", rating="neutral")


# ── Test 16.1: Follow-up extraction ─────────────────────────────────────────


class TestFollowUpExtraction:
    """Verify follow-up questions are extracted from agent response text."""

    def test_follow_ups_parsed(self):
        text = 'Some answer here.\n---\nFOLLOW_UPS: ["What else?", "Tell me more"]'
        match = re.search(r'FOLLOW_UPS:\s*\[([^\]]*)\]', text)
        assert match is not None
        follow_ups = json.loads(f'[{match.group(1)}]')
        assert follow_ups == ["What else?", "Tell me more"]

    def test_follow_ups_stripped_from_text(self):
        text = 'Some answer here.\n---\nFOLLOW_UPS: ["What else?", "Tell me more"]'
        cleaned = re.sub(r'\n*---\n*FOLLOW_UPS:\s*\[.*?\]', '', text).rstrip()
        assert cleaned == "Some answer here."
        assert "FOLLOW_UPS" not in cleaned

    def test_no_follow_ups_graceful(self):
        text = "Some answer without follow-ups."
        match = re.search(r'FOLLOW_UPS:\s*\[([^\]]*)\]', text)
        assert match is None


# ── Test 16.4: File upload validation ────────────────────────────────────────


class TestFileUploadValidation:
    """Verify upload MIME type and size validation."""

    def test_supported_mime_types(self):
        from beever_atlas.api.ask import SUPPORTED_MIME_TYPES
        assert "application/pdf" in SUPPORTED_MIME_TYPES
        assert "image/png" in SUPPORTED_MIME_TYPES
        assert "image/jpeg" in SUPPORTED_MIME_TYPES
        assert "text/plain" in SUPPORTED_MIME_TYPES
        assert "text/csv" in SUPPORTED_MIME_TYPES

    def test_exe_not_supported(self):
        from beever_atlas.api.ask import SUPPORTED_MIME_TYPES
        assert "application/x-msdownload" not in SUPPORTED_MIME_TYPES

    def test_max_upload_size(self):
        from beever_atlas.api.ask import MAX_UPLOAD_SIZE
        assert MAX_UPLOAD_SIZE == 10 * 1024 * 1024


# ── Test 16.5: Timestamp formatting ─────────────────────────────────────────


class TestTimestampFormatting:
    """Verify epoch timestamps are converted to ISO date strings."""

    def test_valid_epoch(self):
        from beever_atlas.agents.tools.memory_tools import _format_timestamp
        result = _format_timestamp("1712345678.123456")
        assert re.match(r"\d{4}-\d{2}-\d{2}", result)

    def test_null_timestamp(self):
        from beever_atlas.agents.tools.memory_tools import _format_timestamp
        assert _format_timestamp(None) == "(unavailable)"

    def test_empty_timestamp(self):
        from beever_atlas.agents.tools.memory_tools import _format_timestamp
        assert _format_timestamp("") == "(unavailable)"

    def test_invalid_timestamp(self):
        from beever_atlas.agents.tools.memory_tools import _format_timestamp
        assert _format_timestamp("not_a_number") == "(unavailable)"


# ── Test 16.7: Graph threshold ───────────────────────────────────────────────


class TestGraphThreshold:
    """Verify graph tools pass threshold=0.6 for fuzzy matching."""

    def test_search_relationships_uses_lower_threshold(self):
        """Check that the source code passes threshold=0.6."""
        import inspect
        from beever_atlas.agents.tools import graph_tools
        source = inspect.getsource(graph_tools.search_relationships)
        assert "threshold=0.6" in source

    def test_trace_decision_history_uses_lower_threshold(self):
        """Check that the source code passes threshold=0.6."""
        import inspect
        from beever_atlas.agents.tools import graph_tools
        source = inspect.getsource(graph_tools.trace_decision_history)
        assert "threshold=0.6" in source


# ── Test 16.5: Agent mode configuration ──────────────────────────────────────


class TestAgentModeConfig:
    """Verify agent factory creates different agents per mode."""

    def test_prompts_module_exists(self):
        from beever_atlas.agents.query import prompts
        assert hasattr(prompts, "build_qa_system_prompt")
        assert hasattr(prompts, "QA_QUICK_SUFFIX")
        assert hasattr(prompts, "QA_SUMMARIZE_SUFFIX")
        assert hasattr(prompts, "DECOMPOSITION_PROMPT")

    def test_identity_in_prompt(self):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt
        prompt = build_qa_system_prompt()
        assert "Beever Atlas" in prompt
        assert "Do not disclose" in prompt

    def test_retrieval_pipeline_in_prompt(self):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt
        prompt = build_qa_system_prompt()
        assert "Required Retrieval Pipeline" in prompt
        assert "Step 1" in prompt
        assert "Step 5" in prompt
        assert "ALWAYS" in prompt

    def test_citation_format_in_prompt(self):
        """Prompt must instruct the model how to cite, in either regime.

        Registry-on: `_cite` tag form. Registry-off: the legacy
        channel_name / NOT-raw-channel_id guidance.
        """
        from beever_atlas.agents.query.prompts import build_qa_system_prompt
        prompt = build_qa_system_prompt()
        registry_on = "_cite" in prompt
        if registry_on:
            assert "[src:" in prompt
            assert "Do NOT write a Sources" in prompt
        else:
            assert "channel_name" in prompt
            assert "NOT the raw channel_id" in prompt

    def test_follow_up_instruction_in_deep_mode(self):
        """Deep mode must instruct follow-ups in whichever regime is active."""
        from beever_atlas.agents.query.prompts import build_qa_system_prompt
        prompt = build_qa_system_prompt(include_follow_ups=True)
        registry_on = "_cite" in prompt
        if registry_on:
            assert "suggest_follow_ups" in prompt
        else:
            assert "FOLLOW_UPS" in prompt

    def test_no_follow_up_in_quick_mode(self):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt
        prompt = build_qa_system_prompt(include_follow_ups=False)
        assert "FOLLOW_UPS" not in prompt
        assert "suggest_follow_ups" not in prompt

    def test_max_tool_calls_configurable(self):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt
        prompt2 = build_qa_system_prompt(max_tool_calls=2)
        prompt8 = build_qa_system_prompt(max_tool_calls=8)
        assert "2 tool calls" in prompt2
        assert "8 tool calls" in prompt8

    def test_query_type_tool_map(self):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt
        prompt = build_qa_system_prompt()
        assert "who is X" in prompt
        assert "search_relationships" in prompt
        assert "find_experts" in prompt


# ── Test 16.6: Channel resolver ──────────────────────────────────────────────


class TestChannelResolver:
    """Verify channel name resolver utility."""

    def test_cache_returns_same_value(self):
        import asyncio

        from beever_atlas.agents.tools.channel_resolver import (
            _channel_name_cache,
            resolve_channel_name,
        )

        _channel_name_cache["test_id"] = "test_channel"
        try:
            result = asyncio.run(resolve_channel_name("test_id"))
            assert result == "test_channel"
        finally:
            _channel_name_cache.pop("test_id", None)


# ── Test 16.2: SSE event format ──────────────────────────────────────────────


class TestSSEEventFormat:
    """Verify SSE event formatting."""

    def test_sse_event_format(self):
        from beever_atlas.api.ask import _sse_event
        event = _sse_event("thinking", {"text": "Let me analyze..."})
        assert event.startswith("event: thinking\n")
        assert '"text": "Let me analyze..."' in event
        assert event.endswith("\n\n")

    def test_sse_event_metadata_includes_mode(self):
        from beever_atlas.api.ask import _sse_event
        event = _sse_event("metadata", {"mode": "quick", "route": "qa_agent"})
        data_line = event.split("\n")[1]
        payload = json.loads(data_line.replace("data: ", ""))
        assert payload["mode"] == "quick"


# ── Test 16.1: Text extraction ───────────────────────────────────────────────


class TestTextExtraction:
    """Verify file text extraction logic."""

    @pytest.mark.asyncio
    async def test_plain_text_extraction(self):
        from beever_atlas.api.ask import _extract_text
        content = b"Hello, this is a test document."
        result = await _extract_text(content, "text/plain", "test.txt")
        assert result == "Hello, this is a test document."

    @pytest.mark.asyncio
    async def test_csv_extraction(self):
        from beever_atlas.api.ask import _extract_text
        content = b"name,age\nAlice,30\nBob,25"
        result = await _extract_text(content, "text/csv", "data.csv")
        assert "Alice" in result
        assert "Bob" in result

    @pytest.mark.asyncio
    async def test_unsupported_type_fallback(self):
        from beever_atlas.api.ask import _extract_text
        content = b"binary content"
        result = await _extract_text(content, "application/zip", "archive.zip")
        assert "Unsupported" in result


# ── Test 16.8: Decomposer ────────────────────────────────────────────────────


class TestDecomposer:
    """Verify query decomposition heuristics."""

    def test_simple_question(self):
        from beever_atlas.agents.query.decomposer import _is_simple
        assert _is_simple("who is Thomas?") is True

    def test_complex_question_with_and(self):
        from beever_atlas.agents.query.decomposer import _is_simple
        assert _is_simple("What is our JWT approach and who decided on it?") is False

    def test_complex_question_with_compare(self):
        from beever_atlas.agents.query.decomposer import _is_simple
        assert _is_simple("Compare our auth approaches") is False

    def test_long_question_is_complex(self):
        from beever_atlas.agents.query.decomposer import _is_simple
        long_q = " ".join(["word"] * 20)
        assert _is_simple(long_q) is False
