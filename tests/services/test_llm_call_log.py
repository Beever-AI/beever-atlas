"""PR-λ: tests for the recent-LLM-calls ring buffer + debug surface."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

from beever_atlas.services import llm_call_log


@pytest.fixture(autouse=True)
def _reset_log() -> Generator[None, None, None]:
    """Clear the ring buffer before each test."""
    llm_call_log.clear()
    yield
    llm_call_log.clear()


def test_record_call_success_captures_provider_model_and_latency() -> None:
    import time

    started = time.monotonic() - 0.05  # 50ms ago

    class _FakeResp:
        model = "gemini-3.1-flash-lite"

    llm_call_log.record_call(
        started_at=started,
        kind="completion",
        consumer="qa_agent",
        provider="openai",
        model="gemini-3.1-flash-lite",
        api_base="https://generativelanguage.googleapis.com/v1beta/openai/",
        response=_FakeResp(),
    )

    snap = llm_call_log.snapshot()
    assert len(snap) == 1
    row = snap[0]
    assert row["ok"] is True
    assert row["consumer"] == "qa_agent"
    assert row["provider"] == "openai"
    assert row["model"] == "gemini-3.1-flash-lite"
    assert row["api_base"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert row["response_model"] == "gemini-3.1-flash-lite"
    assert row["latency_ms"] is not None and row["latency_ms"] >= 50
    assert row["error_class"] is None
    assert row["error_summary"] is None


def test_record_call_failure_captures_error_class_and_summary() -> None:
    import time

    started = time.monotonic()

    llm_call_log.record_call(
        started_at=started,
        kind="completion",
        consumer="qa_agent",
        provider="openai",
        model="gemini-3.1-flash-lite",
        api_base="https://api.example.com/v1",
        exc=RuntimeError("upstream returned 404 NOT_FOUND"),
    )

    snap = llm_call_log.snapshot()
    assert len(snap) == 1
    row = snap[0]
    assert row["ok"] is False
    assert row["error_class"] == "RuntimeError"
    assert "404 NOT_FOUND" in (row["error_summary"] or "")
    assert row["response_model"] is None
    # latency_ms is None on failure (we don't trust partial timings)
    assert row["latency_ms"] is None


def test_record_call_redacts_credential_fragments_in_error() -> None:
    import time

    started = time.monotonic()
    llm_call_log.record_call(
        started_at=started,
        kind="completion",
        consumer=None,
        provider="openai",
        model="gpt-4o-mini",
        api_base=None,
        exc=Exception("Authorization: Bearer sk-leaked-key-123 returned 401"),
    )
    snap = llm_call_log.snapshot()
    assert snap[0]["ok"] is False
    # The credential-redactor should have scrubbed the bearer token.
    assert "sk-leaked-key-123" not in (snap[0]["error_summary"] or "")
    assert "redacted" in (snap[0]["error_summary"] or "").lower()


def test_ring_buffer_caps_at_50_entries() -> None:
    import time

    for i in range(60):
        llm_call_log.record_call(
            started_at=time.monotonic(),
            kind="completion",
            consumer=None,
            provider="openai",
            model=f"model-{i}",
            api_base=None,
            response=type("R", (), {"model": f"model-{i}"})(),
        )
    snap = llm_call_log.snapshot()
    assert len(snap) == 50
    # Newest first — the most recent record should be model-59.
    assert snap[0]["model"] == "model-59"
    # The oldest still in the buffer is model-10 (60 records, 50 kept,
    # newest is 59, oldest = 59 - 49 = 10).
    assert snap[-1]["model"] == "model-10"


def test_record_call_never_raises_on_bad_input() -> None:
    """The recorder must be defensive — a bug in the recorder must NOT
    take down the dispatch path."""
    import time

    # Pass a response object whose ``model`` attribute access raises.
    class _Hostile:
        @property
        def model(self) -> str:
            raise RuntimeError("nope")

    # Should not raise.
    llm_call_log.record_call(
        started_at=time.monotonic(),
        kind="completion",
        consumer=None,
        provider="openai",
        model="gpt-4o",
        api_base=None,
        response=_Hostile(),
    )
    snap = llm_call_log.snapshot()
    # Entry still appended (response_model just left as None).
    assert len(snap) == 1
    assert snap[0]["ok"] is True
    assert snap[0]["response_model"] is None


@pytest.mark.asyncio
async def test_dispatch_completion_records_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check: a successful ``dispatch_completion`` populates the
    ring buffer with the LiteLLM provider + model that actually went on
    the wire (not the unprefixed input)."""
    import litellm  # type: ignore[import-untyped]
    from beever_atlas.services.llm_dispatch import dispatch_completion

    class _FakeResp:
        model = "gemini-3.1-flash-lite"
        status_code = 200
        choices: Any = []

    async def fake_acompletion(**kwargs: Any) -> Any:
        return _FakeResp()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    await dispatch_completion(
        provider="openai",
        model="openai/gemini-3.1-flash-lite",
        messages=[{"role": "user", "content": "hi"}],
        api_base="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key="AIza-...",
        _log_consumer="qa_agent",
    )

    snap = llm_call_log.snapshot()
    assert len(snap) == 1
    row = snap[0]
    assert row["ok"] is True
    assert row["consumer"] == "qa_agent"
    assert row["provider"] == "openai"
    # ``_split_model_for_litellm`` should have stripped the matching prefix.
    assert row["model"] == "gemini-3.1-flash-lite"
    assert row["api_base"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert row["response_model"] == "gemini-3.1-flash-lite"


@pytest.mark.asyncio
async def test_dispatch_assignment_threads_consumer_into_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dispatch_assignment`` must forward the consumer name so the debug
    UI can show "qa_agent" instead of just the bare model."""
    import litellm  # type: ignore[import-untyped]
    from types import SimpleNamespace
    from beever_atlas.services.llm_dispatch import dispatch_assignment

    class _FakeResp:
        model = "gemini-3.1-flash-lite"
        status_code = 200
        choices: Any = []

    async def fake_acompletion(**kwargs: Any) -> Any:
        return _FakeResp()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    assignment = SimpleNamespace(
        consumer="qa_agent",
        provider="openai",
        litellm_model="openai/gemini-3.1-flash-lite",
        endpoint_id="ep-1",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key="AIza-...",
        extra_headers={},
        temperature=None,
        max_tokens=None,
        response_format=None,
        aws_credentials=None,
        vertex_credentials=None,
    )

    await dispatch_assignment(
        assignment=assignment,
        messages=[{"role": "user", "content": "hi"}],
    )

    snap = llm_call_log.snapshot()
    assert len(snap) == 1
    assert snap[0]["consumer"] == "qa_agent"
    assert snap[0]["model"] == "gemini-3.1-flash-lite"


def test_strip_url_credentials_removes_basic_auth_userinfo() -> None:
    """Operator-configured base_url with embedded ``user:pass@`` MUST NOT
    leak to the ring buffer / log line / debug endpoint."""
    from beever_atlas.services.llm_call_log import _strip_url_credentials

    assert (
        _strip_url_credentials("https://alice:secret123@proxy.corp.example/v1")
        == "https://proxy.corp.example/v1"
    )
    # Username-only (still leaky on some servers)
    assert (
        _strip_url_credentials("https://token-xyz@gateway.example.com:8443/api/v1")
        == "https://gateway.example.com:8443/api/v1"
    )
    # Clean URLs pass through unchanged
    assert _strip_url_credentials("https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert _strip_url_credentials("http://localhost:11434/v1") == "http://localhost:11434/v1"
    # Bad input doesn't crash
    assert _strip_url_credentials("") == ""


def test_dispatch_owns_recording_contextvar_skips_custom_logger() -> None:
    """When the dispatch wrapper sets ``_DISPATCH_OWNS_RECORDING``, the
    CustomLogger must skip its own recording to avoid double-entry.

    This is the dedup mechanism that keeps the ring buffer accurate when a
    dispatched call ALSO triggers LiteLLM's success_callback.
    """
    from beever_atlas.services.llm_call_log import _DISPATCH_OWNS_RECORDING

    # When the flag is False (default) the CustomLogger would record;
    # we don't test the callback directly here — we just verify the
    # contextvar's contract.
    assert _DISPATCH_OWNS_RECORDING.get() is False
    token = _DISPATCH_OWNS_RECORDING.set(True)
    try:
        assert _DISPATCH_OWNS_RECORDING.get() is True
    finally:
        _DISPATCH_OWNS_RECORDING.reset(token)
    assert _DISPATCH_OWNS_RECORDING.get() is False


def test_snapshot_serialisation_shape() -> None:
    """Every field in the dataclass must round-trip through ``asdict``."""
    import time

    llm_call_log.record_call(
        started_at=time.monotonic(),
        kind="embedding",
        consumer=None,
        provider="gemini",
        model="gemini/gemini-embedding-001",
        api_base=None,
        response=type("R", (), {"model": "gemini-embedding-001"})(),
    )
    snap = llm_call_log.snapshot()
    assert isinstance(snap, list)
    row = snap[0]
    expected_keys = {
        "ts",
        "kind",
        "consumer",
        "provider",
        "model",
        "api_base",
        "latency_ms",
        "ok",
        "response_model",
        "error_class",
        "error_summary",
    }
    assert set(row.keys()) == expected_keys
