"""Round-trip test for the ExternalSource registry (PR-D).

Code-review CRITICAL guard (second pass): the ``Field(exclude=True)``
on ``ExternalSource.secret`` excludes it from ``model_dump()``. The
persistence path in ``upsert_external_source`` re-adds it explicitly
so the secret lands in MongoDB. Without that re-add, every push
request returns 401 'Invalid signature' because re-reading the source
fails Pydantic validation (``secret`` is a required field).

This test exercises the full register → fetch → verify cycle so the
regression cannot recur silently.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/push-source-ingestion/``
"""

from __future__ import annotations

from typing import Any

from beever_atlas.models.persistence import ExternalSource
from beever_atlas.stores.mongodb_store import MongoDBStore


class _FakeExternalSourcesCollection:
    """In-memory stand-in for the ``external_sources`` motor collection.

    Implements just the operators ``upsert_external_source`` and
    ``get_external_source`` actually use: ``find_one``, ``update_one``
    with ``$set`` + ``upsert=True``, and ``delete_one``.
    """

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        source_id = query.get("source_id")
        if source_id is None:
            return None
        doc = self._docs.get(source_id)
        return dict(doc) if doc else None

    async def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> object:
        source_id = query["source_id"]
        existing = self._docs.get(source_id)
        if existing is None and not upsert:
            return object()
        if existing is None:
            self._docs[source_id] = {}
            existing = self._docs[source_id]
        for k, v in update.get("$set", {}).items():
            existing[k] = v
        return object()

    async def delete_one(self, query: dict[str, Any]) -> object:
        source_id = query.get("source_id")

        class _Result:
            def __init__(self, deleted: int) -> None:
                self.deleted_count = deleted

        if source_id in self._docs:
            del self._docs[source_id]
            return _Result(1)
        return _Result(0)


def _store_with_fake() -> tuple[MongoDBStore, _FakeExternalSourcesCollection]:
    fake = _FakeExternalSourcesCollection()
    store = MongoDBStore.__new__(MongoDBStore)
    store._external_sources = fake  # type: ignore[attr-defined]
    return store, fake


async def test_upsert_then_get_external_source_round_trip() -> None:
    """Spec scenario: ``Source registration``.

    Code-review CRITICAL: registering a source then fetching it back
    must return the SAME secret. The Field(exclude=True) annotation
    on the model could regress this if the persistence path stops
    explicitly re-adding the secret to the dump.
    """
    store, _ = _store_with_fake()
    src = ExternalSource(
        source_id="openclaw-prod",
        secret="LONG-RANDOM-HMAC-KEY-32-bytes-or-more",
        secret_fingerprint="",  # auto-derived in upsert
    )
    await store.upsert_external_source(src)

    fetched = await store.get_external_source("openclaw-prod")
    assert fetched is not None
    assert fetched.source_id == "openclaw-prod"
    assert fetched.secret == "LONG-RANDOM-HMAC-KEY-32-bytes-or-more", (
        "secret round-trip broken — Field(exclude=True) is stripping the "
        "plaintext key from persistence. The push endpoint will return 401 "
        "for every request."
    )
    # secret_fingerprint is auto-derived from the secret.
    assert fetched.secret_fingerprint != ""


async def test_upsert_external_source_persists_secret_in_mongo_doc() -> None:
    """Inspect the actual stored document — secret MUST be present.

    The first reviewer found the regression by reading the production
    code; this test catches it via observable Mongo state so a future
    regression won't reach production.
    """
    store, fake = _store_with_fake()
    await store.upsert_external_source(
        ExternalSource(
            source_id="hermes-staging",
            secret="another-key",
            secret_fingerprint="",
        )
    )
    doc = fake._docs["hermes-staging"]
    assert "secret" in doc, (
        "secret missing from stored doc — Field(exclude=True) bypassed the persistence path"
    )
    assert doc["secret"] == "another-key"
    assert "secret_fingerprint" in doc


async def test_get_external_source_returns_none_when_missing() -> None:
    store, _ = _store_with_fake()
    assert await store.get_external_source("missing") is None


async def test_external_source_secret_excluded_from_model_dump_in_isolation() -> None:
    """The other half of the contract: ``model_dump()`` MUST exclude
    the secret so a future ``GET /api/sources`` admin endpoint can't
    accidentally leak it. This is the original CRITICAL fix being
    locked in alongside the regression guard."""
    src = ExternalSource(
        source_id="openclaw-prod",
        secret="DO-NOT-LEAK",
        secret_fingerprint="abc",
    )
    dumped = src.model_dump()
    assert "secret" not in dumped
    assert src.model_dump(mode="json").get("secret") is None


async def test_upsert_external_source_rotation_sets_rotated_at() -> None:
    """Spec scenario: ``Secret rotation``."""
    store, fake = _store_with_fake()
    await store.upsert_external_source(
        ExternalSource(
            source_id="openclaw",
            secret="original-key",
            secret_fingerprint="",
        )
    )
    # No rotated_at on first insert.
    assert fake._docs["openclaw"].get("rotated_at") is None

    # Rotate.
    await store.upsert_external_source(
        ExternalSource(
            source_id="openclaw",
            secret="new-key",
            secret_fingerprint="",
        )
    )
    # rotated_at is set; secret is updated.
    assert fake._docs["openclaw"].get("rotated_at") is not None
    assert fake._docs["openclaw"]["secret"] == "new-key"
    # Fingerprint reflects the new key.
    fetched = await store.get_external_source("openclaw")
    assert fetched is not None
    assert fetched.secret == "new-key"


async def test_delete_external_source_removes_the_row() -> None:
    store, fake = _store_with_fake()
    await store.upsert_external_source(
        ExternalSource(source_id="x", secret="k", secret_fingerprint="")
    )
    deleted = await store.delete_external_source("x")
    assert deleted is True
    assert "x" not in fake._docs


async def test_delete_missing_external_source_returns_false() -> None:
    store, _ = _store_with_fake()
    deleted = await store.delete_external_source("never-registered")
    assert deleted is False
