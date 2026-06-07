"""Tests for the ``channel_messages`` Message Store accessors on MongoDBStore.

Covers the contract introduced by PR-A of the OSS pipeline + wiki redesign:

  * idempotent upsert by ``(source_id, channel_id, message_id)`` with
    ``$setOnInsert`` for extraction state (so a re-sync does not reset
    rows the worker has already moved past ``pending``)
  * read accessor for the dual-read fallback in ``get_channel_messages``
  * status-aggregation accessor for the future extraction-status endpoint
  * ``find_channel_message_by_message_id`` for the phantom raw_messages fix
  * state-machine validation in ``update_channel_message_status``

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/message-store/``.

No live Mongo — uses lightweight fakes that exercise the call shape +
in-memory ``$setOnInsert``/``$set`` semantics so the test stays fast.

Convention: no `@pytest.mark.asyncio` decorators; pyproject sets
`asyncio_mode = "auto"`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from beever_atlas.models.persistence import ChannelMessage
from beever_atlas.stores.mongodb_store import MongoDBStore


# ─────────────────────────────────────────────────────────────────────────────
# Lightweight fake collection — mimics motor's surface for the methods we use
# ─────────────────────────────────────────────────────────────────────────────


class _FakeBulkResult:
    def __init__(self, inserted: int, modified: int, matched: int, upserted: int) -> None:
        self.inserted_count = inserted
        self.modified_count = modified
        self.matched_count = matched
        self.upserted_ids = {i: object() for i in range(upserted)}


class _FakeUpdateResult:
    def __init__(self, modified: int) -> None:
        self.modified_count = modified


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)
        self._sort_key: str | None = None
        self._sort_dir: int = 1
        self._limit: int | None = None

    def sort(self, key: str, direction: int) -> "_FakeCursor":
        self._sort_key = key
        self._sort_dir = direction
        return self

    def limit(self, n: int) -> "_FakeCursor":
        self._limit = n
        return self

    def __aiter__(self):
        if self._sort_key is not None:
            self._docs.sort(
                key=lambda d: d.get(self._sort_key) or "",
                reverse=(self._sort_dir == -1),
            )
        if self._limit is not None:
            self._docs = self._docs[: self._limit]
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


class _FakeAggregateCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


class _FakeChannelMessages:
    """In-memory stand-in for the ``channel_messages`` motor collection.

    Implements just enough of the surface to exercise ``upsert_channel_messages``,
    ``get_channel_messages``, ``count_channel_messages_by_status``,
    ``find_channel_message_by_message_id``, and ``update_channel_message_status``.
    """

    def __init__(self) -> None:
        # Key = (source_id, channel_id, message_id) → doc dict
        self._docs: dict[tuple[str, str, str], dict[str, Any]] = {}

    @staticmethod
    def _key(filter_: dict[str, Any]) -> tuple[str, str, str]:
        return (filter_["source_id"], filter_["channel_id"], filter_["message_id"])

    async def bulk_write(self, ops: list[Any], ordered: bool = True) -> _FakeBulkResult:
        inserted = 0
        modified = 0
        matched = 0
        upserted = 0
        for op in ops:
            filter_ = op._filter  # private attr, but UpdateOne exposes it
            update = op._doc
            set_part = update.get("$set", {})
            on_insert = update.get("$setOnInsert", {})
            inc_part = update.get("$inc", {})
            # Filters with extra constraints (e.g. extraction_status:{$in:...})
            # don't carry the full key tuple — fall back to scanning when the
            # primary key fields aren't all present.
            if (
                "source_id" in filter_
                and "channel_id" in filter_
                and "message_id" in filter_
                and not isinstance(filter_["source_id"], dict)
                and not isinstance(filter_["channel_id"], dict)
                and not isinstance(filter_["message_id"], dict)
            ):
                key = self._key(filter_)
                existing = self._docs.get(key)
                # Honor any extra constraints in the filter (e.g. status guard).
                if existing is not None and not self._matches(existing, filter_):
                    matched += 0
                    existing = None
                if existing is None and (on_insert or update.get("upsert")):
                    if on_insert:
                        self._docs[key] = {**on_insert, **set_part}
                        inserted += 1
                        upserted += 1
                    continue
                if existing is None:
                    continue
                matched += 1
                changed = False
                for k, v in set_part.items():
                    if existing.get(k) != v:
                        existing[k] = v
                        changed = True
                for k, v in inc_part.items():
                    existing[k] = (existing.get(k) or 0) + v
                    changed = True
                if changed:
                    modified += 1
                continue
            # Fall-through for filter shapes without a full key — scan all docs.
            for doc in self._docs.values():
                if self._matches(doc, filter_):
                    matched += 1
                    changed = False
                    for k, v in set_part.items():
                        if doc.get(k) != v:
                            doc[k] = v
                            changed = True
                    for k, v in inc_part.items():
                        doc[k] = (doc.get(k) or 0) + v
                        changed = True
                    if changed:
                        modified += 1
        return _FakeBulkResult(inserted, modified, matched, upserted)

    def find(self, query: dict[str, Any]) -> _FakeCursor:
        rows = []
        for doc in self._docs.values():
            if self._matches(doc, query):
                rows.append({k: v for k, v in doc.items()})
        return _FakeCursor(rows)

    async def find_one(
        self, query: dict[str, Any], projection: dict[str, int] | None = None
    ) -> dict[str, Any] | None:
        for doc in self._docs.values():
            if self._matches(doc, query):
                return {k: v for k, v in doc.items()}
        return None

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> None:
        for doc in self._docs.values():
            if self._matches(doc, query):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                return

    async def update_many(self, query: dict[str, Any], update: dict[str, Any]) -> _FakeUpdateResult:
        modified = 0
        for doc in self._docs.values():
            if self._matches(doc, query):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                modified += 1
        return _FakeUpdateResult(modified=modified)

    async def find_one_and_update(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        return_document: Any = None,
        sort: list[tuple[str, int]] | None = None,
    ) -> dict[str, Any] | None:
        # Sort the candidates first if a sort key is supplied — the
        # production code uses ``sort=[("next_attempt_at", 1)]`` to drain
        # the oldest-pending row first.
        candidates = [(key, doc) for key, doc in self._docs.items() if self._matches(doc, query)]
        if sort is not None:
            for sort_key, sort_dir in reversed(sort):
                candidates.sort(
                    key=lambda kd: kd[1].get(sort_key) or "",
                    reverse=(sort_dir == -1),
                )
        if not candidates:
            return None
        _, doc = candidates[0]
        for k, v in update.get("$set", {}).items():
            doc[k] = v
        for k, v in update.get("$inc", {}).items():
            doc[k] = (doc.get(k) or 0) + v
        return {**doc}

    def aggregate(self, pipeline: list[dict[str, Any]]) -> _FakeAggregateCursor:
        rows: list[dict[str, Any]] = []
        match: dict[str, Any] = {}
        group_field: str | None = None
        for stage in pipeline:
            if "$match" in stage:
                match = stage["$match"]
            elif "$group" in stage:
                # group expression like {"_id": "$extraction_status", "n": {"$sum": 1}}
                expr = stage["$group"]["_id"]
                if isinstance(expr, str) and expr.startswith("$"):
                    group_field = expr[1:]
        if group_field is None:
            return _FakeAggregateCursor([])
        counts: dict[Any, int] = {}
        for doc in self._docs.values():
            if not self._matches(doc, match):
                continue
            value = doc.get(group_field)
            counts[value] = counts.get(value, 0) + 1
        for value, n in counts.items():
            rows.append({"_id": value, "n": n})
        return _FakeAggregateCursor(rows)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if k == "$or" and isinstance(v, list):
                if not any(_FakeChannelMessages._matches(doc, branch) for branch in v):
                    return False
                continue
            if isinstance(v, dict):
                if "$lt" in v and not (doc.get(k) is not None and doc.get(k) < v["$lt"]):
                    return False
                if "$lte" in v and not (doc.get(k) is not None and doc.get(k) <= v["$lte"]):
                    return False
                if "$gt" in v and not (doc.get(k) is not None and doc.get(k) > v["$gt"]):
                    return False
                if "$gte" in v and not (doc.get(k) is not None and doc.get(k) >= v["$gte"]):
                    return False
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
            else:
                if doc.get(k) != v:
                    return False
        return True


def _store_with_fake() -> tuple[MongoDBStore, _FakeChannelMessages]:
    fake = _FakeChannelMessages()
    store = MongoDBStore.__new__(MongoDBStore)
    store._channel_messages = fake  # type: ignore[attr-defined]
    return store, fake


def _msg(
    source_id: str = "slack",
    channel_id: str = "C123",
    message_id: str = "m1",
    content: str = "hello",
    extraction_status: str = "pending",
) -> ChannelMessage:
    return ChannelMessage(
        source_id=source_id,
        channel_id=channel_id,
        message_id=message_id,
        timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
        author="alice",
        content=content,
        extraction_status=extraction_status,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Idempotent upsert
# ─────────────────────────────────────────────────────────────────────────────


async def test_upsert_same_triple_twice_yields_one_document() -> None:
    """Spec scenario: ``Same message inserted twice``."""
    store, fake = _store_with_fake()
    msg = _msg(content="first")
    r1 = await store.upsert_channel_messages([msg])
    r2 = await store.upsert_channel_messages([_msg(content="first")])

    assert r1["inserted"] == 1
    assert r2["inserted"] == 0
    assert r2["matched"] == 1
    assert len(fake._docs) == 1


async def test_upsert_two_sources_same_message_id_yields_two_documents() -> None:
    """Spec scenario: ``Two messages with identical message_id but different
    source_id``."""
    store, fake = _store_with_fake()
    a = _msg(source_id="slack", message_id="m1")
    b = _msg(source_id="discord", message_id="m1")
    await store.upsert_channel_messages([a, b])

    assert len(fake._docs) == 2
    assert ("slack", "C123", "m1") in fake._docs
    assert ("discord", "C123", "m1") in fake._docs


async def test_upsert_resync_preserves_done_extraction_status() -> None:
    """Spec scenario: ``Re-sync of an already-extracted message``.

    ``$setOnInsert`` for ``extraction_status`` MUST preserve a row already in
    ``done`` so a re-sync does NOT re-queue it for extraction.
    """
    store, fake = _store_with_fake()
    # First sync — message lands as pending, then worker promotes it to done.
    await store.upsert_channel_messages([_msg(content="v1")])
    fake._docs[("slack", "C123", "m1")]["extraction_status"] = "done"

    # Re-sync with the same message (e.g. user clicks Sync again).
    await store.upsert_channel_messages([_msg(content="v1-edited")])

    doc = fake._docs[("slack", "C123", "m1")]
    # Mutable content updated.
    assert doc["content"] == "v1-edited"
    # Extraction status preserved — this is the $setOnInsert contract.
    assert doc["extraction_status"] == "done"


# ─────────────────────────────────────────────────────────────────────────────
# Read accessor
# ─────────────────────────────────────────────────────────────────────────────


async def test_get_channel_messages_returns_all_for_channel() -> None:
    store, _ = _store_with_fake()
    msgs = [_msg(message_id=f"m{i}") for i in range(5)]
    await store.upsert_channel_messages(msgs)
    rows = await store.get_channel_messages(channel_id="C123", limit=10)
    assert len(rows) == 5
    assert {r["message_id"] for r in rows} == {f"m{i}" for i in range(5)}


async def test_get_channel_messages_respects_limit() -> None:
    store, _ = _store_with_fake()
    await store.upsert_channel_messages([_msg(message_id=f"m{i}") for i in range(10)])
    rows = await store.get_channel_messages(channel_id="C123", limit=3)
    assert len(rows) == 3


# ─────────────────────────────────────────────────────────────────────────────
# Status aggregation
# ─────────────────────────────────────────────────────────────────────────────


async def test_count_channel_messages_by_status_zero_fills_missing() -> None:
    """Spec scenario: ``Channel has mixed extraction states``.

    The aggregation MUST always return all four keys, zero-filled for any
    status that has no rows.
    """
    store, fake = _store_with_fake()
    await store.upsert_channel_messages([_msg(message_id=f"m{i}") for i in range(5)])
    # Promote two rows to done, one to extracting, one to failed; leave one pending.
    fake._docs[("slack", "C123", "m0")]["extraction_status"] = "done"
    fake._docs[("slack", "C123", "m1")]["extraction_status"] = "done"
    fake._docs[("slack", "C123", "m2")]["extraction_status"] = "extracting"
    fake._docs[("slack", "C123", "m3")]["extraction_status"] = "failed"
    # m4 stays pending.

    counts = await store.count_channel_messages_by_status("C123")
    assert counts == {"pending": 1, "extracting": 1, "done": 2, "failed": 1}


async def test_count_channel_messages_by_status_empty_channel_zero_fills() -> None:
    store, _ = _store_with_fake()
    counts = await store.count_channel_messages_by_status("C_EMPTY")
    assert counts == {"pending": 0, "extracting": 0, "done": 0, "failed": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Phantom raw_messages fix (preprocessor + coreference resolver lookups)
# ─────────────────────────────────────────────────────────────────────────────


async def test_find_channel_message_by_message_id_returns_doc() -> None:
    """Used by preprocessor.py:252 / coreference_resolver.py:49 to replace
    the prior phantom ``raw_messages`` reads."""
    store, _ = _store_with_fake()
    await store.upsert_channel_messages([_msg(message_id="m_parent", content="parent text")])
    doc = await store.find_channel_message_by_message_id(channel_id="C123", message_id="m_parent")
    assert doc is not None
    assert doc["content"] == "parent text"


async def test_find_channel_message_by_message_id_returns_none_when_missing() -> None:
    store, _ = _store_with_fake()
    doc = await store.find_channel_message_by_message_id(channel_id="C123", message_id="m_missing")
    assert doc is None


async def test_upsert_persists_guild_id_for_discord_permalinks() -> None:
    """Discord ``guild_id`` must survive the upsert→read round-trip so the
    decoupled ExtractionWorker can rebuild it and the citation layer can build
    a ``discord.com/channels/{guild}/{channel}/{message}`` permalink. It lives
    on ``$setOnInsert`` (immutable provenance). Regression guard for the
    permalink fix — the round-trip is the one seam the write must not drop."""
    store, _ = _store_with_fake()
    msg = ChannelMessage(
        source_id="discord",
        channel_id="C123",
        message_id="m_guild",
        timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
        author="alice",
        content="hi",
        guild_id="G999",
    )
    await store.upsert_channel_messages([msg])
    doc = await store.find_channel_message_by_message_id(channel_id="C123", message_id="m_guild")
    assert doc is not None
    assert doc.get("guild_id") == "G999"


async def test_upsert_guild_id_defaults_empty_for_non_discord() -> None:
    """Non-Discord messages persist an empty guild_id (no permalink template
    reads it) — so the field is inert for Slack/Mattermost/Teams."""
    store, _ = _store_with_fake()
    await store.upsert_channel_messages([_msg(message_id="m_slack")])
    doc = await store.find_channel_message_by_message_id(channel_id="C123", message_id="m_slack")
    assert doc is not None
    assert doc.get("guild_id", "") == ""


async def test_resync_backfills_guild_id_onto_existing_row() -> None:
    """A row stored before guild_id existed must gain it on the next sync —
    hence guild_id lives in $set, not $setOnInsert. Without this, existing
    channels would never get clickable Discord permalinks."""
    store, _ = _store_with_fake()
    # First sync: a Discord message whose guild_id wasn't captured yet.
    before = ChannelMessage(
        source_id="discord",
        channel_id="C123",
        message_id="m_bf",
        timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
        author="alice",
        content="hi",
    )
    await store.upsert_channel_messages([before])
    # Re-sync: same message, now carrying the guild_id.
    after = ChannelMessage(
        source_id="discord",
        channel_id="C123",
        message_id="m_bf",
        timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=UTC),
        author="alice",
        content="hi",
        guild_id="G999",
    )
    await store.upsert_channel_messages([after])
    doc = await store.find_channel_message_by_message_id(channel_id="C123", message_id="m_bf")
    assert doc is not None
    assert doc.get("guild_id") == "G999"


# ─────────────────────────────────────────────────────────────────────────────
# State-machine validation
# ─────────────────────────────────────────────────────────────────────────────


async def test_update_status_accepts_legal_transition() -> None:
    """``pending → extracting`` is the worker's atomic-claim transition (PR-B)."""
    store, fake = _store_with_fake()
    await store.upsert_channel_messages([_msg()])
    ok = await store.update_channel_message_status(
        source_id="slack",
        channel_id="C123",
        message_id="m1",
        new_status="extracting",
    )
    assert ok is True
    assert fake._docs[("slack", "C123", "m1")]["extraction_status"] == "extracting"


