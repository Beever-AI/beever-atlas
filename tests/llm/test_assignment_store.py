"""PR-B: Assignment data model + AssignmentStore."""

from __future__ import annotations

from typing import Any

import pytest

from beever_atlas.llm.assignments import (
    DEFAULT_CONSUMERS,
    Assignment,
    AssignmentStore,
)


class _AsyncCursor:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "_AsyncCursor":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _UpdateResult:
    def __init__(self, matched: int) -> None:
        self.matched_count = matched
        self.modified_count = matched


class _DeleteResult:
    def __init__(self, deleted: int) -> None:
        self.deleted_count = deleted


class _FakeCollection:
    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any], _projection: Any = None) -> _AsyncCursor:
        return _AsyncCursor([d for d in self._docs if self._matches(d, query)])

    async def find_one(self, query: dict[str, Any], _projection: Any = None) -> Any:
        for d in self._docs:
            if self._matches(d, query):
                return d
        return None

    async def update_one(
        self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> _UpdateResult:
        for d in self._docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))
                return _UpdateResult(matched=1)
        if upsert:
            new = dict(update.get("$set", {}))
            new.update(query)
            self._docs.append(new)
        return _UpdateResult(matched=0)

    async def delete_one(self, query: dict[str, Any]) -> _DeleteResult:
        for d in list(self._docs):
            if self._matches(d, query):
                self._docs.remove(d)
                return _DeleteResult(deleted=1)
        return _DeleteResult(deleted=0)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        if not query:
            return True
        if "$or" in query:
            return any(_FakeCollection._matches(doc, q) for q in query["$or"])
        return all(doc.get(k) == v for k, v in query.items())


class _FakeMongo:
    def __init__(self) -> None:
        self.db = {"llm_assignments": _FakeCollection()}


@pytest.fixture
def store() -> AssignmentStore:
    return AssignmentStore(_FakeMongo())


def test_default_consumers_includes_embedding_and_all_agents() -> None:
    """The default consumer set MUST contain embedding plus the 16 agents."""
    assert "embedding" in DEFAULT_CONSUMERS
    assert "qa_agent" in DEFAULT_CONSUMERS
    assert "image_describer" in DEFAULT_CONSUMERS
    assert len(DEFAULT_CONSUMERS) == 17


@pytest.mark.asyncio
async def test_upsert_persists_assignment(store: AssignmentStore) -> None:
    a = Assignment(
        consumer="fact_extractor",
        endpoint_id="ep-1",
        model="gemini-2.5-flash",
        temperature=0.2,
    )
    saved = await store.upsert(a)
    assert saved.updated_at  # stamped
    fetched = await store.get("fact_extractor")
    assert fetched is not None
    assert fetched.endpoint_id == "ep-1"
    assert fetched.model == "gemini-2.5-flash"
    assert fetched.temperature == 0.2


@pytest.mark.asyncio
async def test_upsert_overwrites_existing(store: AssignmentStore) -> None:
    await store.upsert(Assignment(consumer="qa_agent", endpoint_id="ep-1", model="m"))
    await store.upsert(Assignment(consumer="qa_agent", endpoint_id="ep-2", model="m2"))
    fetched = await store.get("qa_agent")
    assert fetched is not None
    assert fetched.endpoint_id == "ep-2"
    assert fetched.model == "m2"


@pytest.mark.asyncio
async def test_list_returns_all(store: AssignmentStore) -> None:
    await store.upsert(Assignment(consumer="a", endpoint_id="ep1", model="m1"))
    await store.upsert(Assignment(consumer="b", endpoint_id="ep2", model="m2"))
    everything = await store.list()
    assert {a.consumer for a in everything} == {"a", "b"}


@pytest.mark.asyncio
async def test_delete(store: AssignmentStore) -> None:
    await store.upsert(Assignment(consumer="x", endpoint_id="ep", model="m"))
    assert await store.delete("x") is True
    assert await store.get("x") is None
    assert await store.delete("x") is False


@pytest.mark.asyncio
async def test_list_referencing_endpoint_primary(store: AssignmentStore) -> None:
    await store.upsert(Assignment(consumer="a", endpoint_id="ep-target", model="m"))
    await store.upsert(Assignment(consumer="b", endpoint_id="ep-other", model="m"))
    refs = await store.list_referencing_endpoint("ep-target")
    assert len(refs) == 1
    assert refs[0].consumer == "a"


@pytest.mark.asyncio
async def test_list_referencing_endpoint_fallback(store: AssignmentStore) -> None:
    """An Endpoint referenced only as a fallback SHALL also surface."""
    await store.upsert(
        Assignment(
            consumer="qa_agent",
            endpoint_id="ep-primary",
            model="m",
            fallback_endpoint_id="ep-target",
        )
    )
    refs = await store.list_referencing_endpoint("ep-target")
    assert len(refs) == 1
    assert refs[0].consumer == "qa_agent"


@pytest.mark.asyncio
async def test_list_referencing_endpoint_empty(store: AssignmentStore) -> None:
    assert await store.list_referencing_endpoint("nonexistent") == []


def test_assignment_round_trip_through_document() -> None:
    """from_document / to_document preserve every field."""
    original = Assignment(
        consumer="qa_agent",
        endpoint_id="ep-uuid",
        model="anthropic/claude-sonnet-4-6",
        temperature=0.1,
        max_tokens=2048,
        response_format="json",
        extra_headers={"X-Custom": "yes"},
        fallback_endpoint_id="ep-fallback",
        dimensions=None,
        task=None,
        updated_at="2026-05-12T10:00:00+00:00",
    )
    doc = original.to_document()
    rehydrated = Assignment.from_document(doc)
    assert rehydrated == original


def test_assignment_from_document_tolerates_missing_optional_fields() -> None:
    """Old / partial documents hydrate with sensible defaults."""
    doc = {"consumer": "x", "endpoint_id": "ep", "model": "m"}
    a = Assignment.from_document(doc)
    assert a.consumer == "x"
    assert a.temperature is None
    assert a.extra_headers == {}
    assert a.fallback_endpoint_id is None
