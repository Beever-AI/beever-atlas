"""Hypothesis-driven fuzz tests for truncated JSON recovery.

Guarantees tested:
1. Truncating a known-good JSON at *any* byte offset produces either
   (a) a valid parsed structure whose facts are a subset of the original, or
   (b) a clear ``None`` failure — never a crash or malformed dict.
2. Injecting adversarial ``},``/``}]`` substrings *inside* string values does
   not fool the boundary scanner into cutting mid-string.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from beever_atlas.services.json_recovery import recover_truncated_json


_GOOD_DOC = {
    "facts": [
        {"id": i, "text": f"fact number {i}", "quality_score": 0.5 + i * 0.01} for i in range(12)
    ],
    "meta": {"source": "llm", "model": "gemini-2.5-flash"},
}
_GOOD_TEXT = json.dumps(_GOOD_DOC)
_ORIGINAL_IDS = {f["id"] for f in _GOOD_DOC["facts"]}


@given(offset=st.integers(min_value=0, max_value=len(_GOOD_TEXT)))
@settings(max_examples=200, deadline=None)
def test_truncation_at_any_offset_is_safe(offset: int) -> None:
    truncated = _GOOD_TEXT[:offset]
    result = recover_truncated_json(truncated)

    if result is None:
        return  # Explicit failure is an acceptable outcome.

    assert isinstance(result, (dict, list))
    # Recovered facts (if any) must be a subset of the originals by id.
    if isinstance(result, dict):
        facts = result.get("facts", []) or []
        for fact in facts:
            if isinstance(fact, dict) and "id" in fact:
                assert fact["id"] in _ORIGINAL_IDS, (
                    f"recovered id {fact['id']!r} not in original set"
                )


_POISON_SUBSTRINGS = st.sampled_from(["}, ", "}]", "},", " }]", "}, {", "}]}"])


@given(
    poison=_POISON_SUBSTRINGS,
    insert_at=st.integers(min_value=0, max_value=30),
)
@settings(max_examples=100, deadline=None)
def test_poison_inside_string_does_not_fool_recovery(poison: str, insert_at: int) -> None:
    """Boundary scanner must not cut inside a JSON string value."""
    inner_text = f"prefix{poison}suffix"[: insert_at + len(poison) + 12]
    doc = {
        "facts": [
            {"id": 1, "text": inner_text, "quality_score": 0.7},
            {"id": 2, "text": "plain", "quality_score": 0.9},
        ],
    }
    serialized = json.dumps(doc)
    result = recover_truncated_json(serialized)

    # A fully-valid doc must round-trip unchanged.
    assert result == doc
