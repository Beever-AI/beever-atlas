"""Preset definitions for the Endpoint Add form + the Apply Preset action.

Two roles:
1. **Endpoint presets** (``ENDPOINT_PRESETS``) — autofill shortcuts for the
   Add Endpoint dialog. Each preset declares ``base_url``, ``auth_type``,
   default model list, and badge metadata (embedding-only, local). Selecting
   a preset chip on the form populates these fields; "Custom" leaves them
   blank.
2. **Assignment presets** (``APPLY_PRESETS`` + ``apply_preset(...)``) — full
   17-consumer Assignment seeds that operators apply with one click. Each
   preset requires a specific Endpoint set ("Claude + Gemini hybrid" needs
   one Anthropic + one Google AI Endpoint) and raises
   ``PresetRequirementsNotMet`` when those are absent.

See ``openspec/changes/agent-llm-provider-pluggable/design.md`` D12.
"""

from __future__ import annotations

from typing import TypedDict

from beever_atlas.llm.assignments import Assignment
from beever_atlas.llm.endpoints import AuthType, Endpoint


class EndpointPreset(TypedDict, total=False):
    key: str
    label: str
    base_url: str
    auth_type: AuthType
    default_models: list[str]
    embedding_only: bool
    local: bool
    docs_url: str


# 18+ Endpoint presets ordered by likely first-time-user priority.
ENDPOINT_PRESETS: list[EndpointPreset] = [
    {
        "key": "google_ai",
        # IMPORTANT — leave ``base_url`` empty for google_ai. The
        # OpenAI-compat shim at ``…/v1beta/openai/`` does NOT honor
        # Gemini's ``response_mime_type="application/json"`` directive,
        # which the extraction agents (fact_extractor, entity_extractor,
        # coreference_resolver, …) depend on for strict JSON output.
        # With an empty base_url, ``route_for_endpoint`` returns the
        # native ``gemini`` LiteLLM provider, which DOES translate
        # ADK's ``GenerateContentConfig`` to Google's native API so
        # structured-output extraction works. The May-10 working
        # baseline had google_ai endpoints without this base_url; the
        # later /openai/ default regressed extraction silently because
        # all LLM calls still returned 200 OK — they just returned
        # unstructured text that the fact-extractor parser couldn't
        # turn into facts. See F11 commit for the full trace.
        "label": "Google AI (Gemini)",
        "base_url": "",
        "auth_type": "api_key",
        "default_models": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"],
        "docs_url": "https://aistudio.google.com/apikey",
    },
    {
        "key": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "auth_type": "api_key",
        "default_models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1", "o4-mini"],
        "docs_url": "https://platform.openai.com/api-keys",
    },
    {
        "key": "anthropic",
        "label": "Anthropic Claude",
        "base_url": "https://api.anthropic.com/v1",
        "auth_type": "api_key",
        "default_models": ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"],
        "docs_url": "https://console.anthropic.com/settings/keys",
    },
    {
        "key": "mistral",
        "label": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "auth_type": "api_key",
        "default_models": ["mistral-small-latest", "mistral-large-latest"],
        "docs_url": "https://console.mistral.ai/api-keys",
    },
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "auth_type": "api_key",
        "default_models": ["deepseek-chat", "deepseek-reasoner"],
        "docs_url": "https://platform.deepseek.com/api_keys",
    },
    {
        "key": "groq",
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "auth_type": "api_key",
        "default_models": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"],
        "docs_url": "https://console.groq.com/keys",
    },
    {
        "key": "xai",
        "label": "xAI Grok",
        "base_url": "https://api.x.ai/v1",
        "auth_type": "api_key",
        "default_models": ["grok-4"],
        "docs_url": "https://console.x.ai/team",
    },
    {
        "key": "minimax",
        "label": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "auth_type": "api_key",
        "default_models": ["abab6.5s-chat"],
        "docs_url": "https://platform.minimaxi.com/document/Models",
    },
    {
        "key": "cohere",
        "label": "Cohere",
        "base_url": "https://api.cohere.ai/v1",
        "auth_type": "api_key",
        "default_models": ["embed-multilingual-v3.0"],
        "embedding_only": True,
        "docs_url": "https://dashboard.cohere.com/api-keys",
    },
    {
        "key": "voyage",
        "label": "Voyage AI",
        "base_url": "https://api.voyageai.com/v1",
        "auth_type": "api_key",
        "default_models": ["voyage-3-large"],
        "embedding_only": True,
        "docs_url": "https://dash.voyageai.com/",
    },
    {
        "key": "jina_ai",
        "label": "Jina",
        "base_url": "https://api.jina.ai/v1",
        "auth_type": "api_key",
        "default_models": ["jina-embeddings-v4", "jina-embeddings-v3"],
        "embedding_only": True,
        "docs_url": "https://jina.ai/api-dashboard/",
    },
    {
        "key": "ollama",
        "label": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
        "auth_type": "none",
        "default_models": ["gemma3:e4b", "qwen2.5:14b", "llama3.3", "phi4"],
        "local": True,
        "docs_url": "https://ollama.com/library",
    },
    {
        "key": "vllm",
        "label": "vLLM (self-hosted)",
        "base_url": "http://localhost:8000/v1",
        "auth_type": "none",
        "default_models": [],
        "local": True,
        "docs_url": "https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html",
    },
    {
        "key": "lmstudio",
        "label": "LM Studio (local)",
        "base_url": "http://localhost:1234/v1",
        "auth_type": "none",
        "default_models": [],
        "local": True,
        "docs_url": "https://lmstudio.ai/docs/local-server",
    },
    {
        "key": "openrouter",
        "label": "OpenRouter (proxy)",
        "base_url": "https://openrouter.ai/api/v1",
        "auth_type": "api_key",
        "default_models": [],
        "docs_url": "https://openrouter.ai/keys",
    },
    {
        "key": "litellm_proxy",
        "label": "LiteLLM Proxy (self-hosted)",
        "base_url": "",
        "auth_type": "api_key",
        "default_models": [],
        "docs_url": "https://docs.litellm.ai/docs/simple_proxy",
    },
    {
        "key": "bedrock",
        "label": "AWS Bedrock",
        "base_url": "",
        "auth_type": "aws_iam",
        "default_models": [
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "amazon.nova-pro-v1:0",
        ],
        "docs_url": "https://docs.aws.amazon.com/bedrock/latest/userguide/api-setup.html",
    },
    {
        "key": "vertex_ai",
        "label": "Vertex AI (Google Cloud)",
        "base_url": "",
        "auth_type": "google_sa",
        "default_models": ["gemini-2.5-pro", "gemini-2.5-flash"],
        "docs_url": "https://cloud.google.com/vertex-ai/docs/start/cloud-environment",
    },
    {
        "key": "custom",
        "label": "Custom OpenAI-compatible",
        "base_url": "",
        "auth_type": "api_key",
        "default_models": [],
    },
]


