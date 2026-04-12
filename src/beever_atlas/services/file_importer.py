"""Generic file importer for chat-history CSV / TSV / JSONL exports.

Pipeline: detect encoding → detect format → (optional) preset/fuzzy column
mapping → parse rows into NormalizedMessage. The LLM mapping agent is an
orthogonal layer that sits on top of the fuzzy matcher here (see
``agents.ingestion.csv_mapper``).
"""

from __future__ import annotations

import csv
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from beever_atlas.adapters.base import NormalizedMessage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class ColumnMapping:
    """Which source column supplies each NormalizedMessage field.

    ``content`` is the only strictly required mapping. The rest have graceful
    fallbacks (empty string / generated uuid / now-UTC).
    """

    content: str
    author: str | None = None
    author_name: str | None = None
    timestamp: str | None = None
    message_id: str | None = None
    thread_id: str | None = None
    attachments: str | None = None
    reactions: str | None = None
    # Optional split-column timestamp support: if both timestamp and
    # timestamp_time are set, their values are concatenated with a space.
    timestamp_time: str | None = None


@dataclass
class ParseOptions:
    skip_empty: bool = True
    skip_system: bool = True
    skip_deleted: bool = True
    dayfirst: bool = False
    default_platform: str = "file"
    default_channel_id: str = ""
    default_channel_name: str = ""
    max_rows: int = 0  # 0 = unlimited


# Known system/noise message bodies that callers usually want to drop.
SYSTEM_STRINGS: set[str] = {
    "Pinned a message.",
    "This message was deleted.",
    "This message has been deleted.",
    "<Media omitted>",
    "<attached: image>",
}


# ---------------------------------------------------------------------------
# Encoding & format detection
# ---------------------------------------------------------------------------

_CJK_FALLBACKS = ("big5", "gbk", "gb18030", "shift_jis")


def detect_encoding(path: Path, sample_bytes: int = 65536) -> str:
    """Return a best-effort encoding for ``path``.

    Try strict utf-8 first (cheapest + most common), then utf-8-sig, then
    chardet over the first ``sample_bytes``, then explicit CJK fallbacks.
    Raise ValueError if nothing decodes cleanly.
    """
    raw = path.read_bytes()[:sample_bytes]

    def _try(enc: str) -> bool:
        # Trim up to 4 trailing bytes to tolerate truncation inside a
        # multi-byte character at the sample boundary.
        for trim in range(5):
            try:
                raw[: len(raw) - trim].decode(enc)
                return True
            except UnicodeDecodeError:
                continue
            except LookupError:
                return False
        return False

    for enc in ("utf-8", "utf-8-sig"):
        if _try(enc):
            return enc

    try:
        import chardet  # lazy — keeps import cheap for utf-8 path
        guess = chardet.detect(raw)
        enc = (guess.get("encoding") or "").lower()
        conf = guess.get("confidence") or 0.0
        if enc and conf >= 0.7 and _try(enc):
            return enc
    except ImportError:
        logger.debug("chardet not available; falling back to CJK probe")

    for enc in _CJK_FALLBACKS:
        if _try(enc):
            return enc

    raise ValueError(
        f"Could not decode {path.name}: tried utf-8, utf-8-sig, chardet, and "
        f"{_CJK_FALLBACKS}. Pass an explicit encoding override."
    )


def detect_format(path: Path) -> str:
    """Return ``csv`` | ``tsv`` | ``jsonl`` based on extension + first-line sniff."""
    suffix = path.suffix.lower().lstrip(".")
    if suffix in ("jsonl", "ndjson"):
        return "jsonl"
    if suffix == "tsv":
        return "tsv"
    if suffix == "csv":
        return "csv"

    # Unknown extension — sniff the first non-empty line.
    try:
        with path.open("rb") as f:
            head = f.read(4096)
        text = head.decode("utf-8", errors="replace")
    except OSError:
        return "csv"

    first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
    if first_line.startswith("{") and first_line.rstrip().endswith("}"):
        return "jsonl"
    if first_line.count("\t") > first_line.count(","):
        return "tsv"
    return "csv"


# ---------------------------------------------------------------------------
# Header loading (used by previews, presets, fuzzy matcher, LLM mapper)
# ---------------------------------------------------------------------------