async def test_update_status_rejects_illegal_transition() -> None:
    """``done → pending`` without going through ``failed → pending`` retry path
    is illegal — the worker MUST not silently re-queue a finished message."""
    store, fake = _store_with_fake()
    await store.upsert_channel_messages([_msg()])
    fake._docs[("slack", "C123", "m1")]["extraction_status"] = "done"

    ok = await store.update_channel_message_status(
        source_id="slack",
        channel_id="C123",
        message_id="m1",
        new_status="pending",
    )
    assert ok is False
    assert fake._docs[("slack", "C123", "m1")]["extraction_status"] == "done"


async def test_update_status_failed_increments_attempt_count() -> None:
    """When a message transitions to ``failed``, ``attempt_count`` increments
    so the worker can apply exponential backoff (PR-C)."""
    store, fake = _store_with_fake()
    await store.upsert_channel_messages([_msg()])
    fake._docs[("slack", "C123", "m1")]["extraction_status"] = "extracting"
    fake._docs[("slack", "C123", "m1")]["attempt_count"] = 1

    ok = await store.update_channel_message_status(
        source_id="slack",
        channel_id="C123",
        message_id="m1",
        new_status="failed",
        last_error="503 UNAVAILABLE",
    )
    assert ok is True
    doc = fake._docs[("slack", "C123", "m1")]
    assert doc["extraction_status"] == "failed"
    assert doc["attempt_count"] == 2
    assert doc["last_error"] == "503 UNAVAILABLE"


