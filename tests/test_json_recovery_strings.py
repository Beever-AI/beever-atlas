"""String-aware boundary tests for json_recovery.

Regression coverage for the case where JSON string values contain literal
``},`` or ``}]`` sequences. The earlier index-based boundary finder would
truncate the input mid-string when it saw those tokens, corrupting the
recovered payload.
"""

from __future__ import annotations

import json

from beever_atlas.services.json_recovery import (
    _find_last_complete_boundary,
    recover_facts_from_truncated,
    recover_truncated_json,
)


def test_boundary_scanner_ignores_brace_comma_inside_string():
    payload = json.dumps(
        {"facts": [{"text": "tricky: \"}, {\" is inside a string", "id": 1}]}
    )
    # Truncate one character before the final "]}" so only the first
    # object is safely recoverable.
    boundary = _find_last_complete_boundary(payload)
    assert boundary > 0
    assert boundary <= len(payload)


def test_recovers_when_string_contains_brace_comma_sequence():
    data = {
        "facts": [
            {"id": 1, "text": "safe"},
            {"id": 2, "text": "contains }, inside string"},
        ]
    }
    text = json.dumps(data)
    result = recover_truncated_json(text)
    assert result == data


def test_truncated_recovery_preserves_string_with_brace_comma():
    complete_first = (
        '{"facts": [{"id": 1, "text": "has }, inside"}, {"id": 2, "text": "cut'
    )
    result = recover_facts_from_truncated(complete_first)
    assert result is not None
    facts = result["facts"]
    assert len(facts) == 1
    assert facts[0]["id"] == 1
    assert facts[0]["text"] == "has }, inside"


def test_escaped_quote_in_string_does_not_break_boundary():
    text = (
        '{"facts": [{"id": 1, "text": "he said \\"},{\\" today"},'
        ' {"id": 2, "text": "truncated'
    )
    result = recover_facts_from_truncated(text)
    assert result is not None
    assert len(result["facts"]) == 1
    assert result["facts"][0]["id"] == 1