def get_endpoint_preset(key: str) -> EndpointPreset | None:
    """Return the preset matching ``key``, or None if unknown."""
    return next((p for p in ENDPOINT_PRESETS if p.get("key") == key), None)


# Derived from ENDPOINT_PRESETS — single source of truth for "preset key →
# default base URL". Imported by ``scripts/migrate_to_endpoint_catalog.py`` and
# ``scripts/atlas_apply.py`` so the URL table never drifts across three files.
BASE_URL_BY_PRESET: dict[str, str] = {
    key: url
    for key, url in ((p.get("key", ""), p.get("base_url", "")) for p in ENDPOINT_PRESETS)
    if key and url
}


# ────────────────────────────────────────────────────────────────────────
# Assignment presets — full 17-consumer Assignment seeds.
# ────────────────────────────────────────────────────────────────────────


class PresetRequirementsNotMet(Exception):
    """Raised when an apply-preset call references endpoint presets absent
    from the operator's current Endpoint catalog. Message lists the missing
    presets so the UI can prompt the operator to add them first."""

    def __init__(self, required_presets: list[str], present_presets: list[str]) -> None:
        self.required = required_presets
        self.present = present_presets
        super().__init__(
            f"preset requirements not met: required={required_presets}, present={present_presets}"
        )


def _first_with_preset(endpoints: list[Endpoint], preset_key: str) -> Endpoint | None:
    return next((e for e in endpoints if e.preset == preset_key), None)