async def test_update_status_returns_false_when_message_missing() -> None:
    store, _ = _store_with_fake()
    ok = await store.update_channel_message_status(
        source_id="slack",
        channel_id="C123",
        message_id="m_missing",
        new_status="extracting",
    )
    assert ok is False


# ─────────────────────────────────────────────────────────────────────────────
# ExtractionWorker primitives (PR-B)
# ─────────────────────────────────────────────────────────────────────────────


async def test_claim_pending_returns_only_pending_rows_past_settle_window() -> None:
    """``claim_pending_messages_for_extraction`` honors the 5s settle window."""
    store, fake = _store_with_fake()
    now = datetime.now(tz=UTC)
    # Recent (within settle window) — must be skipped.
    fake._docs[("slack", "C1", "m1")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "m1",
        "extraction_status": "pending",
        "next_attempt_at": now,
        "created_at": now,  # ← brand new, inside settle window
        "timestamp": now,
        "content": "fresh",
    }
    # Older — must be claimed.
    fake._docs[("slack", "C1", "m2")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "m2",
        "extraction_status": "pending",
        "next_attempt_at": now,
        "created_at": now - timedelta(seconds=60),
        "timestamp": now,
        "content": "settled",
    }
    claimed = await store.claim_pending_messages_for_extraction(batch_size=10, settle_seconds=5)
    ids = [d["message_id"] for d in claimed]
    assert ids == ["m2"]
    # Status flipped to extracting.
    assert fake._docs[("slack", "C1", "m2")]["extraction_status"] == "extracting"
    # Recent row untouched.
    assert fake._docs[("slack", "C1", "m1")]["extraction_status"] == "pending"


