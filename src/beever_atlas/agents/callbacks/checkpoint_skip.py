"""Checkpoint skip guards for pipeline stage agents."""

from __future__ import annotations

import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.genai import types

logger = logging.getLogger(__name__)

_AGENT_OUTPUT_KEYS: dict[str, str] = {
    "preprocessor": "preprocessed_messages",
    "fact_extractor": "extracted_facts",
    "entity_extractor": "extracted_entities",
    "embedder": "embedded_facts",
    "cross_batch_validator_agent": "validated_entities",
    "persister": "persist_result",
}


def make_checkpoint_skip_callback(agent_name: str):
    """Factory: returns a before_agent_callback that skips if output_key already exists in state."""
    output_key = _AGENT_OUTPUT_KEYS[agent_name]

    def _skip_if_checkpointed(callback_context: CallbackContext) -> types.Content | None:
        existing = callback_context.state.get(output_key)
        if existing is not None:
            logger.info(
                "CheckpointSkip: skipping %s (restored from checkpoint)",
                agent_name,
            )
            return types.Content(
                role="model",
                parts=[types.Part(text=f"[Skipped: {agent_name} — restored from checkpoint]")],
            )
        return None

    return _skip_if_checkpointed


def should_skip_stage(ctx_state: dict[str, Any], output_key: str, agent_name: str) -> bool:
    """Check if a BaseAgent stage should skip (for use at top of _run_async_impl)."""
    existing = ctx_state.get(output_key)
    if existing is not None:
        logger.info(
            "CheckpointSkip: skipping %s (restored from checkpoint)",
            agent_name,
        )
        return True
    return False
