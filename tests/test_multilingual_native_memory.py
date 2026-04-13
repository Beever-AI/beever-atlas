"""Integration tests for the multilingual-native-memory change.

These tests exercise the non-LLM pieces end-to-end:

1. Language detection on real-shaped chat fixtures.
2. Extractor prompt templates accept {source_language} and substitute correctly.
3. Schemas (Fact/Entity) carry source_lang all the way through.
4. Wiki compiler prepends the language directive header with target_lang.
5. QA system prompt contains the language directive.
6. ChannelSyncState supports primary_language / confidence.

No network/LLM calls. Runs in <1s.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from beever_atlas.agents.prompts.entity_extractor import ENTITY_EXTRACTOR_INSTRUCTION
from beever_atlas.agents.prompts.fact_extractor import FACT_EXTRACTOR_INSTRUCTION
from beever_atlas.agents.query.prompts import build_qa_system_prompt
from beever_atlas.agents.schemas.extraction import ExtractedEntity, ExtractedFact
from beever_atlas.models.domain import AtomicFact, GraphEntity
from beever_atlas.models.persistence import ChannelSyncState
from beever_atlas.services.language_detector import (
    detect_channel_primary_language,
    detect_language,
)
from beever_atlas.wiki.compiler import WikiCompiler


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _stub_llm_provider(monkeypatch):
    """WikiCompiler.__init__ calls get_llm_provider() to resolve the model name.
    These tests never hit an LLM — stub it out so tests run without app startup.
    """
    class _Provider:
        def get_model_string(self, _key: str) -> str:
            return "stub/test-model"

    import beever_atlas.llm.provider as _provider_mod
    monkeypatch.setattr(_provider_mod, "_provider", _Provider(), raising=False)
    yield


def _load_csv_contents(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [row["content"] for row in reader]


# ---------------------------------------------------------------------------
# 1. Language detection on real-shape fixtures
# ---------------------------------------------------------------------------


class TestFixtureLanguageDetection:
    def test_cantonese_fixture_detects_as_zh_hk(self) -> None:
        msgs = _load_csv_contents(FIXTURES / "chat_cantonese.csv")
        assert len(msgs) >= 15, "fixture should have enough messages for channel detection"
        tag, conf = detect_channel_primary_language(msgs)
        assert tag == "zh-HK"
        assert conf >= 0.6

    def test_mixed_fixture_still_resolves_to_a_supported_tag(self) -> None:
        msgs = _load_csv_contents(FIXTURES / "chat_mixed_en_zh.csv")
        tag, _ = detect_channel_primary_language(msgs)
        # Bilingual channel — either zh-HK (majority-vote by canto particles)
        # or en is acceptable. The key contract: it doesn't crash and doesn't
        # return something unsupported.
        assert tag in ("zh-HK", "zh-TW", "zh-CN", "en")

    def test_english_only_channel_stays_en(self) -> None:
        msgs = [
            "Alice decided to use Redis for session caching after evaluating Memcached",
            "Bob will update the API docs before Friday's release to unblock partner integrations",
            "API latency on the /search endpoint reached 200ms, exceeding the 150ms SLA",
            "The team agreed to adopt TypeScript for the frontend rewrite next quarter",
        ]
        tag, _ = detect_channel_primary_language(msgs)
        assert tag == "en"


# ---------------------------------------------------------------------------
# 2. Extractor prompts accept {source_language}
# ---------------------------------------------------------------------------


class TestExtractorPromptSubstitution:
    def test_fact_extractor_prompt_has_source_language_placeholder(self) -> None:
        assert "{source_language}" in FACT_EXTRACTOR_INSTRUCTION
        # Has a CJK calibration example.
        assert "阿明" in FACT_EXTRACTOR_INSTRUCTION

    def test_entity_extractor_prompt_has_source_language_placeholder(self) -> None:
        assert "{source_language}" in ENTITY_EXTRACTOR_INSTRUCTION
        assert "阿明" in ENTITY_EXTRACTOR_INSTRUCTION

    def test_fact_extractor_prompt_substitutes_cleanly(self) -> None:
        rendered = FACT_EXTRACTOR_INSTRUCTION.format(
            source_language="zh-HK",
            channel_name="#test",
            preprocessed_messages="[]",
            max_facts_per_message=2,
        )
        assert "zh-HK" in rendered
        assert "{source_language}" not in rendered  # all placeholders resolved

    def test_entity_extractor_prompt_substitutes_cleanly(self) -> None:
        rendered = ENTITY_EXTRACTOR_INSTRUCTION.format(
            source_language="ja",
            channel_name="#test",
            channel_id="C123",
            known_entities="[]",
            preprocessed_messages="[]",
        )
        assert "ja" in rendered
        assert "{source_language}" not in rendered


# ---------------------------------------------------------------------------
# 3. Schemas carry source_lang
# ---------------------------------------------------------------------------


class TestSchemaSourceLang:
    def test_extracted_fact_has_source_lang_default_en(self) -> None:
        f = ExtractedFact(memory_text="Alice decided X", quality_score=0.8)
        assert f.source_lang == "en"

    def test_extracted_fact_accepts_non_default_source_lang(self) -> None:
        f = ExtractedFact(
            memory_text="阿明決定用 Redis",
            quality_score=0.8,
            source_lang="zh-HK",
        )
        assert f.source_lang == "zh-HK"

    def test_extracted_entity_accepts_source_lang(self) -> None:
        e = ExtractedEntity(name="阿明", type="Person", source_lang="zh-HK")
        assert e.source_lang == "zh-HK"

    def test_atomic_fact_has_source_lang_default(self) -> None:
        f = AtomicFact(memory_text="hello")
        assert f.source_lang == "en"

    def test_atomic_fact_roundtrips_source_lang(self) -> None:
        f = AtomicFact(memory_text="阿明決定用 Redis", source_lang="zh-HK")
        dumped = f.model_dump()
        restored = AtomicFact(**dumped)
        assert restored.source_lang == "zh-HK"

    def test_graph_entity_has_source_lang_default(self) -> None:
        e = GraphEntity(name="Redis", type="Technology")
        assert e.source_lang == "en"

    def test_graph_entity_roundtrips_source_lang(self) -> None:
        e = GraphEntity(name="阿明", type="Person", source_lang="zh-HK")
        dumped = e.model_dump()
        restored = GraphEntity(**dumped)
        assert restored.source_lang == "zh-HK"


# ---------------------------------------------------------------------------
# 4. Wiki compiler prepends the language directive
# ---------------------------------------------------------------------------


class TestWikiCompilerLangHeader:
    def test_compiler_accepts_target_and_source_lang(self) -> None:
        c = WikiCompiler(target_lang="en", source_lang="zh-HK")
        assert c._target_lang == "en"
        assert c._source_lang == "zh-HK"

    def test_compiler_defaults_are_en(self) -> None:
        c = WikiCompiler()
        assert c._target_lang == "en"
        assert c._source_lang == "en"

    def test_fmt_prompt_prepends_language_header(self) -> None:
        c = WikiCompiler(target_lang="en", source_lang="zh-HK")
        # A minimal template with no placeholders still works.
        out = c._fmt_prompt("body content here")
        assert "Language Directive" in out
        assert "en" in out  # target_language substituted
        assert "zh-HK" in out  # source_language substituted
        assert out.endswith("body content here")

    def test_fmt_prompt_passes_kwargs_to_template(self) -> None:
        c = WikiCompiler(target_lang="zh-HK", source_lang="zh-HK")
        out = c._fmt_prompt("Channel: {channel_name}", channel_name="#backend")
        assert "Channel: #backend" in out
        assert "zh-HK" in out


# ---------------------------------------------------------------------------
# 5. QA system prompt carries the language directive
# ---------------------------------------------------------------------------


class TestQaLanguageDirective:
    def test_build_qa_prompt_contains_language_directive(self) -> None:
        prompt = build_qa_system_prompt()
        assert "## Language" in prompt
        assert "same language" in prompt.lower()

    def test_qa_prompt_mentions_preserving_proper_nouns(self) -> None:
        prompt = build_qa_system_prompt()
        assert "Preserve proper nouns" in prompt or "preserve proper nouns" in prompt.lower()


# ---------------------------------------------------------------------------
# 6. ChannelSyncState supports primary_language
# ---------------------------------------------------------------------------


class TestChannelSyncStateLanguage:
    def test_default_primary_language_is_en(self) -> None:
        s = ChannelSyncState(channel_id="C1", last_sync_ts="2026-04-08T10:00:00Z")
        assert s.primary_language == "en"
        assert s.primary_language_confidence == 0.0

    def test_primary_language_roundtrips(self) -> None:
        s = ChannelSyncState(
            channel_id="C1",
            last_sync_ts="2026-04-08T10:00:00Z",
            primary_language="zh-HK",
            primary_language_confidence=0.82,
        )
        dumped = s.model_dump()
        restored = ChannelSyncState(**dumped)
        assert restored.primary_language == "zh-HK"
        assert restored.primary_language_confidence == pytest.approx(0.82)


# ---------------------------------------------------------------------------
# 7. End-to-end sanity: fixture → detect → would-be-tagged on Fact/Entity
# ---------------------------------------------------------------------------


class TestFixtureEndToEnd:
    def test_cantonese_fixture_would_tag_facts_zh_hk(self) -> None:
        msgs = _load_csv_contents(FIXTURES / "chat_cantonese.csv")
        channel_lang, _ = detect_channel_primary_language(msgs)
        assert channel_lang == "zh-HK"

        # Construct a fact as the persister would — inheriting channel lang.
        f = AtomicFact(
            memory_text="阿明決定用 Redis 做 session cache",
            quality_score=0.9,
            source_lang=channel_lang,
        )
        assert f.source_lang == "zh-HK"

        # And an entity.
        e = GraphEntity(name="阿明", type="Person", source_lang=channel_lang)
        assert e.source_lang == "zh-HK"

    def test_per_message_language_override(self) -> None:
        # In a zh-HK channel, a long English-only message should be
        # re-classified when detection is confident enough.
        channel_lang = "zh-HK"
        en_msg = (
            "Jordan kicked off the Neo4j upgrade discussion and confirmed the "
            "migration plan for next Tuesday's maintenance window."
        )
        per_msg, msg_conf = detect_language(en_msg)
        assert per_msg == "en"
        assert msg_conf >= 0.85
        # Simulated per-message override rule from spec (conf>=0.85 AND len>=20)
        effective = per_msg if (msg_conf >= 0.85 and len(en_msg) >= 20) else channel_lang
        assert effective == "en"
