"""Gemini Batch API client.

Wraps the ``google.genai`` SDK's batch API to submit, poll, and parse
large-scale inference jobs without blocking the event loop.

Supports both inline requests (<20 MB) and file-based uploads (>=20 MB).
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 20 MB in bytes — SDK threshold for inline vs. file-based submission.
_INLINE_SIZE_LIMIT = 20 * 1024 * 1024

_TERMINAL_STATES = {
    "JOB_STATE_SUCCEEDED",
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
}
_SUCCESS_STATE = "JOB_STATE_SUCCEEDED"
_FAILED_STATES = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class BatchRequest:
    """A single request to be submitted as part of a Gemini batch job."""

    key: str
    """Caller-supplied identifier used to correlate responses."""

    prompt: str
    """Text prompt to send to the model."""


class GeminiBatchError(Exception):
    """Raised when a batch job reaches a terminal failure state."""

    def __init__(self, message: str, state: str = "") -> None:
        super().__init__(message)
        self.state = state


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GeminiBatchClient:
    """Async client for the Gemini Batch API.

    Args:
        model: Fully-qualified model name, e.g. ``"models/gemini-2.5-flash"``.
        api_key: Google AI API key.
        poll_interval: Seconds between poll attempts while waiting for a job.
        max_wait: Maximum total seconds to wait before raising ``TimeoutError``.
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        poll_interval: int = 15,
        max_wait: int = 3600,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._poll_interval = poll_interval
        self._max_wait = max_wait

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Lazily import and construct a ``google.genai`` client."""
        try:
            from google import genai  # type: ignore[import-untyped]
        except ModuleNotFoundError as exc:
            raise ImportError(
                "google-genai is required for GeminiBatchClient. "
                "Install it with: pip install google-genai"
            ) from exc
        return genai.Client(api_key=self._api_key)

    @staticmethod
    def _build_inline_requests(requests: list[BatchRequest]) -> list[dict[str, Any]]:
        """Convert BatchRequest objects into the SDK's inline request format."""
        return [
            {
                "contents": [
                    {
                        "parts": [{"text": req.prompt}],
                        "role": "user",
                    }
                ],
            }
            for req in requests
        ]

    @staticmethod
    def _serialized_size(payload: list[dict[str, Any]]) -> int:
        """Return the JSON-serialized byte size of *payload*."""
        return len(json.dumps(payload).encode())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def submit_job(self, requests: list[BatchRequest], display_name: str) -> str:
        """Submit a batch inference job to the Gemini API.

        For payloads up to 20 MB the requests are sent inline.  Larger
        payloads are serialized to a temporary file and uploaded via the
        Files API before the batch job is created.

        Args:
            requests: One or more :class:`BatchRequest` items.
            display_name: Human-readable label shown in the Cloud console.

        Returns:
            The job resource name (e.g. ``"batches/abc123"``), used to
            poll and retrieve results.
        """
        client = self._get_client()
        inline_payload = self._build_inline_requests(requests)
        payload_bytes = self._serialized_size(inline_payload)

        logger.info(
            "GeminiBatchClient: submitting job '%s' — %d requests, ~%d bytes",
            display_name,
            len(requests),
            payload_bytes,
        )

        if payload_bytes < _INLINE_SIZE_LIMIT:
            # Inline submission
            batch_job = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.batches.create(
                    model=self._model,
                    src=inline_payload,
                    config={"display_name": display_name},
                ),
            )
            logger.info("GeminiBatchClient: inline job created — name=%s", batch_job.name)
        else:
            # File-based submission for payloads >= 20 MB
            logger.info(
                "GeminiBatchClient: payload exceeds 20 MB (%d bytes) — uploading via Files API",
                payload_bytes,
            )
            batch_job = await self._submit_via_file(client, inline_payload, display_name)

        return batch_job.name

    async def _submit_via_file(
        self,
        client: Any,
        inline_payload: list[dict[str, Any]],
        display_name: str,
    ) -> Any:
        """Upload payload as a file and create a file-based batch job."""
        loop = asyncio.get_event_loop()

        # Write to a temporary JSONL file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
            for item in inline_payload:
                tmp.write(json.dumps(item) + "\n")
            tmp_path = tmp.name

        def _upload_and_create() -> Any:
            uploaded = client.files.upload(file=tmp_path)
            logger.info("GeminiBatchClient: uploaded file — name=%s", uploaded.name)
            return client.batches.create(
                model=self._model,
                src=uploaded.name,
                config={"display_name": display_name},
            )

        batch_job = await loop.run_in_executor(None, _upload_and_create)
        logger.info("GeminiBatchClient: file-based job created — name=%s", batch_job.name)
        return batch_job

    async def poll_job(
        self,
        job_name: str,
        on_status_change: Callable[[str, float], None] | None = None,
    ) -> Any:
        """Poll *job_name* until it reaches a terminal state.

        Args:
            job_name: Resource name returned by :meth:`submit_job`.
            on_status_change: Optional callback invoked whenever the job
                state changes.  Receives ``(state: str, elapsed: float)``.

        Returns:
            The completed batch job object from the SDK.

        Raises:
            GeminiBatchError: If the job fails, is cancelled, or expires.
            TimeoutError: If the job has not completed within ``max_wait``
                seconds.
        """
        client = self._get_client()
        loop = asyncio.get_event_loop()
        start = time.monotonic()
        last_state: str = ""

        logger.info("GeminiBatchClient: starting poll loop for job '%s'", job_name)

        while True:
            elapsed = time.monotonic() - start

            if elapsed > self._max_wait:
                raise TimeoutError(
                    f"GeminiBatchClient: job '{job_name}' did not complete within "
                    f"{self._max_wait}s (elapsed={elapsed:.1f}s)"
                )

            batch_job = await loop.run_in_executor(None, lambda: client.batches.get(name=job_name))
            state: str = (
                batch_job.state.name if hasattr(batch_job.state, "name") else str(batch_job.state)
            )

            if state != last_state:
                logger.info(
                    "GeminiBatchClient: job '%s' state=%s elapsed=%.1fs",
                    job_name,
                    state,
                    elapsed,
                )
                if on_status_change is not None:
                    try:
                        on_status_change(state, elapsed)
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "GeminiBatchClient: on_status_change callback raised an exception",
                            exc_info=True,
                        )
                last_state = state
            else:
                # Heartbeat every 30s so logs aren't silent during long waits
                if int(elapsed) % 30 < self._poll_interval:
                    logger.info(
                        "GeminiBatchClient: job '%s' still %s (%.0fs elapsed)",
                        job_name,
                        state,
                        elapsed,
                    )

            if state in _TERMINAL_STATES:
                if state in _FAILED_STATES:
                    raise GeminiBatchError(
                        f"Batch job '{job_name}' ended with state {state}",
                        state=state,
                    )
                # Success
                logger.info(
                    "GeminiBatchClient: job '%s' succeeded in %.1fs",
                    job_name,
                    elapsed,
                )
                return batch_job

            await asyncio.sleep(self._poll_interval)

    def parse_responses(self, job: Any, request_keys: list[str]) -> dict[str, str]:
        """Map batch response texts to caller-supplied request keys.

        Uses positional matching — the *n*-th response corresponds to the
        *n*-th key in *request_keys*.

        Args:
            job: Completed batch job object returned by :meth:`poll_job`.
            request_keys: Ordered list of keys from the original
                :class:`BatchRequest` list.

        Returns:
            Mapping of ``key → response_text``.  If a response cannot be
            parsed its value is an empty string.
        """
        results: dict[str, str] = {}

        try:
            responses = job.dest.inlined_responses
        except AttributeError:
            logger.warning("GeminiBatchClient: job has no inlined_responses; returning empty dict")
            return results

        if responses is None:
            logger.warning("GeminiBatchClient: inlined_responses is None; returning empty dict")
            return results

        for idx, (key, response) in enumerate(zip(request_keys, responses)):
            text = self._extract_text(response, idx)
            results[key] = text

        if len(request_keys) > len(responses):
            logger.warning(
                "GeminiBatchClient: %d keys provided but only %d responses received; "
                "missing keys will be absent from results",
                len(request_keys),
                len(responses),
            )

        return results

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Remove markdown code fences (```json ... ```) from LLM output."""
        stripped = text.strip()
        if stripped.startswith("```"):
            # Remove opening fence (```json or ```)
            first_newline = stripped.find("\n")
            if first_newline != -1:
                stripped = stripped[first_newline + 1 :]
            # Remove closing fence
            if stripped.rstrip().endswith("```"):
                stripped = stripped.rstrip()[:-3].rstrip()
        return stripped

    @staticmethod
    def _extract_text(response: Any, idx: int) -> str:
        """Extract the text string from a single batch response object."""
        raw = ""
        try:
            # Fast path: direct .text attribute
            text = response.response.text
            if text is not None:
                raw = text
        except AttributeError:
            pass

        if not raw:
            try:
                # Fallback: navigate candidates → content → parts
                raw = response.response.candidates[0].content.parts[0].text or ""
            except (AttributeError, IndexError, TypeError):
                logger.warning(
                    "GeminiBatchClient: could not extract text from response at index %d",
                    idx,
                )
                return ""

        # Strip markdown code fences if present
        return GeminiBatchClient._strip_code_fences(raw)
