"""Tests for services.file_importer and agents.ingestion.csv_mapper."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from beever_atlas.agents.ingestion.csv_mapper import (
    infer_mapping_deterministic,
)
from beever_atlas.services.file_importer import (
    ColumnMapping,
    ParseOptions,
    detect_encoding,
    detect_format,
    detect_preset,
    fuzzy_match,
    overall_fuzzy_confidence,
    parse_file,
    preview_file,
    read_headers_and_samples,
    validate_mapping,
)


# ---------------------------------------------------------------------------
# Fixtures — write each CSV format to a tmp_path to keep tests hermetic.
# ---------------------------------------------------------------------------


DISCORD_CSV = (
    'AuthorID,Author,Date,Content,Attachments,Reactions\n'
    '"111","alice","2024-01-01T10:00:00+00:00","hello world","",""\n'
    '"111","alice","2024-01-01T10:00:05+00:00","Pinned a message.","",""\n'
    '"222","bob","2024-01-01T11:30:00+00:00","酒干倘賣無","",""\n'
)


def _write(tmp_path: Path, name: str, content: str, encoding: str = "utf-8") -> Path:
    p = tmp_path / name
    p.write_bytes(content.encode(encoding))
    return p


# ---------------------------------------------------------------------------
# Encoding / format detection
# ---------------------------------------------------------------------------


def test_detect_encoding_utf8(tmp_path: Path) -> None:
    p = _write(tmp_path, "a.csv", DISCORD_CSV, "utf-8")
    assert detect_encoding(p) == "utf-8"


def test_detect_encoding_big5(tmp_path: Path) -> None:
    # big5 can encode 酒干倘賣無 — if chardet/CJK fallback works this passes.
    p = _write(tmp_path, "a.csv", DISCORD_CSV, "big5")
    enc = detect_encoding(p)
    # Accept any encoding that can actually decode the file.
    assert p.read_bytes().decode(enc, errors="strict")


def test_detect_encoding_rejects_garbage(tmp_path: Path) -> None:
    p = tmp_path / "bad.csv"
    # Bytes that aren't valid in utf-8 / utf-8-sig / big5 / gbk.
    # Build a sequence of isolated 0x80-range bytes that break every fallback.
    p.write_bytes(b"\x81\x00\x82\x00\x83\x00" * 100)
    with pytest.raises(ValueError):
        detect_encoding(p)


def test_detect_format_csv(tmp_path: Path) -> None:
    p = _write(tmp_path, "a.csv", "a,b\n1,2\n")
    assert detect_format(p) == "csv"


def test_detect_format_tsv(tmp_path: Path) -> None:
    p = _write(tmp_path, "a.tsv", "a\tb\n1\t2\n")
    assert detect_format(p) == "tsv"


def test_detect_format_jsonl(tmp_path: Path) -> None:
    p = _write(tmp_path, "a.jsonl", '{"a":1}\n{"a":2}\n')
    assert detect_format(p) == "jsonl"


# ---------------------------------------------------------------------------
# Fuzzy matching and presets
# ---------------------------------------------------------------------------


def test_preset_discord_exporter_exact_match() -> None:
    preset = detect_preset(
        ["AuthorID", "Author", "Date", "Content", "Attachments", "Reactions"]
    )
    assert preset is not None
    assert preset.name == "discord_chat_exporter"
    assert preset.mapping.content == "Content"


def test_preset_no_match_returns_none() -> None:
    assert detect_preset(["foo", "bar"]) is None


def test_fuzzy_match_basic() -> None:
    mapping, conf = fuzzy_match(["user", "time", "message"])
    assert mapping.content == "message"
    assert mapping.timestamp == "time"
    assert mapping.author_name == "user"
    assert overall_fuzzy_confidence(conf) > 0.8


def test_fuzzy_match_cjk_headers() -> None:
    mapping, _conf = fuzzy_match(["發送者", "時間", "內容"])
    assert mapping.content == "內容"
    assert mapping.timestamp == "時間"
    assert mapping.author_name == "發送者"


def test_fuzzy_match_misses_gracefully() -> None:
    mapping, conf = fuzzy_match(["x", "y", "z"])
    assert mapping.content == ""
    assert overall_fuzzy_confidence(conf) == 0.0


def test_validate_mapping_catches_hallucinated_column() -> None:
    headers = ["user", "time", "message"]
    bad = ColumnMapping(content="not_a_column", author="user", timestamp="time")
    errors = validate_mapping(bad, headers)
    assert any("not_a_column" in e for e in errors)


def test_validate_mapping_requires_content() -> None:
    headers = ["user", "time", "message"]
    bad = ColumnMapping(content="")
    errors = validate_mapping(bad, headers)
    assert errors  # at least one error reported


# ---------------------------------------------------------------------------
# End-to-end parse
# ---------------------------------------------------------------------------


def test_parse_discord_csv(tmp_path: Path) -> None:
    p = _write(tmp_path, "discord.csv", DISCORD_CSV)
    preset = detect_preset(
        ["AuthorID", "Author", "Date", "Content", "Attachments", "Reactions"]
    )
    assert preset is not None
    messages = parse_file(p, preset.mapping, ParseOptions())
    # The Pinned-a-message row is dropped by default skip rules.
    assert len(messages) == 2
    first = messages[0]
    assert first.content == "hello world"
    assert first.author == "111"
    assert first.author_name == "alice"
    assert first.timestamp == datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    # CJK round-trips.
    assert messages[1].content == "酒干倘賣無"


def test_parse_skip_options_disabled(tmp_path: Path) -> None:
    p = _write(tmp_path, "discord.csv", DISCORD_CSV)
    preset = detect_preset(
        ["AuthorID", "Author", "Date", "Content", "Attachments", "Reactions"]
    )
    assert preset is not None
    opts = ParseOptions(skip_system=False, skip_empty=False, skip_deleted=False)
    messages = parse_file(p, preset.mapping, opts)
    assert len(messages) == 3
    assert any(m.content == "Pinned a message." for m in messages)


def test_parse_timestamp_fallback_on_garbage(tmp_path: Path) -> None:
    csv_text = (
        "user,time,message\n"
        "alice,not-a-date,hi\n"
    )
    p = _write(tmp_path, "a.csv", csv_text)
    mapping, _ = fuzzy_match(["user", "time", "message"])
    messages = parse_file(p, mapping, ParseOptions())
    assert len(messages) == 1
    # Fallback is now-UTC so we don't assert exact value, only that tz is set.
    assert messages[0].timestamp.tzinfo is not None


def test_parse_jsonl(tmp_path: Path) -> None:
    lines = [
        {"user": "alice", "time": "2024-01-01T00:00:00Z", "message": "hi"},
        {"user": "bob", "time": "2024-01-01T00:01:00Z", "message": "yo"},
    ]
    p = _write(tmp_path, "a.jsonl", "\n".join(json.dumps(x) for x in lines))
    mapping, _ = fuzzy_match(["user", "time", "message"])
    messages = parse_file(p, mapping, ParseOptions())
    assert len(messages) == 2
    assert messages[0].content == "hi"


def test_parse_tsv(tmp_path: Path) -> None:
    p = _write(tmp_path, "a.tsv", "user\ttime\tmessage\nalice\t2024-01-01T00:00:00Z\thi\n")
    mapping, _ = fuzzy_match(["user", "time", "message"])
    messages = parse_file(p, mapping, ParseOptions())
    assert len(messages) == 1


def test_max_rows(tmp_path: Path) -> None:
    rows = "\n".join(f'"x","x","2024-01-01T00:00:00Z","m{i}","",""' for i in range(10))
    csv_text = (
        "AuthorID,Author,Date,Content,Attachments,Reactions\n" + rows + "\n"
    )
    p = _write(tmp_path, "a.csv", csv_text)
    preset = detect_preset(
        ["AuthorID", "Author", "Date", "Content", "Attachments", "Reactions"]
    )
    assert preset is not None
    messages = parse_file(p, preset.mapping, ParseOptions(max_rows=3))
    assert len(messages) == 3


# ---------------------------------------------------------------------------
# Preview + deterministic mapper
# ---------------------------------------------------------------------------


def test_preview_file_discord(tmp_path: Path) -> None:
    p = _write(tmp_path, "discord.csv", DISCORD_CSV)
    pr = preview_file(p)
    assert pr.preset == "discord_chat_exporter"
    assert pr.needs_review is False
    assert pr.overall_confidence == 1.0


def test_preview_file_unknown(tmp_path: Path) -> None:
    p = _write(tmp_path, "weird.csv", "x,y,z\n1,2,3\n")
    pr = preview_file(p)
    assert pr.preset is None
    assert pr.needs_review is True  # fuzzy fails on unknown headers


def test_infer_mapping_deterministic_preset(tmp_path: Path) -> None:
    p = _write(tmp_path, "discord.csv", DISCORD_CSV)
    result = infer_mapping_deterministic(p)
    assert result.source == "preset"
    assert result.preset == "discord_chat_exporter"


def test_infer_mapping_deterministic_fuzzy_success(tmp_path: Path) -> None:
    csv_text = (
        "user,time,message\n"
        "alice,2024-01-01T00:00:00Z,hi\n"
    )
    p = _write(tmp_path, "a.csv", csv_text)
    result = infer_mapping_deterministic(p)
    assert result.source == "fuzzy"
    assert result.mapping.content == "message"


def test_infer_mapping_deterministic_fuzzy_fallback(tmp_path: Path) -> None:
    p = _write(tmp_path, "weird.csv", "x,y,z\n1,2,3\n")
    result = infer_mapping_deterministic(p)
    assert result.source == "fuzzy_fallback"
    assert result.needs_review is True


def test_read_headers_and_samples(tmp_path: Path) -> None:
    p = _write(tmp_path, "discord.csv", DISCORD_CSV)
    headers, samples, fmt = read_headers_and_samples(p)
    assert fmt == "csv"
    assert headers == ["AuthorID", "Author", "Date", "Content", "Attachments", "Reactions"]
    assert len(samples) == 3
    assert samples[0]["Content"] == "hello world"