async def test_claim_pending_skips_done_rows() -> None:
    store, fake = _store_with_fake()
    now = datetime.now(tz=UTC)
    fake._docs[("slack", "C1", "m1")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "m1",
        "extraction_status": "done",
        "next_attempt_at": now,
        "created_at": now - timedelta(minutes=5),
    }
    claimed = await store.claim_pending_messages_for_extraction(batch_size=10)
    assert claimed == []


async def test_claim_pending_respects_batch_size_cap() -> None:
    store, fake = _store_with_fake()
    now = datetime.now(tz=UTC)
    for i in range(7):
        fake._docs[("slack", "C1", f"m{i}")] = {
            "source_id": "slack",
            "channel_id": "C1",
            "message_id": f"m{i}",
            "extraction_status": "pending",
            "next_attempt_at": now,
            "created_at": now - timedelta(minutes=1),
            "timestamp": now,
            "content": "x",
        }
    claimed = await store.claim_pending_messages_for_extraction(batch_size=3)
    assert len(claimed) == 3


async def test_claim_pending_filters_by_channel_id() -> None:
    store, fake = _store_with_fake()
    now = datetime.now(tz=UTC)
    for ch in ("A", "B"):
        fake._docs[("slack", ch, "m1")] = {
            "source_id": "slack",
            "channel_id": ch,
            "message_id": "m1",
            "extraction_status": "pending",
            "next_attempt_at": now,
            "created_at": now - timedelta(minutes=1),
            "timestamp": now,
            "content": "x",
        }
    claimed = await store.claim_pending_messages_for_extraction(batch_size=10, channel_id="A")
    assert {d["channel_id"] for d in claimed} == {"A"}