def _build_assignment(
    consumer: str,
    endpoint: Endpoint,
    model: str,
    **overrides: object,
) -> Assignment:
    """Build a fresh Assignment pointing at ``endpoint`` + ``model``."""
    return Assignment(
        consumer=consumer,
        endpoint_id=endpoint.id,
        model=model,
        **overrides,  # type: ignore[arg-type]
    )


def _preset_gemini_balanced(endpoints: list[Endpoint]) -> dict[str, Assignment]:
    """Single-provider Gemini setup — fast for everything, embedding via gemini-embedding-001."""
    google = _first_with_preset(endpoints, "google_ai")
    if google is None:
        raise PresetRequirementsNotMet(["google_ai"], [e.preset for e in endpoints])
    ollama = _first_with_preset(endpoints, "ollama")
    media_ep = ollama or google
    media_model = "gemma3:e4b" if ollama else "gemini-2.5-flash"

    return {
        "embedding": _build_assignment(
            "embedding", google, "gemini-embedding-001", dimensions=3072
        ),
        "fact_extractor": _build_assignment("fact_extractor", google, "gemini-2.5-flash"),
        "entity_extractor": _build_assignment("entity_extractor", google, "gemini-2.5-flash"),
        "cross_batch_validator": _build_assignment(
            "cross_batch_validator", google, "gemini-2.5-flash-lite"
        ),
        "coreference_resolver": _build_assignment(
            "coreference_resolver", google, "gemini-2.5-flash-lite"
        ),
        "contradiction_detector": _build_assignment(
            "contradiction_detector", google, "gemini-2.5-flash-lite"
        ),
        "summarizer": _build_assignment("summarizer", google, "gemini-2.5-flash-lite"),
        "image_describer": _build_assignment("image_describer", media_ep, media_model),
        "video_analyzer": _build_assignment("video_analyzer", google, "gemini-2.5-flash"),
        "audio_transcriber": _build_assignment("audio_transcriber", google, "gemini-2.5-flash"),
        "document_digester": _build_assignment("document_digester", google, "gemini-2.5-flash"),
        "echo": _build_assignment("echo", google, "gemini-2.5-flash-lite"),
        "wiki_compiler": _build_assignment("wiki_compiler", google, "gemini-2.5-flash"),
        "wiki_maintainer": _build_assignment("wiki_maintainer", google, "gemini-2.5-flash"),
        "qa_router": _build_assignment("qa_router", google, "gemini-2.5-flash-lite"),
        "qa_agent": _build_assignment("qa_agent", google, "gemini-2.5-flash"),
        "csv_mapper": _build_assignment(
            "csv_mapper", google, "gemini-2.5-flash-lite", response_format="json"
        ),
    }


