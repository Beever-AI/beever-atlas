"""Per-model classification: which of Atlas's 5 categories a model serves.

Discovery (``/v1/models``) returns every model a provider serves — including
rerankers, vision-only encoders, image generators, TTS / STT, fine-tunes, and
moderation models that Atlas does not consume. Persisting these into
``Endpoint.models[]`` breaks Test Connection (Jina's ``models[0] = jina-vlm``
is not a valid embedding model ID) and clutters the model picker.

This module classifies a single ``(preset, model_id)`` pair into the kind
that downstream UI / dispatch cares about.

Resolution order:

  1. **Catalog lookup** — ``known_models.lookup_by_id("<preset>/<model>")``
     returns the canonical ``"chat"`` or ``"embedding"`` when the model is
     in our curated catalog.
  2. **Preset-specific rules** — name-pattern table per provider. Gemini's
     ``models/`` prefix is stripped first; OpenRouter's ``<vendor>/`` prefix
     is stripped and the suffix dispatched to the upstream-vendor rules.
  3. **Generic name-pattern fallback** — ``*embed*`` → embedding,
     ``*rerank*`` → reranker, ``whisper*`` → audio_stt, etc.
  4. **Default** — trusted chat-capable presets default to ``"chat"``;
     embedding-only presets (jina_ai, voyage) default to ``"other"`` for
     ids that match nothing (e.g. an unknown Jina artifact).

Conscious choices baked into this module:

* The persisted ``Endpoint.model_kinds`` map only ever stores
  ``"chat" | "embedding"``. The finer categories (``reranker``,
  ``image_gen``, …) are returned for the *dropped* bucket so the UI can
  show a breakdown — they never surface as user-facing "kind of this model".
* No ``"unknown"`` value anywhere. If we cannot decide we trust the
  preset's default (chat for chat-capable presets, ``other`` for
  embedding-only presets).
"""

from __future__ import annotations

import re
from typing import Literal

from beever_atlas.llm.known_models import lookup_by_id

ModelKind = Literal[
    "chat",
    "embedding",
    "reranker",
    "image_gen",
    "audio_synth",
    "audio_stt",
    "clip",
    "segmenter",
    "reader",
    "moderation",
    "fine_tune",
    "other",
]


# Presets that legitimately serve chat completions. For these, an
# unrecognised model id defaults to ``chat`` — trusting the operator who
# added the endpoint.
_TRUSTED_CHAT_PRESETS: frozenset[str] = frozenset(
    {
        "openai",
        "google_ai",
        "gemini",
        "anthropic",
        "mistral",
        "deepseek",
        "groq",
        "xai",
        "minimax",
        "together_ai",
        "cohere",
        "ollama",
        "ollama_chat",
    }
)


# Presets that serve embeddings ONLY (no chat completion route). An
# unrecognised id under these presets is almost certainly a non-embedding
# artifact (reader, segmenter, VLM…) and gets the ``other`` kind so we drop
# it from the kept-models list.
_EMBEDDING_ONLY_PRESETS: frozenset[str] = frozenset({"jina_ai", "voyage"})


def _strip_gemini_prefix(model_id: str) -> str:
    """Gemini's ``/v1beta/openai/models`` returns ids like ``models/gemini-2.5-flash``."""
    return model_id.removeprefix("models/")


def _classify_openai(model_id: str) -> ModelKind | None:
    if model_id.startswith("ft:"):
        return "fine_tune"
    if model_id.startswith(("text-embedding-",)) or "ada" in model_id:
        return "embedding"
    if model_id.startswith("whisper-"):
        return "audio_stt"
    if model_id.startswith("tts-"):
        return "audio_synth"
    if model_id.startswith(("dall-e-", "gpt-image-")):
        return "image_gen"
    if model_id.startswith("omni-moderation-"):
        return "moderation"
    if model_id.startswith(("babbage-", "davinci-")):
        return "other"
    if (
        model_id.startswith(("gpt-", "chatgpt-"))
        or re.match(r"^o\d", model_id)
        or re.match(r"^chatgpt", model_id)
    ):
        return "chat"
    return None


