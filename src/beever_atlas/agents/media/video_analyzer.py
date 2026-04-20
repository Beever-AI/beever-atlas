"""Video analysis agent — transcribes and describes video content."""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from beever_atlas.agents.prompts.media import VIDEO_ANALYZER_INSTRUCTION
from beever_atlas.agents.schemas.media import VideoAnalysisResult
from beever_atlas.llm import get_llm_provider


def create_video_analyzer(model=None) -> LlmAgent:
    """Create the video analysis LlmAgent."""
    return LlmAgent(
        name="video_analyzer",
        model=model or get_llm_provider().resolve_model("video_analyzer"),
        instruction=VIDEO_ANALYZER_INSTRUCTION,
        output_key="video_analysis",
        output_schema=VideoAnalysisResult,
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