async def test_finalize_extraction_status_bulk_marks_done() -> None:
    store, fake = _store_with_fake()
    fake._docs[("slack", "C1", "m1")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "m1",
        "extraction_status": "extracting",
    }
    fake._docs[("slack", "C1", "m2")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "m2",
        "extraction_status": "extracting",
    }
    modified = await store.finalize_extraction_status_bulk(
        keys=[("slack", "C1", "m1"), ("slack", "C1", "m2")],
        new_status="done",
    )
    assert modified == 2
    for key in (("slack", "C1", "m1"), ("slack", "C1", "m2")):
        assert fake._docs[key]["extraction_status"] == "done"


async def test_finalize_extraction_status_bulk_marks_failed_increments_attempt_count() -> None:
    store, fake = _store_with_fake()
    fake._docs[("slack", "C1", "m1")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "m1",
        "extraction_status": "extracting",
        "attempt_count": 0,
    }
    next_at = datetime.now(tz=UTC) + timedelta(seconds=30)
    modified = await store.finalize_extraction_status_bulk(
        keys=[("slack", "C1", "m1")],
        new_status="failed",
        last_error="503 UNAVAILABLE",
        next_attempt_at=next_at,
    )
    assert modified == 1
    doc = fake._docs[("slack", "C1", "m1")]
    assert doc["extraction_status"] == "failed"
    assert doc["attempt_count"] == 1
    assert doc["last_error"] == "503 UNAVAILABLE"