def _classify_google_ai(model_id: str) -> ModelKind | None:
    # Caller already strips ``models/`` prefix.
    if model_id.startswith(("text-embedding-", "gemini-embedding-")):
        return "embedding"
    if model_id.startswith("imagen-"):
        return "image_gen"
    if model_id.startswith("veo-"):
        return "other"
    if model_id == "aqa" or model_id.startswith("learnlm-"):
        return "other"
    if model_id.startswith("gemini-"):
        # Carve specialised Gemini variants out of the chat bucket — the
        # ``/v1beta/openai/`` shim returns ``INVALID_ARGUMENT: 'This model
        # only supports Interactions API.'`` for these, so probing one of
        # them as a "chat" model breaks Test even though the model id
        # superficially looks like a chat model.
        if (
            "-live" in model_id
            or "-image-generation" in model_id
            or "-tts" in model_id
            or "-native-audio" in model_id
            or "-realtime" in model_id
        ):
            return "other"
        return "chat"
    return None


def _classify_anthropic(model_id: str) -> ModelKind | None:
    if model_id.startswith("claude-"):
        return "chat"
    return None


def _classify_mistral(model_id: str) -> ModelKind | None:
    if model_id.startswith("ft:"):
        return "fine_tune"
    if model_id.startswith("mistral-moderation-"):
        return "moderation"
    if model_id in ("mistral-embed", "codestral-embed"):
        return "embedding"
    if model_id.startswith(("mistral-", "pixtral-", "codestral-")):
        # Catch any "*-embed" suffix we didn't list explicitly.
        if model_id.endswith("-embed"):
            return "embedding"
        return "chat"
    return None


def _classify_deepseek(model_id: str) -> ModelKind | None:
    if model_id.startswith("deepseek-"):
        return "chat"
    return None


def _classify_groq(model_id: str) -> ModelKind | None:
    if model_id.startswith("whisper-large-"):
        return "audio_stt"
    if model_id.startswith("playai-tts-"):
        return "audio_synth"
    if model_id.startswith("llama-guard-"):
        return "moderation"
    if model_id.startswith(("llama-", "mixtral-", "gemma-", "qwen-", "kimi-")):
        return "chat"
    return None


def _classify_xai(model_id: str) -> ModelKind | None:
    if model_id.startswith("grok-") and "-image-" in model_id:
        return "image_gen"
    if model_id.startswith("grok-"):
        return "chat"
    return None


def _classify_minimax(model_id: str) -> ModelKind | None:
    if model_id.startswith(("speech-", "music-")):
        return "audio_synth"
    if model_id.startswith("image-"):
        return "image_gen"
    if model_id.startswith("video-"):
        return "other"
    if model_id.startswith("abab") or model_id.startswith("MiniMax-"):
        return "chat"
    return None


def _classify_cohere(model_id: str) -> ModelKind | None:
    if model_id.startswith("rerank-"):
        return "reranker"
    if model_id.startswith("embed-"):
        return "embedding"
    if model_id.startswith("command-"):
        return "chat"
    return None


def _classify_voyage(model_id: str) -> ModelKind | None:
    if model_id.startswith("rerank-"):
        return "reranker"
    if model_id.startswith("voyage-"):
        return "embedding"
    return None


def _classify_jina(model_id: str) -> ModelKind | None:
    if model_id.startswith("jina-embeddings-"):
        return "embedding"
    if model_id.startswith("jina-reranker-"):
        return "reranker"
    if (
        model_id == "jina-vlm"
        or model_id.startswith("jina-vlm-")
        or model_id.startswith("jina-clip-")
    ):
        return "clip"
    if model_id.startswith("jina-segmenter-"):
        return "segmenter"
    if model_id.startswith("reader-"):
        return "reader"
    return None


# Map preset → vendor key used by OpenRouter prefix routing.
_OPENROUTER_VENDOR_TO_PRESET: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google_ai",
    "mistralai": "mistral",
    "mistral": "mistral",
    "deepseek": "deepseek",
    "groq": "groq",
    "x-ai": "xai",
    "xai": "xai",
    "minimax": "minimax",
    "cohere": "cohere",
    "meta-llama": "groq",  # llama-* → chat
    "qwen": "groq",
}


