"""Image description agent — generates text descriptions of images."""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from beever_atlas.agents.prompts.media import IMAGE_DESCRIBER_INSTRUCTION
from beever_atlas.agents.schemas.media import ImageDescriptionResult
from beever_atlas.llm import get_llm_provider


def create_image_describer(model=None) -> LlmAgent:
    """Create the image description LlmAgent."""
    return LlmAgent(
        name="image_describer",
        model=model or get_llm_provider().resolve_model("image_describer"),
        instruction=IMAGE_DESCRIBER_INSTRUCTION,
        output_key="image_description",
        output_schema=ImageDescriptionResult,
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
