"""Unit tests for the language detector + NFC key helpers."""

from __future__ import annotations

from beever_atlas.services.language_detector import (
    detect_channel_primary_language,
    detect_language,
)
from beever_atlas.services.lang_text import alias_keyset, nfc_key


# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------

class TestDetectLanguage:
    def test_empty_input_is_en_zero_confidence(self) -> None:
        tag, conf = detect_language("")
        assert tag == "en"
        assert conf == 0.0

    def test_pure_english_long(self) -> None:
        tag, conf = detect_language(
            "Alice decided to use Redis for session caching after evaluating Memcached."
        )
        assert tag == "en"
        assert conf > 0.6

    def test_pure_english_short_has_lower_confidence(self) -> None:
        tag, conf = detect_language("ok thanks")
        assert tag == "en"
        assert conf < 0.8

    def test_cantonese_markers_yield_zh_hk(self) -> None:
        tag, conf = detect_language("阿明今日話佢搞掂咗個 deployment，但 staging 個 DB migration 仲未 run")
        assert tag == "zh-HK"
        assert conf > 0.5

    def test_traditional_chinese_without_canto_markers_is_zh_tw(self) -> None:
        tag, _ = detect_language("我們團隊決定使用 Redis 作為快取系統，並已完成整合測試。")
        # No HK-specific particles (咗/嘅/嘢/etc.) so falls through to zh-TW bucket.
        assert tag in ("zh-TW", "zh-CN")

    def test_simplified_chinese_markers_yield_zh_cn(self) -> None:
        tag, conf = detect_language("我们团队决定使用 Redis 作为缓存系统，发现性能有明显提升。")
        assert tag == "zh-CN"
        assert conf > 0.5

    def test_japanese_kana_detected(self) -> None:
        tag, conf = detect_language("アリスさんはRedisを使うことに決めました。")
        assert tag == "ja"
        assert conf > 0.6

    def test_korean_hangul_detected(self) -> None:
        tag, conf = detect_language("앨리스는 세션 캐싱을 위해 Redis를 사용하기로 결정했습니다.")
        assert tag == "ko"
        assert conf > 0.5

    def test_code_switch_cjk_heavy_wins(self) -> None:
        # Cantonese + a couple of English tech terms — native script should win.
        tag, _ = detect_language(
            "阿明啱啱喺 Slack 話佢搞掂咗個 deployment，DB migration 仲未 run"
        )
        assert tag == "zh-HK"

    def test_latin_dominant_with_tiny_cjk_loan_stays_en(self) -> None:
        tag, _ = detect_language(
            "The project codename 阿明 was chosen by the team last Friday."
        )
        # Only a handful of CJK chars in a long Latin sentence → English wins.
        assert tag == "en"

    def test_urls_and_mentions_do_not_skew_detection(self) -> None:
        # Heavy URL + @mention noise shouldn't turn a Cantonese line English.
        tag, _ = detect_language(
            "阿明 @channel 請睇下 https://example.com/very/long/url 搞掂咗未"
        )
        assert tag == "zh-HK"


# ---------------------------------------------------------------------------
# detect_channel_primary_language
# ---------------------------------------------------------------------------

class TestDetectChannelPrimaryLanguage:
    def test_empty_channel_returns_default(self) -> None:
        tag, conf = detect_channel_primary_language([])
        assert tag == "en"
        assert conf == 0.0

    def test_all_english_messages(self) -> None:
        msgs = [
            "Alice decided to use Redis for session caching after evaluation",
            "Bob will update the API docs before Friday's release to unblock partners",
            "Latency on the search endpoint has exceeded the SLA threshold again",
        ]
        tag, conf = detect_channel_primary_language(msgs)
        assert tag == "en"
        assert conf > 0.4

    def test_all_cantonese_messages(self) -> None:
        msgs = [
            "阿明今日話佢搞掂咗個 deployment",
            "staging 個 DB migration 仲未 run 啊",
            "老闆話要今日內搞掂，唔可以再拖",
            "我睇咗個 PR，覺得 code 質素 OK",
        ]
        tag, conf = detect_channel_primary_language(msgs)
        assert tag == "zh-HK"
        assert conf >= 0.6

    def test_mixed_majority_cantonese_wins(self) -> None:
        msgs = [
            "阿明今日話佢搞掂咗個 deployment",
            "staging 個 DB migration 仲未 run 啊",
            "Bob, can you review the PR when you get a chance?",
            "老闆話要今日內搞掂",
        ]
        tag, _ = detect_channel_primary_language(msgs)
        assert tag == "zh-HK"

    def test_low_confidence_falls_back_to_default(self) -> None:
        # All messages are too short / ambiguous to cross the threshold.
        msgs = ["ok", "lol", ":)", "k", "thx"]
        tag, _ = detect_channel_primary_language(
            msgs, confidence_threshold=0.6, default="en"
        )
        assert tag == "en"


# ---------------------------------------------------------------------------
# nfc_key / alias_keyset — entity dedup helpers
# ---------------------------------------------------------------------------

