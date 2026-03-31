"""Structured JSON logging for Beever Atlas pipeline observability."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any


class StructuredFormatter(logging.Formatter):
    """JSON log formatter with category support.

    Output format: {"ts": "...", "level": "info", "cat": "sync", "msg": "...", ...data}

    Categories:
    - sync: SyncRunner, BatchProcessor lifecycle
    - llm: LLM agent calls (extraction, classification, validation)
    - embed: Jina embedding calls
    - store: Weaviate, Neo4j, MongoDB operations
    - api: FastAPI request/response
    - pipeline: Pipeline stage transitions with timing
    - app: Default for uncategorized logs

    Filter examples:
        grep '"cat":"llm"' to see only LLM calls
        grep '"cat":"store"' to see only DB operations
        grep '"level":"error"' to see only errors
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "cat": getattr(record, "cat", "app"),
            "msg": record.getMessage(),
        }
        # Merge extra data fields from category-aware helpers
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            entry.update(data)
        return json.dumps(entry, default=str)


def sync_log(logger: logging.Logger, msg: str, level: str = "info", **data: Any) -> None:
    """Log a sync/pipeline lifecycle event."""
    getattr(logger, level)(msg, extra={"cat": "sync", "data": data})


def llm_log(logger: logging.Logger, msg: str, level: str = "info", **data: Any) -> None:
    """Log an LLM agent call event."""
    getattr(logger, level)(msg, extra={"cat": "llm", "data": data})


def store_log(logger: logging.Logger, msg: str, level: str = "info", **data: Any) -> None:
    """Log a store (Weaviate/Neo4j/MongoDB) operation."""
    getattr(logger, level)(msg, extra={"cat": "store", "data": data})


def embed_log(logger: logging.Logger, msg: str, level: str = "info", **data: Any) -> None:
    """Log a Jina embedding operation."""
    getattr(logger, level)(msg, extra={"cat": "embed", "data": data})