async def test_finalize_extraction_status_bulk_with_no_keys_returns_zero() -> None:
    store, _ = _store_with_fake()
    modified = await store.finalize_extraction_status_bulk(keys=[], new_status="done")
    assert modified == 0


async def test_sweep_stale_extracting_resets_old_rows_to_pending() -> None:
    store, fake = _store_with_fake()
    now = datetime.now(tz=UTC)
    fake._docs[("slack", "C1", "old")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "old",
        "extraction_status": "extracting",
        "updated_at": now - timedelta(minutes=15),
    }
    fake._docs[("slack", "C1", "fresh")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "fresh",
        "extraction_status": "extracting",
        "updated_at": now - timedelta(seconds=30),
    }
    swept = await store.sweep_stale_extracting(stale_seconds=600)
    assert swept == 1
    assert fake._docs[("slack", "C1", "old")]["extraction_status"] == "pending"
    assert fake._docs[("slack", "C1", "fresh")]["extraction_status"] == "extracting"


async def test_sweep_stale_extracting_returns_zero_when_no_stale_rows() -> None:
    store, _ = _store_with_fake()
    swept = await store.sweep_stale_extracting(stale_seconds=600)
    assert swept == 0


# ─────────────────────────────────────────────────────────────────────────────
# PR-C: auto-retry of failed rows whose backoff has elapsed
# ─────────────────────────────────────────────────────────────────────────────