def read_headers_and_samples(
    path: Path,
    encoding: str | None = None,
    sample_rows: int = 3,
) -> tuple[list[str], list[dict[str, str]], str]:
    """Return ``(headers, sample_rows, detected_format)`` for preview/mapping UIs.

    For JSONL the "headers" are the union of top-level keys from the first
    ``sample_rows`` objects; values are stringified.
    """
    enc = encoding or detect_encoding(path)
    fmt = detect_format(path)

    if fmt == "jsonl":
        samples: list[dict[str, str]] = []
        with path.open(encoding=enc) as f:
            for i, line in enumerate(f):
                if i >= sample_rows:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    samples.append({k: _stringify(v) for k, v in obj.items()})
        headers = list(dict.fromkeys(k for s in samples for k in s.keys()))
        return headers, samples, fmt

    delimiter = "\t" if fmt == "tsv" else ","
    with path.open(encoding=enc, newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        samples_csv: list[dict[str, str]] = []
        for i, row in enumerate(reader):
            if i >= sample_rows:
                break
            samples_csv.append({(k or "").strip(): (v or "") for k, v in row.items()})
    return headers, samples_csv, fmt


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Presets: exact header-signature → ColumnMapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    name: str
    headers: frozenset[str]
    mapping: ColumnMapping
    options_overrides: dict[str, Any] = field(default_factory=dict)


PRESETS: list[Preset] = [
    Preset(
        name="discord_chat_exporter",
        headers=frozenset({"AuthorID", "Author", "Date", "Content", "Attachments", "Reactions"}),
        mapping=ColumnMapping(
            content="Content",
            author="AuthorID",
            author_name="Author",
            timestamp="Date",
            attachments="Attachments",
            reactions="Reactions",
        ),
    ),
    Preset(
        name="slack_csv",
        headers=frozenset({"user", "user_name", "ts", "text"}),
        mapping=ColumnMapping(
            content="text",
            author="user",
            author_name="user_name",
            timestamp="ts",
        ),
    ),
    Preset(
        name="whatsapp_basic",
        headers=frozenset({"Date", "Time", "Sender", "Message"}),
        mapping=ColumnMapping(
            content="Message",
            author="Sender",
            author_name="Sender",
            timestamp="Date",
            timestamp_time="Time",
        ),
    ),
]


def detect_preset(headers: Iterable[str]) -> Preset | None:
    """Return the first preset whose header set exactly matches ``headers``."""
    hset = frozenset(h.strip() for h in headers if h)
    for preset in PRESETS:
        if preset.headers == hset:
            return preset
    return None


# ---------------------------------------------------------------------------
# Fuzzy matcher
# ---------------------------------------------------------------------------


_ALIAS_TABLE: dict[str, list[str]] = {
    "content": [
        "content", "message", "msg", "text", "body", "內容", "内容", "訊息", "消息",
    ],
    "author": [
        "authorid", "userid", "user_id", "sender_id", "from_id",
    ],
    "author_name": [
        "author", "user", "username", "name", "sender", "from",
        "發送者", "发送者", "用戶", "用户", "作者",
    ],
    "timestamp": [
        "timestamp", "date", "time", "datetime", "created_at", "created", "sent_at",
        "時間", "时间", "日期",
    ],
    "message_id": [
        "message_id", "msg_id", "id", "messageid",
    ],
    "thread_id": [
        "thread_id", "thread_ts", "parent_id", "reply_to",
    ],
    "attachments": [
        "attachments", "media", "files", "attachment",
    ],
    "reactions": [
        "reactions", "emoji", "likes",
    ],
}


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", s.lower())


def fuzzy_match(headers: Iterable[str]) -> tuple[ColumnMapping, dict[str, float]]:
    """Return a best-effort mapping and per-field confidence (0.0–1.0).

    Score 1.0 = exact case-insensitive alias hit.
    Score 0.6 = alias is a substring of a header (or vice versa).
    Score 0.0 = no match.
    """
    hlist = [h.strip() for h in headers if h and h.strip()]
    norm_headers = {h: _normalize(h) for h in hlist}
    picked: dict[str, str] = {}
    confidence: dict[str, float] = {}
    used: set[str] = set()

    for field_name, aliases in _ALIAS_TABLE.items():
        norm_aliases = [_normalize(a) for a in aliases]
        best_header: str | None = None
        best_score = 0.0
        for header, nh in norm_headers.items():
            if header in used:
                continue
            for na in norm_aliases:
                if nh == na:
                    if 1.0 > best_score:
                        best_score, best_header = 1.0, header
                elif nh and (nh in na or na in nh) and min(len(nh), len(na)) >= 3:
                    if 0.6 > best_score:
                        best_score, best_header = 0.6, header
        if best_header and best_score > 0:
            picked[field_name] = best_header
            confidence[field_name] = best_score
            used.add(best_header)

    # Second pass: if author_name matched but author didn't, leave it.
    if "author" not in picked and "author_name" in picked:
        # many exports only have a single "user" column — that's fine.
        pass

    mapping = ColumnMapping(
        content=picked.get("content", ""),
        author=picked.get("author") or picked.get("author_name"),
        author_name=picked.get("author_name") or picked.get("author"),
        timestamp=picked.get("timestamp"),
        message_id=picked.get("message_id"),
        thread_id=picked.get("thread_id"),
        attachments=picked.get("attachments"),
        reactions=picked.get("reactions"),
    )
    return mapping, confidence


def overall_fuzzy_confidence(confidence: dict[str, float]) -> float:
    """Combine per-field confidences into a single 0.0–1.0 score.

    Content + timestamp + author_name are the load-bearing fields; message_id /
    thread_id / attachments are bonuses. Returns 0 if content missing.
    """
    if confidence.get("content", 0.0) == 0.0:
        return 0.0
    base = confidence.get("content", 0.0) * 0.5
    base += confidence.get("timestamp", 0.0) * 0.3
    base += max(confidence.get("author_name", 0.0), confidence.get("author", 0.0)) * 0.2
    return round(base, 3)


def validate_mapping(mapping: ColumnMapping, headers: Iterable[str]) -> list[str]:
    """Return a list of validation errors (empty = valid).

    Catches LLM hallucinations by ensuring every non-None column value
    actually appears in ``headers``.
    """
    errors: list[str] = []
    hset = {h.strip() for h in headers if h}
    if not mapping.content:
        errors.append("ColumnMapping.content is required")
    elif mapping.content not in hset:
        errors.append(f"content column {mapping.content!r} not in headers")

    for fname in (
        "author", "author_name", "timestamp", "message_id", "thread_id",
        "attachments", "reactions", "timestamp_time",
    ):
        val = getattr(mapping, fname)
        if val and val not in hset:
            errors.append(f"{fname} column {val!r} not in headers")
    return errors


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_timestamp(raw: str, dayfirst: bool = False) -> datetime:
    """Parse a timestamp string with broad fallbacks. Returns now-UTC on failure."""
    if not raw:
        return datetime.now(timezone.utc)
    s = raw.strip()
    if not s:
        return datetime.now(timezone.utc)

    # Epoch seconds
    try:
        as_float = float(s)
        if 10_000_000 < as_float < 4_000_000_000:  # sensible epoch window
            return datetime.fromtimestamp(as_float, tz=timezone.utc)
    except ValueError:
        pass

    # ISO-8601 (covers DiscordChatExporter's extended +HH:MM format)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # dateutil fallback for humanized formats
    try:
        from dateutil import parser as _dtparser  # lazy
        dt = _dtparser.parse(s, dayfirst=dayfirst)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, ImportError, OverflowError):
        logger.debug("timestamp parse failed for %r; using now()", s)
        return datetime.now(timezone.utc)


