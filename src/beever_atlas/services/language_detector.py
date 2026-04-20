"""Multi-language detector for ingestion.

Two-stage classification:

1. **Script fast-path** — for non-Latin scripts (CJK, Hangul, Hiragana/Katakana,
   Cyrillic, Arabic, Devanagari, Hebrew, Thai, Greek), a single pass over
   Unicode codepoints is sufficient and deterministic. This covers all the
   CJK dialects (zh-HK / zh-TW / zh-CN / ja / ko) where an English-trained
   LLM is most likely to drift.
2. **langdetect fallback** — for Latin-script messages (en/es/fr/de/pt/it/
   nl/sv/da/no/tr/vi/id/pl/cs/…), we delegate to the `langdetect` library
   which is trained on n-grams and handles the common European languages
   reliably. The library is seeded for deterministic output.

Every return value is a (BCP-47 tag, confidence ∈ [0, 1]) tuple. Short or
ambiguous messages yield low confidence; callers should fall back to the
channel's primary language in that case.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Script ranges. Non-Latin scripts each map to one or a family of BCP-47 tags.
# ---------------------------------------------------------------------------

_CJK_RANGES = [
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x20000, 0x2A6DF),  # CJK Extension B
]
_HIRAGANA_RANGE = (0x3040, 0x309F)
_KATAKANA_RANGE = (0x30A0, 0x30FF)
_HANGUL_RANGES = [
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0x1100, 0x11FF),  # Hangul Jamo
]
_CYRILLIC_RANGE = (0x0400, 0x04FF)
_ARABIC_RANGES = [
    (0x0600, 0x06FF),  # Arabic
    (0x0750, 0x077F),  # Arabic Supplement
    (0xFB50, 0xFDFF),  # Arabic Presentation Forms-A
    (0xFE70, 0xFEFF),  # Arabic Presentation Forms-B
]
_DEVANAGARI_RANGE = (0x0900, 0x097F)
_HEBREW_RANGE = (0x0590, 0x05FF)
_THAI_RANGE = (0x0E00, 0x0E7F)
_GREEK_RANGE = (0x0370, 0x03FF)

# Default script → BCP-47 mapping (used when we don't need sub-disambiguation).
# CJK / Cyrillic are split further below because they cover multiple langs.
_SCRIPT_DEFAULT_TAG: dict[str, str] = {
    "hiragana": "ja",
    "katakana": "ja",
    "hangul": "ko",
    "arabic": "ar",
    "devanagari": "hi",
    "hebrew": "he",
    "thai": "th",
    "greek": "el",
}

# Cantonese-diagnostic characters (HK written colloquial).
_CANTONESE_MARKERS = set("㗎喺咗嘅咁喎啦啲嚟咪咩冇嘢嗰佢哋仲唔啱喇嘞晒咋啩嚿")
# Simplified-only characters (not in Traditional).
_SIMPLIFIED_MARKERS = set("们这个为从发会来时对国说话还书车长见两点")

# langdetect returns ISO 639-1 (sometimes with region). Map to BCP-47 where
# different. Anything not in the map is returned as-is.
_LANGDETECT_TO_BCP47: dict[str, str] = {
    # langdetect uses zh-cn / zh-tw — normalize to canonical BCP-47 casing.
    "zh-cn": "zh-CN",
    "zh-tw": "zh-TW",
}


# ---------------------------------------------------------------------------
# langdetect integration (deterministic seed, optional dep)
#
# TODO(phase4-followup): langdetect is unmaintained (last release 2021-05).
# Evaluate replacement with `lingua-py` (active, higher accuracy, but ~200MB
# model download and different API surface). Migration touches:
#   - _langdetect_classify() — lingua uses Language enum, not ISO strings
#   - _LANGDETECT_TO_BCP47 map — lingua already emits ISO 639-1 names
#   - tests/test_language_detector.py — mock surface changes
# Estimated 2-4h including regression validation. Tracked separately.
# ---------------------------------------------------------------------------

_LANGDETECT_AVAILABLE = False
_detect_langs_fn = None
try:
    from langdetect import DetectorFactory  # type: ignore[import-not-found]
    from langdetect import detect_langs as _detect_langs_fn  # type: ignore[import-not-found]

    DetectorFactory.seed = 0  # deterministic
    _LANGDETECT_AVAILABLE = True
except ImportError:  # pragma: no cover
    logger.warning(
        "langdetect not installed — Latin-script languages will be "
        "classified as 'en'. Install `langdetect` for es/fr/de/pt/it/… support."
    )


def _langdetect_classify(text: str) -> tuple[str, float] | None:
    """Return the top (BCP-47 tag, confidence) from langdetect, or None on
    failure. Separate function so it's easy to mock in tests."""
    if not _LANGDETECT_AVAILABLE or _detect_langs_fn is None:
        return None
    try:
        results = _detect_langs_fn(text)
    except Exception:  # noqa: BLE001 — library throws LangDetectException on noise
        return None
    if not results:
        return None
    top = results[0]
    lang_str = str(getattr(top, "lang", "") or "")
    if not lang_str:
        return None
    tag = _LANGDETECT_TO_BCP47.get(lang_str.lower(), lang_str)
    return tag, float(getattr(top, "prob", 0.0) or 0.0)


