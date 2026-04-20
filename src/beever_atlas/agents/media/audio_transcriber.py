"""Audio transcription agent — transcribes audio content."""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from beever_atlas.agents.prompts.media import AUDIO_TRANSCRIBER_INSTRUCTION
from beever_atlas.agents.schemas.media import AudioTranscriptionResult
from beever_atlas.llm import get_llm_provider


def create_audio_transcriber(model=None) -> LlmAgent:
    """Create the audio transcription LlmAgent."""
    return LlmAgent(
        name="audio_transcriber",
        model=model or get_llm_provider().resolve_model("audio_transcriber"),
        instruction=AUDIO_TRANSCRIBER_INSTRUCTION,
        output_key="audio_transcription",
        output_schema=AudioTranscriptionResult,
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