def _parse_attachments(raw: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    return [{"url": u.strip(), "type": "file"} for u in raw.split(",") if u.strip()]


def _parse_reactions(raw: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    out: list[dict[str, Any]] = []
    for part in raw.split(","):
        part = part.strip()
        m = re.match(r"(.+)\s*-\s*(\d+)$", part)
        if m:
            out.append({"name": m.group(1).strip(), "count": int(m.group(2))})
    return out


def _should_skip(content: str, options: ParseOptions) -> bool:
    if options.skip_empty and not content:
        return True
    if options.skip_system and content in SYSTEM_STRINGS:
        return True
    if options.skip_deleted and content.lower() in {
        "this message was deleted.", "this message has been deleted.",
        "<message deleted>",
    }:
        return True
    return False


def _iter_rows(path: Path, fmt: str, encoding: str) -> Iterable[dict[str, str]]:
    if fmt == "jsonl":
        with path.open(encoding=encoding) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSONL line")
                    continue
                if isinstance(obj, dict):
                    yield {k: _stringify(v) for k, v in obj.items()}
        return

    delimiter = "\t" if fmt == "tsv" else ","
    with path.open(encoding=encoding, newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            yield {(k or "").strip(): (v or "") for k, v in row.items()}


def parse_file(
    path: Path,
    mapping: ColumnMapping,
    options: ParseOptions | None = None,
    encoding: str | None = None,
) -> list[NormalizedMessage]:
    """Parse a file into NormalizedMessage objects using the given mapping."""
    opts = options or ParseOptions()
    enc = encoding or detect_encoding(path)
    fmt = detect_format(path)
    channel_id = opts.default_channel_id or path.stem
    channel_name = opts.default_channel_name or channel_id

    messages: list[NormalizedMessage] = []
    for idx, row in enumerate(_iter_rows(path, fmt, enc)):
        if opts.max_rows and len(messages) >= opts.max_rows:
            break

        content = (row.get(mapping.content, "") or "").strip()
        if _should_skip(content, opts):
            continue

        author_id = ""
        if mapping.author:
            author_id = (row.get(mapping.author, "") or "").strip()
        author_name = ""
        if mapping.author_name:
            author_name = (row.get(mapping.author_name, "") or "").strip()
        if not author_id:
            author_id = author_name or "unknown"
        if not author_name:
            author_name = author_id

        ts_raw = ""
        if mapping.timestamp:
            ts_raw = row.get(mapping.timestamp, "") or ""
        if mapping.timestamp_time:
            tt = row.get(mapping.timestamp_time, "") or ""
            if tt:
                ts_raw = f"{ts_raw} {tt}".strip()
        timestamp = _parse_timestamp(ts_raw, dayfirst=opts.dayfirst)

        msg_id = ""
        if mapping.message_id:
            msg_id = (row.get(mapping.message_id, "") or "").strip()
        if not msg_id:
            msg_id = str(uuid.uuid4())

        thread_id: str | None = None
        if mapping.thread_id:
            t = (row.get(mapping.thread_id, "") or "").strip()
            thread_id = t or None

        attachments = _parse_attachments(row.get(mapping.attachments, "") if mapping.attachments else "")
        reactions = _parse_reactions(row.get(mapping.reactions, "") if mapping.reactions else "")

        messages.append(NormalizedMessage(
            content=content,
            author=author_id,
            platform=opts.default_platform,
            channel_id=channel_id,
            channel_name=channel_name,
            message_id=msg_id,
            timestamp=timestamp,
            thread_id=thread_id,
            attachments=attachments,
            reactions=reactions,
            reply_count=0,
            raw_metadata={
                "source": "file_import",
                "row_index": idx,
                "raw": row,
            },
            author_name=author_name,
            author_image="",
        ))

    return messages


# ---------------------------------------------------------------------------
# Preview: combines detection + fuzzy in one call for the API / dry-run
# ---------------------------------------------------------------------------


@dataclass
class PreviewResult:
    encoding: str
    format: str
    headers: list[str]
    samples: list[dict[str, str]]
    preset: str | None
    mapping: ColumnMapping
    confidence: dict[str, float]
    overall_confidence: float
    needs_review: bool


def preview_file(path: Path) -> PreviewResult:
    """Run the deterministic half of the pipeline (no LLM)."""
    enc = detect_encoding(path)
    headers, samples, fmt = read_headers_and_samples(path, encoding=enc)
    preset = detect_preset(headers)
    if preset is not None:
        return PreviewResult(
            encoding=enc,
            format=fmt,
            headers=headers,
            samples=samples,
            preset=preset.name,
            mapping=preset.mapping,
            confidence={k: 1.0 for k in ("content", "author_name", "timestamp")},
            overall_confidence=1.0,
            needs_review=False,
        )

    mapping, conf = fuzzy_match(headers)
    overall = overall_fuzzy_confidence(conf)
    return PreviewResult(
        encoding=enc,
        format=fmt,
        headers=headers,
        samples=samples,
        preset=None,
        mapping=mapping,
        confidence=conf,
        overall_confidence=overall,
        needs_review=overall < 0.9,
    )