def _classify_generic_fallback(model_id: str) -> ModelKind | None:
    """Generic name-pattern fallback used when preset rules return ``None``."""
    lower = model_id.lower()
    if lower.startswith("ft:"):
        return "fine_tune"
    if "rerank" in lower:
        return "reranker"
    if "embed" in lower:
        return "embedding"
    if lower.startswith("whisper"):
        return "audio_stt"
    if lower.startswith("tts") or "-tts" in lower:
        return "audio_synth"
    if (
        lower.startswith("dall-e")
        or lower.startswith("gpt-image-")
        or lower.startswith("imagen-")
        or lower.startswith("image-")
    ):
        return "image_gen"
    if "clip" in lower:
        return "clip"
    if "moderation" in lower or "guard" in lower:
        return "moderation"
    return None


def classify_model(preset: str, model_id: str) -> ModelKind:
    """Classify a raw discovery id under a given preset.

    Returns one of the 12 ``ModelKind`` literals. The persisted
    ``Endpoint.model_kinds`` only ever stores ``"chat"`` or ``"embedding"``;
    the other categories surface in the *dropped* breakdown for the UI.
    """
    if not model_id:
        return "other"

    # ── 1) Catalog lookup ────────────────────────────────────────────────
    # ``preset_to_provider`` would translate google_ai→gemini etc.; the
    # catalog uses provider prefixes so try that, plus the raw preset prefix.
    candidate_ids: list[str] = []
    if "/" in model_id:
        candidate_ids.append(model_id)
    candidate_ids.append(f"{preset}/{model_id}")
    if preset == "google_ai":
        candidate_ids.append(f"gemini/{_strip_gemini_prefix(model_id)}")
    elif preset == "ollama":
        candidate_ids.append(f"ollama_chat/{model_id}")

    for cid in candidate_ids:
        spec = lookup_by_id(cid)
        if spec is None:
            continue
        kind = spec.get("kind")
        if kind == "embedding":
            return "embedding"
        if kind == "chat":
            return "chat"
        # "both" — fall through to preset rules.

    # ── 2) Preset-specific rules ─────────────────────────────────────────
    # Normalise the id we feed to per-preset classifiers.
    if preset == "google_ai":
        normalised = _strip_gemini_prefix(model_id)
    else:
        normalised = model_id

    # OpenRouter — dispatch on the ``<vendor>/`` prefix.
    if preset == "openrouter" and "/" in normalised:
        vendor, _, suffix = normalised.partition("/")
        target_preset = _OPENROUTER_VENDOR_TO_PRESET.get(vendor.lower())
        if target_preset is not None:
            inner = classify_model(target_preset, suffix)
            return inner
        # Unknown vendor — generic fallback below.
        normalised = suffix

    preset_classifier = {
        "openai": _classify_openai,
        "google_ai": _classify_google_ai,
        "gemini": _classify_google_ai,
        "anthropic": _classify_anthropic,
        "mistral": _classify_mistral,
        "deepseek": _classify_deepseek,
        "groq": _classify_groq,
        "xai": _classify_xai,
        "minimax": _classify_minimax,
        "cohere": _classify_cohere,
        "voyage": _classify_voyage,
        "jina_ai": _classify_jina,
    }.get(preset)

    if preset_classifier is not None:
        result = preset_classifier(normalised)
        if result is not None:
            return result

    # ── 3) Generic name-pattern fallback ────────────────────────────────
    fallback = _classify_generic_fallback(normalised)
    if fallback is not None:
        return fallback

    # ── 4) Default ───────────────────────────────────────────────────────
    if preset in _EMBEDDING_ONLY_PRESETS:
        return "other"
    if preset in _TRUSTED_CHAT_PRESETS:
        return "chat"
    # custom / litellm_proxy / vllm / lmstudio / openrouter (no vendor match)
    # — trust the operator's intent, conservative default to chat.
    return "chat"


__all__ = ["ModelKind", "classify_model"]