def _preset_openai_quality(endpoints: list[Endpoint]) -> dict[str, Assignment]:
    openai = _first_with_preset(endpoints, "openai")
    if openai is None:
        raise PresetRequirementsNotMet(["openai"], [e.preset for e in endpoints])
    ollama = _first_with_preset(endpoints, "ollama")
    return {
        "embedding": _build_assignment(
            "embedding", openai, "text-embedding-3-large", dimensions=3072
        ),
        "fact_extractor": _build_assignment("fact_extractor", openai, "gpt-4o-mini"),
        "entity_extractor": _build_assignment("entity_extractor", openai, "gpt-4o-mini"),
        "cross_batch_validator": _build_assignment("cross_batch_validator", openai, "gpt-4o-mini"),
        "coreference_resolver": _build_assignment("coreference_resolver", openai, "gpt-4o-mini"),
        "contradiction_detector": _build_assignment(
            "contradiction_detector", openai, "gpt-4o-mini"
        ),
        "summarizer": _build_assignment("summarizer", openai, "gpt-4o-mini"),
        "image_describer": _build_assignment(
            "image_describer",
            ollama or openai,
            "gemma3:e4b" if ollama else "gpt-4o-mini",
        ),
        "video_analyzer": _build_assignment("video_analyzer", openai, "gpt-4o"),
        "audio_transcriber": _build_assignment("audio_transcriber", openai, "gpt-4o-mini"),
        "document_digester": _build_assignment("document_digester", openai, "gpt-4o-mini"),
        "echo": _build_assignment("echo", openai, "gpt-4o-mini"),
        "wiki_compiler": _build_assignment("wiki_compiler", openai, "gpt-4.1"),
        "wiki_maintainer": _build_assignment("wiki_maintainer", openai, "gpt-4.1"),
        "qa_router": _build_assignment("qa_router", openai, "gpt-4o-mini"),
        "qa_agent": _build_assignment("qa_agent", openai, "gpt-4o"),
        "csv_mapper": _build_assignment(
            "csv_mapper", openai, "gpt-4o-mini", response_format="json"
        ),
    }


def _preset_claude_quality_gemini_fast(endpoints: list[Endpoint]) -> dict[str, Assignment]:
    anthropic = _first_with_preset(endpoints, "anthropic")
    google = _first_with_preset(endpoints, "google_ai")
    missing: list[str] = []
    if anthropic is None:
        missing.append("anthropic")
    if google is None:
        missing.append("google_ai")
    if missing:
        raise PresetRequirementsNotMet(missing, [e.preset for e in endpoints])
    assert anthropic is not None and google is not None  # narrow for typer
    ollama = _first_with_preset(endpoints, "ollama")
    return {
        "embedding": _build_assignment(
            "embedding", google, "gemini-embedding-001", dimensions=3072
        ),
        "fact_extractor": _build_assignment("fact_extractor", google, "gemini-2.5-flash"),
        "entity_extractor": _build_assignment("entity_extractor", google, "gemini-2.5-flash"),
        "cross_batch_validator": _build_assignment(
            "cross_batch_validator", google, "gemini-2.5-flash-lite"
        ),
        "coreference_resolver": _build_assignment(
            "coreference_resolver", google, "gemini-2.5-flash-lite"
        ),
        "contradiction_detector": _build_assignment(
            "contradiction_detector", google, "gemini-2.5-flash-lite"
        ),
        "summarizer": _build_assignment("summarizer", google, "gemini-2.5-flash-lite"),
        "image_describer": _build_assignment(
            "image_describer",
            ollama or google,
            "gemma3:e4b" if ollama else "gemini-2.5-flash",
        ),
        "video_analyzer": _build_assignment("video_analyzer", google, "gemini-2.5-flash"),
        "audio_transcriber": _build_assignment("audio_transcriber", google, "gemini-2.5-flash"),
        "document_digester": _build_assignment("document_digester", google, "gemini-2.5-flash"),
        "echo": _build_assignment("echo", google, "gemini-2.5-flash-lite"),
        "wiki_compiler": _build_assignment("wiki_compiler", anthropic, "claude-sonnet-4-6"),
        "wiki_maintainer": _build_assignment("wiki_maintainer", anthropic, "claude-sonnet-4-6"),
        "qa_router": _build_assignment("qa_router", anthropic, "claude-haiku-4-5"),
        "qa_agent": _build_assignment("qa_agent", anthropic, "claude-sonnet-4-6"),
        "csv_mapper": _build_assignment(
            "csv_mapper", google, "gemini-2.5-flash-lite", response_format="json"
        ),
    }