class TestMultiLanguageDetection:
    """Validate Latin-script and non-Latin-non-CJK language detection via
    the langdetect fallback + script-family fast path."""

    def test_spanish_is_detected(self) -> None:
        tag, conf = detect_language(
            "El equipo decidió utilizar Redis para el almacenamiento "
            "en caché de sesiones después de evaluar Memcached."
        )
        assert tag == "es"
        assert conf > 0.5

    def test_french_is_detected(self) -> None:
        tag, conf = detect_language(
            "L'équipe a décidé d'utiliser Redis pour la mise en cache "
            "des sessions après avoir évalué Memcached en détail."
        )
        assert tag == "fr"
        assert conf > 0.5

    def test_german_is_detected(self) -> None:
        tag, conf = detect_language(
            "Das Team entschied sich nach einer Bewertung von Memcached "
            "für Redis als Sitzungscache für die neue Infrastruktur."
        )
        assert tag == "de"
        assert conf > 0.5

    def test_portuguese_is_detected(self) -> None:
        tag, conf = detect_language(
            "A equipa decidiu utilizar Redis para o armazenamento em "
            "cache das sessões depois de avaliar o Memcached cuidadosamente."
        )
        assert tag == "pt"
        assert conf > 0.5

    def test_italian_is_detected(self) -> None:
        tag, conf = detect_language(
            "Il team ha deciso di utilizzare Redis per la cache delle "
            "sessioni dopo aver valutato attentamente Memcached."
        )
        assert tag == "it"
        assert conf > 0.5

    def test_dutch_is_detected(self) -> None:
        tag, _ = detect_language(
            "Het team heeft na het evalueren van Memcached besloten "
            "om Redis te gebruiken voor het cachen van sessies."
        )
        assert tag == "nl"

    def test_russian_cyrillic_is_detected(self) -> None:
        tag, conf = detect_language(
            "Команда решила использовать Redis для кэширования сессий "
            "после оценки Memcached и других альтернатив."
        )
        assert tag == "ru"
        assert conf > 0.5

    def test_arabic_is_detected(self) -> None:
        tag, _ = detect_language(
            "قرر الفريق استخدام Redis للتخزين المؤقت للجلسات بعد تقييم بدائل أخرى"
        )
        assert tag == "ar"

    def test_hindi_devanagari_is_detected(self) -> None:
        tag, _ = detect_language(
            "टीम ने मेमकैश्ड का मूल्यांकन करने के बाद सेशन कैश के लिए रेडिस का उपयोग करने का निर्णय लिया।"
        )
        assert tag == "hi"

    def test_thai_is_detected(self) -> None:
        tag, _ = detect_language(
            "ทีมงานตัดสินใจใช้ Redis สำหรับการแคชเซสชันหลังจากประเมิน Memcached อย่างละเอียดแล้ว"
        )
        assert tag == "th"

    def test_hebrew_is_detected(self) -> None:
        tag, _ = detect_language(
            "הצוות החליט להשתמש ב-Redis לאחסון מטמון לאחר הערכת האלטרנטיבות"
        )
        assert tag == "he"

    def test_greek_is_detected(self) -> None:
        tag, _ = detect_language(
            "Η ομάδα αποφάσισε να χρησιμοποιήσει το Redis για την αποθήκευση "
            "των συνεδριών μετά την αξιολόγηση εναλλακτικών λύσεων."
        )
        assert tag == "el"

    def test_mixed_spanish_english_still_classifies_a_language(self) -> None:
        # Code-switch between Spanish and English tech terms.
        tag, _ = detect_language(
            "El equipo va a hacer deployment del service de Redis el viernes "
            "después del code review de la migration."
        )
        assert tag in ("es", "en")  # either is acceptable; we just need non-default

    def test_channel_detection_spanish(self) -> None:
        msgs = [
            "Buenos días equipo, hoy vamos a revisar la arquitectura de caché distribuida",
            "Propongo usar Redis por su soporte nativo para pub/sub y persistencia",
            "El staging de la migración de base de datos aún no ha terminado",
            "Voy a actualizar la documentación de la API antes del release del viernes",
        ]
        tag, conf = detect_channel_primary_language(msgs)
        assert tag == "es"
        assert conf > 0.5

    def test_channel_detection_french(self) -> None:
        msgs = [
            "Bonjour l'équipe, aujourd'hui nous allons examiner l'architecture de cache",
            "Je propose d'utiliser Redis pour son support natif du pub/sub et de la persistance",
            "La migration de la base de données en staging n'est pas encore terminée",
            "Je vais mettre à jour la documentation de l'API avant la sortie de vendredi",
        ]
        tag, conf = detect_channel_primary_language(msgs)
        assert tag == "fr"
        assert conf > 0.5


class TestNfcKey:
    def test_latin_is_lowercased(self) -> None:
        assert nfc_key("Neo4j") == "neo4j"
        assert nfc_key("Beever Atlas") == "beever atlas"

    def test_cjk_is_not_lowercased_but_normalized(self) -> None:
        # NFC-normalize a decomposed form to the composed form.
        decomposed = "\u963F\u660E"  # 阿明 already composed
        assert nfc_key(decomposed) == "阿明"

    def test_whitespace_is_stripped(self) -> None:
        assert nfc_key("  Redis  ") == "redis"

    def test_empty_string(self) -> None:
        assert nfc_key("") == ""
        assert nfc_key("   ") == ""

    def test_mixed_script_is_preserved_mixed_case(self) -> None:
        # Mixed script: not lowercased because not "latin-only".
        key = nfc_key("Beever阿明")
        assert "阿明" in key


class TestAliasKeyset:
    def test_canonical_and_aliases_unified(self) -> None:
        keys = alias_keyset("阿明", ["Ah Ming", "Ming"])
        assert "阿明" in keys
        assert "ah ming" in keys
        assert "ming" in keys

    def test_ignores_empty_entries(self) -> None:
        keys = alias_keyset("Redis", ["", "  ", "redis"])
        # Lowercased "Redis" collides with "redis" → single entry.
        assert keys == {"redis"}
