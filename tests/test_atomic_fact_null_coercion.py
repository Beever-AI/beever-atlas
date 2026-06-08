"""Regression: AtomicFact must tolerate ``None`` for plain-str metadata fields.

Rows written to Weaviate before a field existed (or for platforms that don't
populate it) read back as an explicit ``null`` property. A field default only
applies when the key is ABSENT — not present-but-None — so ``_obj_to_fact``
crashed with ``ValidationError: guild_id Input should be a valid string`` when
listing legacy facts (observed on the EE demo after the OSS sync: non-Discord
facts have ``guild_id=None``). The model coerces these to "" on read.
"""

from __future__ import annotations

import pytest

from beever_atlas.models.domain import AtomicFact

# Plain-str fields that carry a default and are read back from stored objects,
# so a legacy/explicit None must coerce to "" rather than raise.
_COERCED_FIELDS = [
    "channel_id",
    "platform",
    "guild_id",
    "author_id",
    "author_name",
    "message_ts",
    "source_message_id",
    "importance",
    "fact_type",
    "source_lang",
    "tier",
    "thread_context_summary",
    "derived_from",
    "source_media_url",
    "source_media_type",
]


@pytest.mark.parametrize("field", _COERCED_FIELDS)
def test_none_coerces_to_empty_string(field: str) -> None:
    fact = AtomicFact(memory_text="x", **{field: None})
    assert getattr(fact, field) == "", f"{field}=None should coerce to ''"


def test_all_metadata_none_at_once_does_not_raise() -> None:
    """The real legacy-row shape: many str props come back null together."""
    fact = AtomicFact(memory_text="legacy fact", **{f: None for f in _COERCED_FIELDS})
    assert fact.guild_id == ""
    assert fact.platform == ""
    assert fact.memory_text == "legacy fact"


def test_present_values_are_preserved() -> None:
    fact = AtomicFact(memory_text="x", guild_id="123", platform="discord")
    assert fact.guild_id == "123"
    assert fact.platform == "discord"