# ---------------------------------------------------------------------------
# Script counting
# ---------------------------------------------------------------------------


def _in_range(cp: int, lo: int, hi: int) -> bool:
    return lo <= cp <= hi


def _is_latin_letter(ch: str) -> bool:
    """Latin script, including Latin Extended (accented chars é, ñ, ü, ß, ç…)."""
    if not ch.isalpha():
        return False
    ord(ch)
    # Basic Latin + Latin-1 Supplement + Latin Extended-A/B + Latin Extended
    # Additional = 0x0000–0x024F and 0x1E00–0x1EFF. Simpler check: unicodedata
    # block name starts with "LATIN".
    try:
        return unicodedata.name(ch, "").startswith("LATIN")
    except ValueError:
        return False


def _count_scripts(text: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for ch in text:
        cp = ord(ch)
        if any(_in_range(cp, lo, hi) for lo, hi in _CJK_RANGES):
            counts["cjk"] += 1
        elif _in_range(cp, *_HIRAGANA_RANGE):
            counts["hiragana"] += 1
        elif _in_range(cp, *_KATAKANA_RANGE):
            counts["katakana"] += 1
        elif any(_in_range(cp, lo, hi) for lo, hi in _HANGUL_RANGES):
            counts["hangul"] += 1
        elif _in_range(cp, *_CYRILLIC_RANGE):
            counts["cyrillic"] += 1
        elif any(_in_range(cp, lo, hi) for lo, hi in _ARABIC_RANGES):
            counts["arabic"] += 1
        elif _in_range(cp, *_DEVANAGARI_RANGE):
            counts["devanagari"] += 1
        elif _in_range(cp, *_HEBREW_RANGE):
            counts["hebrew"] += 1
        elif _in_range(cp, *_THAI_RANGE):
            counts["thai"] += 1
        elif _in_range(cp, *_GREEK_RANGE):
            counts["greek"] += 1
        elif _is_latin_letter(ch):
            counts["latin"] += 1
    counts["canto_markers"] = sum(1 for ch in text if ch in _CANTONESE_MARKERS)
    counts["simp_markers"] = sum(1 for ch in text if ch in _SIMPLIFIED_MARKERS)
    return dict(counts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_language(
    text: str,
    *,
    min_chars: int = 20,
) -> tuple[str, float]:
    """Classify `text` and return (BCP-47 tag, confidence ∈ [0.0, 1.0]).

    Non-Latin scripts are decided by script fast-path (deterministic, no dep).
    Latin-script text is delegated to `langdetect` when available; otherwise
    it degrades to "en" with a reduced confidence.

    Confidence encodes *strength of signal*: short messages and mixed-script
    messages yield lower confidence so callers can fall back to a channel
    primary.
    """
    if not text or not text.strip():
        return ("en", 0.0)
    text = unicodedata.normalize("NFC", text)

    # Strip URLs / @mentions / code blocks — they skew Latin counts heavily.
    stripped = re.sub(r"https?://\S+", " ", text)
    stripped = re.sub(r"@\w+|#\w+", " ", stripped)
    stripped = re.sub(r"`[^`]*`", " ", stripped)

    counts = _count_scripts(stripped)
    cjk = counts.get("cjk", 0)
    hira = counts.get("hiragana", 0)
    kata = counts.get("katakana", 0)
    hangul = counts.get("hangul", 0)
    cyrillic = counts.get("cyrillic", 0)
    arabic = counts.get("arabic", 0)
    devanagari = counts.get("devanagari", 0)
    hebrew = counts.get("hebrew", 0)
    thai = counts.get("thai", 0)
    greek = counts.get("greek", 0)
    latin = counts.get("latin", 0)
    total_script = (
        cjk + hira + kata + hangul + cyrillic + arabic + devanagari + hebrew + thai + greek + latin
    )

    if total_script == 0:
        return ("en", 0.0)

    # --- Script fast-path (non-Latin, non-ambiguous) ---

    if hangul > 0 and hangul / max(total_script, 1) >= 0.2:
        return ("ko", round(min(1.0, hangul / max(total_script, 1) + 0.2), 3))

    if hira + kata > 0:
        kana_ratio = (hira + kata) / max(total_script, 1)
        if kana_ratio >= 0.1 or (cjk > 0 and (hira + kata) >= 2):
            return ("ja", round(min(1.0, 0.6 + kana_ratio), 3))

    if cjk > 0:
        cjk_ratio = cjk / max(total_script, 1)
        canto = counts.get("canto_markers", 0)
        simp = counts.get("simp_markers", 0)

        # Cantonese particles short-circuit even in Latin-heavy code-switching.
        if canto >= 1:
            base_conf = min(1.0, 0.6 + cjk_ratio)
            if len(stripped.strip()) < min_chars:
                base_conf *= 0.85
            return ("zh-HK", round(base_conf, 3))

        if cjk_ratio < 0.2:
            # Tiny CJK loan inside a Latin-dominant string — fall through to
            # langdetect for the Latin portion.
            pass
        else:
            base_conf = min(1.0, cjk_ratio + 0.2)
            if len(stripped.strip()) < min_chars:
                base_conf *= 0.8
            if simp >= 2:
                return ("zh-CN", round(base_conf, 3))
            return ("zh-TW", round(base_conf, 3))

    # Other non-Latin scripts — each maps to a single default tag. When the
    # detector can be more specific (e.g. Cyrillic is ru vs uk vs bg), we
    # defer to langdetect; otherwise use the script default.
    for script, default_tag in _SCRIPT_DEFAULT_TAG.items():
        cnt = counts.get(script, 0)
        if cnt > 0 and cnt / max(total_script, 1) >= 0.3:
            # For Cyrillic, Greek, and Arabic we want langdetect's
            # sub-classification (ru vs uk, ar vs fa, el, …).
            if script in ("cyrillic", "greek", "arabic") and _LANGDETECT_AVAILABLE:
                ld = _langdetect_classify(stripped)
                if ld is not None:
                    tag, conf = ld
                    return (tag, round(min(1.0, conf), 3))
            return (default_tag, round(min(1.0, cnt / max(total_script, 1) + 0.2), 3))

    # Cyrillic without langdetect → default to Russian.
    if cyrillic > 0 and cyrillic / max(total_script, 1) >= 0.3:
        return ("ru", round(min(1.0, cyrillic / max(total_script, 1) + 0.2), 3))

    # --- Latin fast-path: defer to langdetect for en / es / fr / de / pt / …
    if latin > 0:
        latin_ratio = latin / max(total_script, 1)
        if _LANGDETECT_AVAILABLE and len(stripped.strip()) >= 10:
            ld = _langdetect_classify(stripped)
            if ld is not None:
                tag, conf = ld
                # Scale confidence down for very short inputs.
                if len(stripped.strip()) < min_chars:
                    conf *= 0.7
                return (tag, round(min(1.0, conf), 3))
        # Degrade to English when langdetect is unavailable or fails.
        conf = min(1.0, latin_ratio)
        if len(stripped.strip()) < min_chars:
            conf *= 0.7
        return ("en", round(conf, 3))

    # Everything else: unknown. Return default with zero confidence.
    return ("en", 0.0)


def detect_channel_primary_language(
    messages: list[str],
    *,
    min_samples: int = 20,
    confidence_threshold: float = 0.6,
    default: str = "en",
) -> tuple[str, float]:
    """Classify a channel's primary language from a list of message texts.

    Uses per-message `detect_language` and aggregates by weighted vote.
    Returns (BCP-47 tag, confidence ∈ [0, 1]). Falls back to `default` when
    no language clears `confidence_threshold`.
    """
    if not messages:
        return (default, 0.0)

    samples = [m for m in messages if m and len(m.strip()) >= 15]
    samples = samples[:min_samples] if samples else messages[:min_samples]

    votes: Counter[str] = Counter()
    conf_sum: dict[str, float] = {}
    for m in samples:
        lang, conf = detect_language(m)
        if conf <= 0.0:
            continue
        votes[lang] += 1
        conf_sum[lang] = conf_sum.get(lang, 0.0) + conf

    if not votes:
        return (default, 0.0)

    top_lang, top_votes = votes.most_common(1)[0]
    avg_conf = conf_sum[top_lang] / max(top_votes, 1)
    share = top_votes / sum(votes.values())
    final = round(min(1.0, avg_conf * 0.6 + share * 0.4), 3)

    if final < confidence_threshold:
        return (default, final)
    return (top_lang, final)
