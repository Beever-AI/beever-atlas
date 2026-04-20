"""File import endpoints — upload → preview → commit → ingest.

Two-step flow:
  POST /api/imports/preview   multipart upload → file_id + inferred mapping
  POST /api/imports/commit    { file_id, channel_name, mapping } → sync_job_id

The commit endpoint creates a ``platform="file"`` PlatformConnection (if
one doesn't already exist for this channel), creates the channel sync
job, runs the BatchProcessor in the background, and triggers
``on_ingestion_complete`` so wiki generation works identically to live
platform channels.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from beever_atlas.agents.ingestion.csv_mapper import (
    infer_mapping,
    infer_mapping_deterministic,
)
from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.infra.config import get_settings
from beever_atlas.services.file_importer import (
    ColumnMapping,
    ParseOptions,
    detect_encoding,
    detect_format,
    parse_file,
    validate_mapping,
)
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMPORT_UPLOAD_SIZE = 200 * 1024 * 1024  # 200 MB hard cap for file imports
ALLOWED_IMPORT_EXTENSIONS = {".csv", ".tsv", ".jsonl", ".ndjson", ".json", ".txt"}
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _extract_user_id(request: Request) -> str:
    """Extract user_id from request state (auth middleware) or fall back."""
    return getattr(request.state, "user_id", None) or "api_user"


def _sanitize_filename(raw: str | None) -> tuple[str, str]:
    """Return (safe_basename, extension_lower) or raise HTTPException.

    - Strips any directory components.
    - Replaces non-[A-Za-z0-9._-] characters with '_'.
    - Rejects null bytes and empty stems.
    - Enforces an extension allowlist.
    """
    if not raw:
        raise HTTPException(status_code=400, detail="Missing filename")
    if "\x00" in raw:
        raise HTTPException(status_code=400, detail="Invalid filename")
    name = Path(raw).name  # strip directories
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_IMPORT_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file extension {ext!r}. Allowed: "
            f"{sorted(ALLOWED_IMPORT_EXTENSIONS)}",
        )
    safe = _SAFE_FILENAME_RE.sub("_", name) or f"upload{ext}"
    return safe, ext


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ColumnMappingDTO(BaseModel):
    content: str
    author: str | None = None
    author_name: str | None = None
    timestamp: str | None = None
    timestamp_time: str | None = None
    message_id: str | None = None
    thread_id: str | None = None
    attachments: str | None = None
    reactions: str | None = None


class PreviewResponse(BaseModel):
    file_id: str
    filename: str
    encoding: str
    format: str
    row_count_estimate: int
    headers: list[str]
    sample_messages: list[dict[str, Any]]
    mapping: ColumnMappingDTO
    mapping_source: str
    preset: str | None
    overall_confidence: float
    per_field_confidence: dict[str, float]
    needs_review: bool
    detected_source: str | None = None
    notes: str = ""
    expires_at: str


class CommitRequest(BaseModel):
    file_id: str
    channel_name: str
    channel_id: str | None = None  # default: generated uuid
    mapping: ColumnMappingDTO
    skip_empty: bool = True
    skip_system: bool = True
    skip_deleted: bool = True
    dayfirst: bool = False
    max_rows: int = Field(default=0, description="0 = config default")


class CommitResponse(BaseModel):
    job_id: str
    channel_id: str
    channel_name: str
    connection_id: str
    total_messages: int
    status: str = "queued"


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def _staging_root() -> Path:
    root = Path(get_settings().file_import_staging_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _stage_paths(file_id: str) -> tuple[Path, Path, Path]:
    base = _staging_root() / file_id
    return base, base / "original", base / "meta.json"


def _meta(file_id: str) -> dict[str, Any] | None:
    _base, _original, meta = _stage_paths(file_id)
    if not meta.exists():
        return None
    try:
        return json.loads(meta.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _is_expired(meta: dict[str, Any]) -> bool:
    ttl = get_settings().file_import_staging_ttl_seconds
    return time.time() - float(meta.get("created_at", 0)) > ttl


def _cleanup_expired_stages() -> None:
    """Best-effort cleanup of expired stage dirs. Safe to call anytime."""
    root = _staging_root()
    ttl = get_settings().file_import_staging_ttl_seconds
    now = time.time()
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        meta_path = entry / "meta.json"
        try:
            if not meta_path.exists():
                if now - entry.stat().st_mtime > ttl:
                    _rm_tree(entry)
                continue
            meta = json.loads(meta_path.read_text())
            if now - float(meta.get("created_at", 0)) > ttl:
                _rm_tree(entry)
        except Exception as exc:  # noqa: BLE001
            logger.debug("stage cleanup: skipping %s: %s", entry, exc)


def _rm_tree(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            _rm_tree(child)
        else:
            child.unlink(missing_ok=True)
    path.rmdir()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mapping_from_dto(dto: ColumnMappingDTO) -> ColumnMapping:
    return ColumnMapping(
        content=dto.content,
        author=dto.author,
        author_name=dto.author_name or dto.author,
        timestamp=dto.timestamp,
        timestamp_time=dto.timestamp_time,
        message_id=dto.message_id,
        thread_id=dto.thread_id,
        attachments=dto.attachments,
        reactions=dto.reactions,
    )


def _dto_from_mapping(m: ColumnMapping) -> ColumnMappingDTO:
    return ColumnMappingDTO(**dataclasses.asdict(m))


def _row_count_estimate(path: Path, encoding: str, fmt: str) -> int:
    """Cheap line-count based estimate (subtract 1 for CSV/TSV header)."""
    count = 0
    with path.open(encoding=encoding, errors="replace") as f:
        for _ in f:
            count += 1
    if fmt in ("csv", "tsv") and count > 0:
        count -= 1
    return count


async def _ensure_file_connection(
    display_name: str,
    owner_principal_id: str | None = None,
) -> str:
    """Return the single shared ``platform="file"`` connection id, creating it if needed.

    When a new file connection is created, ``owner_principal_id`` is stamped
    with the caller's principal so channel-access guards admit them on the
    synthetic file channels. Older file connections are untouched (the
    startup backfill rewrites missing owners to ``"legacy:shared"``).
    """
    stores = get_stores()
    existing = await stores.platform.list_connections()
    for conn in existing:
        if conn.platform == "file":
            return conn.id
    conn = await stores.platform.create_connection(
        platform="file",
        display_name=display_name or "File Imports",
        credentials={},
        status="connected",
        source="ui",
        owner_principal_id=owner_principal_id,
    )
    logger.info("imports: created platform=file connection id=%s", conn.id)
    return conn.id


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api/imports/preview", response_model=PreviewResponse)
async def preview_import(
    request: Request,
    file: UploadFile = File(...),
    use_llm: bool = Form(default=True),
    principal: Principal = Depends(require_user),
) -> PreviewResponse:
    """Stage an uploaded file and return the inferred column mapping."""
    _ = _extract_user_id(request)  # enforce auth context, scope cleanup per-user later
    # RES-177 M6: stamp the uploader's principal id on the stage so the
    # commit step can reject cross-user claims on the stage dir.
    uploader_principal_id = getattr(principal, "id", None) or str(principal)

    safe_name, _ext = _sanitize_filename(file.filename)

    _cleanup_expired_stages()

    file_id = str(uuid.uuid4())
    base, original, meta_path = _stage_paths(file_id)
    base.mkdir(parents=True, exist_ok=True)
    # Preserve original (sanitized) filename extension for format detection.
    original_with_ext = base / safe_name
    # Stream to disk in chunks, enforcing a hard size cap to bound RAM/disk.
    total = 0
    try:
        try:
            with original_with_ext.open("wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)  # 1 MB chunks
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_IMPORT_UPLOAD_SIZE:
                        raise HTTPException(
                            status_code=413,
                            detail=f"File too large. Max {MAX_IMPORT_UPLOAD_SIZE} bytes.",
                        )
                    out.write(chunk)
        finally:
            await file.close()
    except HTTPException:
        _rm_tree(base)
        raise
    except Exception as exc:
        _rm_tree(base)
        raise HTTPException(status_code=400, detail=f"Upload failed: {exc}") from exc

    try:
        encoding = detect_encoding(original_with_ext)
    except ValueError as exc:
        _rm_tree(base)
        raise HTTPException(status_code=400, detail=str(exc))

    fmt = detect_format(original_with_ext)
    try:
        row_estimate = _row_count_estimate(original_with_ext, encoding, fmt)
    except Exception:
        row_estimate = 0

    # Inference — LLM only if caller opted in AND feature flag allows it.
    flag_on = get_settings().file_import_llm_mapping_enabled
    run_llm = use_llm and flag_on
    if run_llm:
        mapping_result = await infer_mapping(original_with_ext, use_llm=True)
    else:
        mapping_result = infer_mapping_deterministic(original_with_ext)

    # Parse a handful of sample messages for the UI preview.
    sample_opts = ParseOptions(
        default_platform="file",
        default_channel_id=f"preview-{file_id[:8]}",
        default_channel_name=Path(safe_name).stem,
        max_rows=5,
    )
    try:
        samples = parse_file(
            original_with_ext, mapping_result.mapping, sample_opts, encoding=encoding
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("imports: sample parse failed (mapping may need review): %s", exc)
        samples = []

    sample_dicts = [
        {
            "content": m.content[:200],
            "author": m.author,
            "author_name": m.author_name,
            "timestamp": m.timestamp.isoformat(),
        }
        for m in samples
    ]

    # Persist metadata so commit can find the file later.
    meta = {
        "file_id": file_id,
        "filename": safe_name,
        "original_path": str(original_with_ext),
        "encoding": encoding,
        "format": fmt,
        "created_at": time.time(),
        "uploader_principal_id": uploader_principal_id,
    }
    meta_path.write_text(json.dumps(meta))

    ttl = get_settings().file_import_staging_ttl_seconds
    expires_at = datetime.fromtimestamp(meta["created_at"] + ttl, tz=timezone.utc).isoformat()

    return PreviewResponse(
        file_id=file_id,
        filename=safe_name,
        encoding=encoding,
        format=fmt,
        row_count_estimate=row_estimate,
        headers=list(samples[0].raw_metadata.get("raw", {}).keys()) if samples else [],
        sample_messages=sample_dicts,
        mapping=_dto_from_mapping(mapping_result.mapping),
        mapping_source=mapping_result.source,
        preset=mapping_result.preset,
        overall_confidence=mapping_result.overall_confidence,
        per_field_confidence=mapping_result.confidence,
        needs_review=mapping_result.needs_review,
        detected_source=mapping_result.detected_source,
        notes=mapping_result.notes,
        expires_at=expires_at,
    )


@router.post("/api/imports/commit", response_model=CommitResponse)
async def commit_import(
    request: Request,
    body: CommitRequest,
    principal: Principal = Depends(require_user),
) -> CommitResponse:
    _ = _extract_user_id(request)
    """Parse the staged file using ``body.mapping`` and kick off ingestion."""
    meta = _meta(body.file_id)
    if meta is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown file_id {body.file_id!r}. Re-upload via /api/imports/preview.",
        )
    if _is_expired(meta):
        _rm_tree(_stage_paths(body.file_id)[0])
        raise HTTPException(
            status_code=410,
            detail="Staged file has expired. Re-upload via /api/imports/preview.",
        )

    # RES-177 M6: reject cross-user commits on a stage uploaded by someone
    # else. Stages written before this change have no uploader recorded;
    # honour them only when single-tenant fallback would otherwise apply.
    caller_pid = getattr(principal, "id", None) or str(principal)
    uploader_pid = meta.get("uploader_principal_id")
    if uploader_pid and uploader_pid != caller_pid:
        logger.info(
            "imports.commit deny: file_id=%s uploader=%s caller=%s reason=principal_mismatch",
            body.file_id,
            uploader_pid,
            caller_pid,
        )
        raise HTTPException(
            status_code=403,
            detail="Staged upload belongs to a different user.",
        )

    original_path = Path(meta["original_path"])
    encoding = meta["encoding"]
    meta["format"]
    if not original_path.exists():
        raise HTTPException(status_code=410, detail="Staged file missing on disk. Re-upload.")

    # Validate mapping against actual headers now, before we start ingestion.
    from beever_atlas.services.file_importer import read_headers_and_samples

    headers, _samples, _fmt = read_headers_and_samples(original_path, encoding=encoding)
    mapping = _mapping_from_dto(body.mapping)
    errors = validate_mapping(mapping, headers)
    if errors:
        raise HTTPException(status_code=422, detail={"mapping_errors": errors})

    channel_id = body.channel_id or f"file-{uuid.uuid4()}"
    channel_name = body.channel_name or Path(meta["filename"]).stem

    settings = get_settings()
    max_rows = body.max_rows if body.max_rows > 0 else settings.file_import_max_rows
    opts = ParseOptions(
        skip_empty=body.skip_empty,
        skip_system=body.skip_system,
        skip_deleted=body.skip_deleted,
        dayfirst=body.dayfirst,
        default_platform="file",
        default_channel_id=channel_id,
        default_channel_name=channel_name,
        max_rows=max_rows,
    )
    try:
        messages = parse_file(original_path, mapping, opts, encoding=encoding)
    except Exception as exc:  # noqa: BLE001
        logger.exception("imports: parse failed for file_id=%s", body.file_id)
        raise HTTPException(status_code=400, detail=f"Parse failed: {exc}") from exc

    if not messages:
        raise HTTPException(
            status_code=422,
            detail="No messages parsed. Check the mapping or skip options.",
        )

    stores = get_stores()
    connection_id = await _ensure_file_connection(
        "File Imports",
        owner_principal_id=caller_pid,
    )

    # RES-177 H1 + M6 (review fix): check access BEFORE mutating the file
    # connection's selected_channels. If the caller doesn't own this channel,
    # the guard raises 403 and no state is touched; otherwise we append the
    # channel to the sync pick-list below. The previous ordering was
    # self-granting — appending first meant the guard would always admit the
    # caller that had just written themselves into selected_channels.
    await assert_channel_access(principal, channel_id)

    # Tie this channel to the file connection so the sidebar groups it under
    # "File Imports" instead of treating it as orphaned.
    file_conn = await stores.platform.get_connection(connection_id)
    if file_conn is not None and channel_id not in file_conn.selected_channels:
        await stores.platform.update_connection(
            connection_id,
            selected_channels=list(file_conn.selected_channels) + [channel_id],
        )

    # Persist raw messages so the Messages tab can render them. Platform
    # channels re-fetch from the bridge, but file channels have no upstream —
    # we keep a copy in the imported_messages collection keyed by channel_id.
    docs = [
        {
            "channel_id": channel_id,
            "message_id": m.message_id,
            "content": m.content,
            "author": m.author,
            "author_name": m.author_name,
            "author_image": m.author_image,
            "platform": "file",
            "channel_name": channel_name,
            "timestamp": m.timestamp,
            "timestamp_iso": m.timestamp.isoformat(),
            "thread_id": m.thread_id,
            "attachments": m.attachments,
            "reactions": m.reactions,
            "reply_count": m.reply_count,
        }
        for m in messages
    ]
    await stores.mongodb.db["imported_messages"].delete_many({"channel_id": channel_id})
    if docs:
        await stores.mongodb.db["imported_messages"].insert_many(docs)
    await stores.mongodb.db["imported_messages"].create_index(
        [("channel_id", 1), ("timestamp", -1)]
    )

    # Log the channel's display name up front so get_channel_display_name
    # resolves correctly even before ingestion finishes.
    await stores.mongodb.log_activity(
        event_type="file_import_started",
        channel_id=channel_id,
        details={
            "channel_name": channel_name,
            "connection_id": connection_id,
            "file_id": body.file_id,
            "total_messages": len(messages),
            "source": "file_import",
        },
    )

    # Register sync_state so the channel shows as "ready" (0 processed / N total)
    # in the sidebar. Do NOT run BatchProcessor here — matching the platform
    # flow, extraction runs when the user clicks "Sync Channel".
    await stores.mongodb.update_channel_sync_state(
        channel_id=channel_id,
        last_sync_ts="",  # no ingestion has happened yet
        set_total=len(messages),
    )
    logger.info(
        "imports: commit file_id=%s channel=%s messages=%d (awaiting manual sync)",
        body.file_id,
        channel_id,
        len(messages),
    )

    # Cleanup the staged file — we've copied the rows into imported_messages.
    try:
        _rm_tree(_stage_paths(body.file_id)[0])
    except Exception:
        pass

    return CommitResponse(
        job_id="",
        channel_id=channel_id,
        channel_name=channel_name,
        connection_id=connection_id,
        total_messages=len(messages),
        status="pending",
    )


async def _run_ingestion(
    *,
    messages: list,
    channel_id: str,
    channel_name: str,
    job_id: str,
    file_id: str,
    fmt: str,
) -> None:
    """Background worker — mirrors scripts/ingest_from_csv.py."""
    from beever_atlas.services.batch_processor import BatchProcessor
    from beever_atlas.services.pipeline_orchestrator import on_ingestion_complete
    from beever_atlas.services.policy_resolver import resolve_effective_policy

    stores = get_stores()
    try:
        effective_policy = await resolve_effective_policy(channel_id)
        ingestion_config = effective_policy.ingestion
        result = await BatchProcessor().process_messages(
            messages=messages,
            channel_id=channel_id,
            channel_name=channel_name,
            sync_job_id=job_id,
            ingestion_config=ingestion_config,
            use_batch_api=False,
        )
    except Exception as exc:
        logger.exception("imports: ingestion failed job_id=%s", job_id)
        await stores.mongodb.complete_sync_job(
            job_id=job_id,
            status="failed",
            errors=[str(exc)],
            failed_stage=f"imports_pipeline: {str(exc)[:200]}",
        )
        return

    timestamps = [m.timestamp for m in messages if m.timestamp and not m.thread_id]
    last_ts = max(timestamps).isoformat() if timestamps else None

    sync_status = "failed" if result.errors else "completed"
    sync_errors: list[str] | None = None
    if result.errors:
        sync_errors = [
            f"batch={err.get('batch_num')} error={err.get('error')}" for err in result.errors
        ]
    await stores.mongodb.complete_sync_job(
        job_id=job_id,
        status=sync_status,
        errors=sync_errors,
    )

    if last_ts:
        await stores.mongodb.update_channel_sync_state(
            channel_id=channel_id,
            last_sync_ts=last_ts,
            set_total=len(messages),
        )

    await stores.mongodb.log_activity(
        event_type="sync_failed" if result.errors else "sync_completed",
        channel_id=channel_id,
        details={
            "job_id": job_id,
            "channel_name": channel_name,
            "total_facts": result.total_facts,
            "total_entities": result.total_entities,
            "total_relationships": result.total_relationships,
            "total_messages": len(messages),
            "error_count": len(result.errors),
            "source": "file_import",
            "file_id": file_id,
            "file_format": fmt,
        },
    )

    # File imports are one-shot; consolidation (topic clustering / memories /
    # wiki generation) is left manual so users can review the raw data first.
    # They can still trigger it via the Memories or Wiki tab's Generate button.
    _ = on_ingestion_complete  # kept imported for future opt-in wiring

    # Best-effort stage cleanup.
    try:
        _rm_tree(_stage_paths(file_id)[0])
    except Exception:
        pass