async def test_claim_re_claims_failed_rows_below_max_retries() -> None:
    """Spec scenario: ``Failed message becomes eligible for retry``.

    Rows in ``failed`` state with ``attempt_count < max_retries`` AND
    ``next_attempt_at <= now`` must be re-claimed by the worker so the
    PR-C exponential backoff retry path actually retries them.
    """
    store, fake = _store_with_fake()
    now = datetime.now(tz=UTC)
    fake._docs[("slack", "C1", "retry-me")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "retry-me",
        "extraction_status": "failed",
        "attempt_count": 1,
        "next_attempt_at": now - timedelta(seconds=1),  # backoff elapsed
        "created_at": now - timedelta(minutes=5),
        "timestamp": now,
        "content": "x",
    }
    claimed = await store.claim_pending_messages_for_extraction(batch_size=10, max_retries=5)
    ids = [d["message_id"] for d in claimed]
    assert ids == ["retry-me"]
    # Status flipped to extracting; attempt_count preserved (NOT reset).
    doc = fake._docs[("slack", "C1", "retry-me")]
    assert doc["extraction_status"] == "extracting"
    assert doc["attempt_count"] == 1


async def test_claim_does_not_retry_rows_at_max_retries() -> None:
    """Spec scenario: ``Message exhausts retry budget``.

    A row whose ``attempt_count == max_retries`` is permanently failed.
    The worker must skip it so the row stays out of the queue.
    """
    store, fake = _store_with_fake()
    now = datetime.now(tz=UTC)
    fake._docs[("slack", "C1", "exhausted")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "exhausted",
        "extraction_status": "failed",
        "attempt_count": 5,  # at max
        "next_attempt_at": now - timedelta(minutes=1),
        "created_at": now - timedelta(minutes=10),
        "timestamp": now,
    }
    claimed = await store.claim_pending_messages_for_extraction(batch_size=10, max_retries=5)
    assert claimed == []
    # Row stays failed.
    assert fake._docs[("slack", "C1", "exhausted")]["extraction_status"] == "failed"


async def test_claim_does_not_retry_failed_rows_in_backoff() -> None:
    """A failed row whose ``next_attempt_at`` is still in the future
    must NOT be re-claimed even if ``attempt_count < max_retries``.
    The backoff schedule is the budget governor."""
    store, fake = _store_with_fake()
    now = datetime.now(tz=UTC)
    fake._docs[("slack", "C1", "in-backoff")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "in-backoff",
        "extraction_status": "failed",
        "attempt_count": 2,
        "next_attempt_at": now + timedelta(minutes=5),  # still in cooldown
        "created_at": now - timedelta(minutes=10),
        "timestamp": now,
    }
    claimed = await store.claim_pending_messages_for_extraction(batch_size=10, max_retries=5)
    assert claimed == []


async def test_claim_drains_pending_and_retried_in_one_pass() -> None:
    """Both fresh-pending and retry-eligible-failed rows must surface
    in the same claim cycle so the worker doesn't have to alternate."""
    store, fake = _store_with_fake()
    now = datetime.now(tz=UTC)
    fake._docs[("slack", "C1", "fresh")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "fresh",
        "extraction_status": "pending",
        "attempt_count": 0,
        "next_attempt_at": now,
        "created_at": now - timedelta(minutes=5),
        "timestamp": now,
    }
    fake._docs[("slack", "C1", "retry")] = {
        "source_id": "slack",
        "channel_id": "C1",
        "message_id": "retry",
        "extraction_status": "failed",
        "attempt_count": 1,
        "next_attempt_at": now - timedelta(seconds=1),
        "created_at": now - timedelta(minutes=5),
        "timestamp": now,
    }
    claimed = await store.claim_pending_messages_for_extraction(batch_size=10, max_retries=5)
    ids = sorted(d["message_id"] for d in claimed)
    assert ids == ["fresh", "retry"]