def _preset_fully_local(endpoints: list[Endpoint]) -> dict[str, Assignment]:
    ollama = _first_with_preset(endpoints, "ollama")
    if ollama is None:
        raise PresetRequirementsNotMet(["ollama"], [e.preset for e in endpoints])
    return {
        # Embedding uses Ollama too — the local nomic-embed-text model.
        "embedding": _build_assignment("embedding", ollama, "nomic-embed-text", dimensions=768),
        "fact_extractor": _build_assignment("fact_extractor", ollama, "qwen2.5:14b"),
        "entity_extractor": _build_assignment("entity_extractor", ollama, "qwen2.5:14b"),
        "cross_batch_validator": _build_assignment("cross_batch_validator", ollama, "qwen2.5:14b"),
        "coreference_resolver": _build_assignment("coreference_resolver", ollama, "qwen2.5:14b"),
        "contradiction_detector": _build_assignment(
            "contradiction_detector", ollama, "qwen2.5:14b"
        ),
        "summarizer": _build_assignment("summarizer", ollama, "qwen2.5:14b"),
        "image_describer": _build_assignment("image_describer", ollama, "llama3.2-vision"),
        "video_analyzer": _build_assignment("video_analyzer", ollama, "llama3.2-vision"),
        "audio_transcriber": _build_assignment("audio_transcriber", ollama, "qwen2.5:14b"),
        "document_digester": _build_assignment("document_digester", ollama, "llama3.2-vision"),
        "echo": _build_assignment("echo", ollama, "phi4"),
        "wiki_compiler": _build_assignment("wiki_compiler", ollama, "qwen2.5:14b"),
        "wiki_maintainer": _build_assignment("wiki_maintainer", ollama, "qwen2.5:14b"),
        "qa_router": _build_assignment("qa_router", ollama, "phi4"),
        "qa_agent": _build_assignment("qa_agent", ollama, "qwen2.5:14b"),
        "csv_mapper": _build_assignment(
            "csv_mapper", ollama, "qwen2.5-coder:14b", response_format="json"
        ),
    }


APPLY_PRESETS: dict[str, str] = {
    "gemini-balanced": "Use Gemini for every agent + embedding",
    "openai-quality": "Use OpenAI everywhere; GPT-4.1 for wiki/QA, 4o-mini for ingestion",
    "claude-quality-gemini-fast": "Claude Sonnet for QA + wiki, Gemini Flash for ingestion",
    "fully-local": "Ollama for every agent + embedding (air-gapped friendly)",
    "custom": "Configure each agent individually in the UI",
}


_APPLY_DISPATCH = {
    "gemini-balanced": _preset_gemini_balanced,
    "openai-quality": _preset_openai_quality,
    "claude-quality-gemini-fast": _preset_claude_quality_gemini_fast,
    "fully-local": _preset_fully_local,
}


def apply_preset(preset_key: str, endpoints: list[Endpoint]) -> dict[str, Assignment]:
    """Compute the full Assignment seed for ``preset_key`` against the operator's
    current Endpoint catalog. Raises :class:`PresetRequirementsNotMet` when the
    required endpoints are absent. ``custom`` returns an empty dict (operator
    configures everything in the UI)."""
    if preset_key == "custom":
        return {}
    fn = _APPLY_DISPATCH.get(preset_key)
    if fn is None:
        raise ValueError(f"unknown preset {preset_key!r}; valid: {list(APPLY_PRESETS)}")
    return fn(endpoints)


__all__ = [
    "APPLY_PRESETS",
    "BASE_URL_BY_PRESET",
    "ENDPOINT_PRESETS",
    "EndpointPreset",
    "PresetRequirementsNotMet",
    "apply_preset",
    "get_endpoint_preset",
]
