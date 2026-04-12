"""CSV / JSONL column-mapping orchestrator.

Pipeline:
    preset exact-match
    → fuzzy regex (short-circuits when overall confidence ≥ fuzzy_threshold)
    → one LLM call
    → validate LLM output against real headers
    → fall back to fuzzy on any failure, with needs_review=True

The LLM is called at most once per file regardless of size.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from beever_atlas.agents.prompts.csv_mapper import (
    CSV_MAPPER_INSTRUCTION,
    build_user_prompt,
)
from beever_atlas.agents.schemas.csv_mapping import LLMColumnMapping
from beever_atlas.services.file_importer import (
    ColumnMapping,
    PreviewResult,
    detect_preset,
    fuzzy_match,
    overall_fuzzy_confidence,
    preview_file,
    read_headers_and_samples,
    validate_mapping,
)

logger = logging.getLogger(__name__)

# Fuzzy confidence at or above which we skip the LLM entirely.
FUZZY_SHORT_CIRCUIT_THRESHOLD = 0.9


@dataclass
class MappingResult:
    mapping: ColumnMapping
    source: str  # "preset" | "fuzzy" | "llm" | "fuzzy_fallback"
    preset: str | None
    confidence: dict[str, float]
    overall_confidence: float
    needs_review: bool
    notes: str = ""
    detected_source: str | None = None


def _llm_to_column_mapping(llm: LLMColumnMapping) -> ColumnMapping:
    return ColumnMapping(
        content=llm.content,
        author=llm.author,
        author_name=llm.author_name or llm.author,
        timestamp=llm.timestamp,
        timestamp_time=llm.timestamp_time,
        message_id=llm.message_id,
        thread_id=llm.thread_id,
        attachments=llm.attachments,
        reactions=llm.reactions,
    )


async def _call_llm_mapper(
    filename: str,
    headers: list[str],
    samples: list[dict[str, str]],
    model: str | None = None,
) -> LLMColumnMapping | None:
    """Invoke Gemini (via google.genai) in JSON mode. Returns None on any error."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logger.warning("csv_mapper: google-genai not available, skipping LLM step")
        return None

    try:
        from beever_atlas.llm import get_llm_provider
        model_name = model or get_llm_provider().get_model_string("csv_mapper")
    except Exception:
        model_name = model or "gemini-2.5-flash"

    user_prompt = build_user_prompt(filename, headers, samples)
    try:
        client = genai.Client()
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=[
                {"role": "user", "parts": [{"text": CSV_MAPPER_INSTRUCTION}]},
                {"role": "user", "parts": [{"text": user_prompt}]},
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        raw = (response.text or "").strip()
    except Exception as exc:
        logger.warning("csv_mapper: LLM call failed: %s", exc)
        return None

    if not raw:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("csv_mapper: LLM returned non-JSON: %s", exc)
        return None

    try:
        return LLMColumnMapping.model_validate(data)
    except Exception as exc:
        logger.warning("csv_mapper: LLM output failed schema validation: %s", exc)
        return None


async def infer_mapping(
    path: Path,
    *,
    use_llm: bool = True,
    model: str | None = None,
) -> MappingResult:
    """Run the full inference pipeline against ``path``."""
    preview: PreviewResult = preview_file(path)

    # 1. Exact preset
    if preview.preset is not None:
        return MappingResult(
            mapping=preview.mapping,
            source="preset",
            preset=preview.preset,
            confidence=preview.confidence,
            overall_confidence=preview.overall_confidence,
            needs_review=False,
        )

    fuzzy_map = preview.mapping
    fuzzy_conf = preview.confidence
    fuzzy_overall = preview.overall_confidence

    # 2. Fuzzy high-confidence short-circuit
    if fuzzy_overall >= FUZZY_SHORT_CIRCUIT_THRESHOLD:
        return MappingResult(
            mapping=fuzzy_map,
            source="fuzzy",
            preset=None,
            confidence=fuzzy_conf,
            overall_confidence=fuzzy_overall,
            needs_review=False,
        )

    # 3. LLM (optional) — one call only
    if use_llm:
        llm_out = await _call_llm_mapper(
            filename=path.name,
            headers=preview.headers,
            samples=preview.samples,
            model=model,
        )
        if llm_out is not None:
            candidate = _llm_to_column_mapping(llm_out)
            errors = validate_mapping(candidate, preview.headers)
            if not errors:
                return MappingResult(
                    mapping=candidate,
                    source="llm",
                    preset=None,
                    confidence=llm_out.confidence or {},
                    overall_confidence=_llm_overall(llm_out.confidence),
                    needs_review=False,
                    notes=llm_out.notes,
                    detected_source=llm_out.detected_source,
                )
            logger.warning(
                "csv_mapper: LLM produced invalid mapping (%s); falling back to fuzzy",
                "; ".join(errors),
            )

    # 4. Fallback: fuzzy with needs_review flagged
    return MappingResult(
        mapping=fuzzy_map,
        source="fuzzy_fallback",
        preset=None,
        confidence=fuzzy_conf,
        overall_confidence=fuzzy_overall,
        needs_review=True,
    )


def _llm_overall(conf: dict[str, float]) -> float:
    if not conf:
        return 0.0
    content = conf.get("content", 0.0)
    if content == 0.0:
        return 0.0
    base = content * 0.5
    base += conf.get("timestamp", 0.0) * 0.3
    base += max(conf.get("author_name", 0.0), conf.get("author", 0.0)) * 0.2
    return round(base, 3)


# Convenience sync entry point used by CLI scripts that don't want async.
def infer_mapping_deterministic(path: Path) -> MappingResult:
    """Preset + fuzzy only, no LLM. Always safe to call from sync code."""
    headers, _samples, _fmt = read_headers_and_samples(path)
    preset = detect_preset(headers)
    if preset is not None:
        return MappingResult(
            mapping=preset.mapping,
            source="preset",
            preset=preset.name,
            confidence={"content": 1.0, "author_name": 1.0, "timestamp": 1.0},
            overall_confidence=1.0,
            needs_review=False,
        )
    mapping, conf = fuzzy_match(headers)
    overall = overall_fuzzy_confidence(conf)
    return MappingResult(
        mapping=mapping,
        source="fuzzy" if overall >= FUZZY_SHORT_CIRCUIT_THRESHOLD else "fuzzy_fallback",
        preset=None,
        confidence=conf,
        overall_confidence=overall,
        needs_review=overall < FUZZY_SHORT_CIRCUIT_THRESHOLD,
    )
