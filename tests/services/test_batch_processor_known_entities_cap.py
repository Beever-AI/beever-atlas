"""RES-945 — the known-entity registry injected into every extraction batch must
be bounded so a large corpus cannot push the serialised prompt past the model
input-token ceiling (which made LiteLLM reject every batch → 0 facts on the RLP
36k-doc run).

Covers ``_cap_known_entities`` (keep most-connected entities first, deterministic,
0 disables) and the config default ``extraction_known_entities_max``.
"""

from __future__ import annotations

from beever_atlas.infra.config import Settings
from beever_atlas.services.batch_processor import _cap_known_entities


def _ent(name: str, alias_count: int) -> dict:
    return {"name": name, "type": "PERSON", "aliases": [f"{name}-{i}" for i in range(alias_count)]}


def test_under_limit_returns_input_unchanged() -> None:
    ents = [_ent("a", 1), _ent("b", 2)]
    assert _cap_known_entities(ents, 10) is ents


def test_caps_to_max_keeping_most_aliased_first() -> None:
    ents = [_ent("low", 0), _ent("high", 9), _ent("mid", 4)]
    capped = _cap_known_entities(ents, 2)
    assert [e["name"] for e in capped] == ["high", "mid"]  # dropped the 0-alias tail


def test_tie_break_is_deterministic_by_name() -> None:
    # All same alias count → must fall back to name-ascending, stable across runs.
    ents = [_ent("charlie", 3), _ent("alpha", 3), _ent("bravo", 3)]
    capped = _cap_known_entities(ents, 2)
    assert [e["name"] for e in capped] == ["alpha", "bravo"]


def test_zero_disables_cap() -> None:
    ents = [_ent(str(i), i) for i in range(50)]
    assert _cap_known_entities(ents, 0) is ents


def test_negative_disables_cap() -> None:
    ents = [_ent("x", 1)]
    assert _cap_known_entities(ents, -1) is ents


def test_missing_or_malformed_aliases_does_not_raise() -> None:
    ents = [{"name": "n", "type": "ORG"}, {"name": "m", "aliases": None}, _ent("k", 5)]
    capped = _cap_known_entities(ents, 2)
    # The 5-alias entity is the most prominent and must survive; no KeyError/TypeError.
    assert "k" in [e["name"] for e in capped]
    assert len(capped) == 2


def test_config_default_fits_both_model_windows() -> None:
    # 500 canonical entities serialise to ~15-20k tokens — safe under Gemini's
    # 1,048,576 window AND the self-hosted Qwen 64k window (RES-944 / F1).
    s = Settings()
    assert s.extraction_known_entities_max == 500
