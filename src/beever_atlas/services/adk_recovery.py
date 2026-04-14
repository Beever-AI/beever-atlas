"""Centralized ADK LlmAgent recovery wrapper.

Any LlmAgent that uses ``output_schema=`` MUST be created via
``wrap_with_recovery`` so truncation/JSON-parse failures follow a single
code path.  This prevents individual agents from silently swallowing
truncation errors or re-introducing the cliff independently.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, TYPE_CHECKING

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types
from pydantic import BaseModel, ValidationError

from beever_atlas.services.json_recovery import (
    TruncationReport,
    recover_truncated_json_with_report,
    _close_open_structures,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_REPAIR_SUFFIX = (
    "\n\nThe previous response was truncated. "
    "Return ONLY a valid, complete JSON object that conforms to the required schema. "
    "Do not include any explanation — JSON only."
)


def wrap_with_recovery(
    agent: LlmAgent,
    recovery_fn: Callable[[str], dict | list | None],
    model: type[BaseModel],
) -> LlmAgent:
    """Install a truncation-recovery ``after_agent_callback`` on *agent*.

    The callback:
    1. If the output_key value is already a valid Pydantic model instance or
       a dict that validates against *model*, do nothing.
    2. If it is a raw string (LLM emitted text instead of structured output),
       attempt JSON recovery via *recovery_fn*.
    3. Validate the recovered dict against *model*.
    4. On second failure, emit a ``TruncationReport`` to state and mark
       ``failed_recoverable=True`` so checkpoint resume (A4) picks it up.

    Args:
        agent: The LlmAgent to wrap (mutated in-place and returned).
        recovery_fn: A callable that takes raw text and returns a recovered
            dict/list, or None on failure.  Typically one of the helpers in
            ``json_recovery``.
        model: The Pydantic model class to validate the recovered payload
            against.

    Returns:
        The same *agent* with ``after_agent_callback`` installed.
    """
    output_key: str = agent.output_key or ""

    def _recovery_callback(callback_context: CallbackContext) -> None:
        raw = callback_context.state.get(output_key)

        # Already a valid structured payload — nothing to do.
        if isinstance(raw, dict):
            try:
                model.model_validate(raw)
                return
            except ValidationError:
                pass  # fall through to recovery

        if not isinstance(raw, str) or not raw.strip():
            # Nothing recoverable.
            _mark_failed(callback_context, output_key, raw, model)
            return

        # Attempt recovery.
        recovered = recovery_fn(raw)
        if recovered is not None:
            try:
                validated = model.model_validate(recovered)
                callback_context.state[output_key] = validated.model_dump()
                logger.info(
                    "adk_recovery: recovered %s output via recovery_fn for key=%s",
                    model.__name__,
                    output_key,
                )
                return
            except ValidationError as exc:
                logger.warning(
                    "adk_recovery: recovery_fn returned data that failed %s validation "
                    "for key=%s: %s",
                    model.__name__,
                    output_key,
                    exc,
                )

        # Last-resort: close any open JSON brackets and try to parse directly.
        # Handles top-level truncated objects that have no internal boundary.
        closed = _close_open_structures(raw.strip())
        try:
            parsed = json.loads(closed)
            validated = model.model_validate(parsed)
            callback_context.state[output_key] = validated.model_dump()
            logger.info(
                "adk_recovery: last-resort close-and-parse succeeded for key=%s model=%s",
                output_key,
                model.__name__,
            )
            return
        except (json.JSONDecodeError, ValidationError):
            pass

        _mark_failed(callback_context, output_key, raw, model)

    agent.after_agent_callback = _recovery_callback
    # Strip output_schema so ADK does not invoke model_validate_json before
    # the callback runs — that internal validation is the EOF cliff we are fixing.
    # The callback above is now the sole validation path.
    agent.output_schema = None
    return agent


def _mark_failed(
    callback_context: CallbackContext,
    output_key: str,
    raw: Any,
    model: type[BaseModel],
) -> None:
    raw_text = raw if isinstance(raw, str) else json.dumps(raw) if raw is not None else ""
    raw_bytes = len(raw_text.encode()) if raw_text else 0
    report = TruncationReport(
        recovered_count=0,
        estimated_lost=1,
        raw_bytes=raw_bytes,
        last_boundary_offset=-1,
    )
    callback_context.state["truncation_report"] = {
        "output_key": output_key,
        "model": model.__name__,
        "recovered_count": report.recovered_count,
        "estimated_lost": report.estimated_lost,
        "raw_bytes": report.raw_bytes,
        "last_boundary_offset": report.last_boundary_offset,
    }
    callback_context.state["failed_recoverable"] = True
    logger.error(
        "adk_recovery: unrecoverable output for key=%s model=%s raw_bytes=%d; "
        "marked failed_recoverable=True",
        output_key,
        model.__name__,
        raw_bytes,
    )
