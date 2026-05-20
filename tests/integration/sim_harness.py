"""Simulated end-to-end test harness.

Phase 5 of ``sync-pipeline-feedback-and-auto-wiki``. Provides a single
``SimStack`` object that wires together fake mongo + an
``ExtractionWorker`` + an ``AutoOverviewSubscriber`` + a
``WikiMaintainer``-shaped recorder + an LLM mock so each integration
test can exercise the real service code paths without burning provider
quota or starting docker.

Design choices
--------------
* Mock at the store layer (fastest, no docker dependency) — the
  proposal explicitly allows this.
* The LLM mock is keyed on the request shape so the same input always
  produces the same fact extractions (deterministic, replayable).
* All time-sensitive pieces (debounce, throttle window) are compressed
  to <1s so the suite runs in <60s wall clock.

Spec: ``openspec/changes/sync-pipeline-feedback-and-auto-wiki/tasks.md``
section 5.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from beever_atlas.services.auto_overview_subscriber import AutoOverviewSubscriber
from beever_atlas.services.extraction_worker import ExtractionWorker
from beever_atlas.services.pipeline_events import get_pipeline_events


# ---------------------------------------------------------------------------
# LLM mock — deterministic, fault-injectable
# ---------------------------------------------------------------------------


@dataclass
class LLMCallRecord:
    """One recorded mock LLM call."""

    provider: str
    kind: str  # "completion" | "embedding"
    model: str
    payload_snippet: str
    ts: float


@dataclass
class FaultRule:
    """One fault-injection rule.

    ``match`` is a callable receiving the request kwargs; when True the
    fault fires. ``mode`` chooses how:

    * ``"429"``  — raise a stub RateLimitError carrying ``status_code=429``
    * ``"timeout"`` — raise ``asyncio.TimeoutError``
    * ``"malformed"`` — return a non-JSON string when JSON was expected
    * ``"empty"`` — return an empty completion

    ``count`` lets a rule apply to the first N matching calls and then
    stop firing (the underlying happy path takes over).
    """

    match: Any  # Callable[[dict], bool]
    mode: str
    count: int = 10**9


class _StubRateLimit(Exception):
    """Stand-in for ``litellm.RateLimitError``.

    The dispatcher's ``_is_429`` helper detects rate-limit errors by
    isinstance check against ``litellm.RateLimitError`` first, then
    falls through to a status-code/message sniff. Carrying
    ``status_code=429`` covers the second path so tests can patch
    ``litellm.acompletion`` without also patching
    ``litellm.RateLimitError``.
    """

    status_code = 429

    def __init__(self, message: str = "rate limit exceeded") -> None:
        super().__init__(message)


class LLMMock:
    """Replaces ``litellm.acompletion`` and ``litellm.aembedding``.

    Holds counters per ``(provider, kind)`` plus a queue of fault rules.
    Tests configure faults via ``inject_429`` / ``inject_for_n_seconds``.
    """

    def __init__(self) -> None:
        self.calls: list[LLMCallRecord] = []
        self._rules: list[FaultRule] = []
        self._global_429_until: float = 0.0
        self._sub_batch_target_429: int | None = None

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------

    def call_count(self, provider: str | None = None, kind: str | None = None) -> int:
        out = 0
        for c in self.calls:
            if provider is not None and c.provider != provider:
                continue
            if kind is not None and c.kind != kind:
                continue
            out += 1
        return out

    def reset_calls(self) -> None:
        self.calls.clear()

    # ------------------------------------------------------------------
    # Fault injection
    # ------------------------------------------------------------------

    def inject_429_for_seconds(self, seconds: float) -> None:
        """Force every completion call to return 429 until ``seconds`` elapse."""
        self._global_429_until = time.monotonic() + seconds

    def clear_global_429(self) -> None:
        self._global_429_until = 0.0

    def add_rule(self, rule: FaultRule) -> None:
        self._rules.append(rule)

    def clear_rules(self) -> None:
        self._rules.clear()

    # ------------------------------------------------------------------
    # The two replacement coroutines
    # ------------------------------------------------------------------

    async def acompletion(self, *, model: str, messages: Any, **kwargs: Any) -> Any:
        provider = _provider_from_model(model)
        record = LLMCallRecord(
            provider=provider,
            kind="completion",
            model=str(model),
            payload_snippet=_snippet(messages),
            ts=time.monotonic(),
        )
        self.calls.append(record)

        # Global 429 window — used by Scenario I (storm recovery).
        if record.ts < self._global_429_until:
            raise _StubRateLimit("global 429 window")

        # Targeted rules.
        for rule in list(self._rules):
            try:
                hit = rule.match({"model": model, "messages": messages, **kwargs})
            except Exception:  # noqa: BLE001
                hit = False
            if not hit:
                continue
            if rule.count <= 0:
                continue
            rule.count -= 1
            return await self._fire_rule(rule)

        # Happy path: deterministic completion shaped for the persister
        # actor. The test paths never read the JSON, so a minimal stub
        # is sufficient.
        return _StubCompletionResponse(content='{"facts":[],"entities":[],"relationships":[]}')

    async def aembedding(self, *, model: str, input: Any, **kwargs: Any) -> Any:
        provider = _provider_from_model(model)
        record = LLMCallRecord(
            provider=provider,
            kind="embedding",
            model=str(model),
            payload_snippet=_snippet(input),
            ts=time.monotonic(),
        )
        self.calls.append(record)

        # Targeted rules apply to embeddings too.
        for rule in list(self._rules):
            try:
                hit = rule.match({"model": model, "input": input, **kwargs})
            except Exception:  # noqa: BLE001
                hit = False
            if not hit:
                continue
            if rule.count <= 0:
                continue
            rule.count -= 1
            return await self._fire_rule(rule)

        # Happy path: 8-d vector per input row, value derived from the
        # snippet length so two identical inputs hash to the same
        # vector.
        items = input if isinstance(input, list) else [input]
        return _StubEmbeddingResponse(items)

    async def _fire_rule(self, rule: FaultRule) -> Any:
        if rule.mode == "429":
            raise _StubRateLimit("rule 429")
        if rule.mode == "timeout":
            raise TimeoutError("rule timeout")
        if rule.mode == "malformed":
            return _StubCompletionResponse(content="<<<not json>>>")
        if rule.mode == "empty":
            return _StubCompletionResponse(content="{}")
        raise RuntimeError(f"unknown fault rule mode: {rule.mode}")


def _provider_from_model(model: str) -> str:
    """Map a litellm model string to its provider key.

    LiteLLM accepts ``gemini/gemini-2.0-flash`` and bare ``gpt-4o``.
    The first token before ``/`` is the provider; bare strings imply
    OpenAI per LiteLLM's convention.
    """
    s = str(model)
    if "/" in s:
        return s.split("/", 1)[0].lower().replace("-", "_")
    return "openai"


def _snippet(payload: Any) -> str:
    raw = str(payload)
    return raw[:120]


class _StubCompletionResponse:
    """Minimal stand-in for a LiteLLM completion response."""

    def __init__(self, content: str) -> None:
        choice = MagicMock()
        choice.message = MagicMock()
        choice.message.content = content
        self.choices = [choice]
        self.status_code = 200


class _StubEmbeddingResponse:
    """Minimal stand-in for a LiteLLM embedding response.

    ``data[i].embedding`` mimics the LiteLLM shape; the embedding pipeline
    doesn't actually inspect dimensions for the simulated tests.
    """

    def __init__(self, items: list[Any]) -> None:
        self.data = []
        for idx, item in enumerate(items):
            entry = {"embedding": [float((len(str(item)) + idx) % 7) for _ in range(8)]}
            self.data.append(entry)
        self.status_code = 200
        # LiteLLM responses are dict-accessible via __getitem__; tests
        # that read into the response shape go through that path.
        self._dict_view = {"data": self.data}

    def __getitem__(self, key: str) -> Any:
        return self._dict_view[key]


# ---------------------------------------------------------------------------
# Fake Mongo — implements just the methods the worker / subscriber / API call.
# ---------------------------------------------------------------------------


@dataclass
class _ChannelMessage:
    """Minimal in-memory channel_messages row."""

    source_id: str
    channel_id: str
    message_id: str
    channel_name: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    author: str = ""
    author_name: str = ""
    author_image: str = ""
    content: str = ""
    thread_id: str | None = None
    attachments: list[Any] = field(default_factory=list)
    reactions: list[Any] = field(default_factory=list)
    reply_count: int = 0
    raw_metadata: dict[str, Any] = field(default_factory=dict)
    extraction_status: str = "pending"
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    last_error: str | None = None
    extracting_started_at: datetime | None = None

    def key(self) -> tuple[str, str, str]:
        return (self.source_id, self.channel_id, self.message_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "channel_name": self.channel_name,
            "timestamp": self.timestamp,
            "author": self.author,
            "author_name": self.author_name,
            "author_image": self.author_image,
            "content": self.content,
            "thread_id": self.thread_id,
            "attachments": list(self.attachments),
            "reactions": list(self.reactions),
            "reply_count": self.reply_count,
            "raw_metadata": dict(self.raw_metadata),
            "extraction_status": self.extraction_status,
            "attempt_count": self.attempt_count,
            "next_attempt_at": self.next_attempt_at,
        }


class _FakeWikiPagesCollection:
    def __init__(self, store: "_FakeMongo") -> None:
        self._store = store

    async def find_one(
        self,
        query: dict[str, Any],
        projection: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        ch = query.get("channel_id")
        page_type = query.get("page_type")
        for page in self._store.wiki_pages:
            if page.get("channel_id") == ch and (
                page_type is None or page.get("page_type") == page_type
            ):
                return dict(page)
        return None

    async def insert_one(self, doc: dict[str, Any]) -> Any:
        self._store.wiki_pages.append(dict(doc))
        return MagicMock(inserted_id="x")


class _FakeDB:
    def __init__(self, store: "_FakeMongo") -> None:
        self._store = store

    def __getitem__(self, name: str) -> Any:
        if name == "wiki_pages":
            return _FakeWikiPagesCollection(self._store)
        raise KeyError(name)


class _FakeMongo:
    """In-memory mongo-shaped store that the production code can talk to.

    Implements only the methods the extraction worker, the auto-overview
    subscriber, and the sync status API actually call. Anything outside
    that surface raises ``AttributeError`` on the store, surfacing test
    coverage gaps loudly rather than silently no-opping.
    """

    def __init__(self) -> None:
        self.messages: list[_ChannelMessage] = []
        self.wiki_pages: list[dict[str, Any]] = []
        self.sync_progress_log: list[dict[str, Any]] = []
        self.batch_stage_log: list[dict[str, Any]] = []
        self.activity_log: list[dict[str, Any]] = []
        self.checkpoints: dict[str, dict[str, Any]] = {}
        self.completed_jobs: list[dict[str, Any]] = []
        self.batches_completed_count: int = 0
        self._sync_jobs: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Mongo-store API used by ExtractionWorker
    # ------------------------------------------------------------------

    @property
    def db(self) -> Any:
        return _FakeDB(self)

    async def get_purging_channel_ids(self) -> set[str]:
        """Sim default: no channels are mid-purge. Mirrors the real store's
        accessor so ExtractionWorker.tick's Wave-0 purge filter resolves
        cleanly instead of hitting the broad-except fallback."""
        return set()

    async def claim_pending_messages_for_extraction(
        self,
        *,
        batch_size: int,
        channel_id: str | None = None,
        settle_seconds: int = 5,
        max_retries: int = 5,
        purging_channel_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return up to ``batch_size`` rows whose extraction_status=pending.

        Atomically flips them to extracting + bumps attempt_count.
        Honours per-row ``next_attempt_at`` retry barriers.
        Excludes channels in ``purging_channel_ids`` (delete-channel Wave 0).
        """
        now = datetime.now(tz=UTC)
        purging = purging_channel_ids or set()
        out: list[dict[str, Any]] = []
        for msg in self.messages:
            if len(out) >= batch_size:
                break
            if channel_id is not None and msg.channel_id != channel_id:
                continue
            if msg.channel_id in purging:
                continue
            if msg.extraction_status == "pending":
                # Retry barrier: rows reset to pending after a failure carry
                # ``next_attempt_at``; skip until the wall clock catches up.
                if msg.next_attempt_at is not None and msg.next_attempt_at > now:
                    continue
                msg.extraction_status = "extracting"
                msg.attempt_count += 1
                msg.extracting_started_at = now
                out.append(msg.to_dict() | {"attempt_count": msg.attempt_count})
                continue
            if (
                msg.extraction_status == "failed"
                and msg.attempt_count < max_retries
                and (msg.next_attempt_at is None or msg.next_attempt_at <= now)
            ):
                msg.extraction_status = "extracting"
                msg.attempt_count += 1
                msg.extracting_started_at = now
                out.append(msg.to_dict() | {"attempt_count": msg.attempt_count})
        return out

    async def finalize_extraction_status_bulk(
        self,
        *,
        keys: list[tuple[str, str, str]],
        new_status: str,
        last_error: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> int:
        modified = 0
        key_set = {tuple(k) for k in keys}
        for msg in self.messages:
            if msg.key() in key_set:
                msg.extraction_status = new_status
                msg.last_error = last_error
                msg.next_attempt_at = next_attempt_at
                modified += 1
        return modified

    async def sweep_stale_extracting(self, *, stale_seconds: int = 600) -> int:
        now = datetime.now(tz=UTC)
        n = 0
        for msg in self.messages:
            if msg.extraction_status != "extracting":
                continue
            if msg.extracting_started_at is None:
                continue
            age = (now - msg.extracting_started_at).total_seconds()
            if age >= stale_seconds:
                msg.extraction_status = "pending"
                msg.extracting_started_at = None
                n += 1
        return n

    async def count_channel_messages_by_status(self, channel_id: str) -> dict[str, int]:
        out = {"pending": 0, "extracting": 0, "done": 0, "failed": 0}
        for msg in self.messages:
            if msg.channel_id != channel_id:
                continue
            out[msg.extraction_status] = out.get(msg.extraction_status, 0) + 1
        return out

    async def count_channel_messages_failure_subtypes(
        self, channel_id: str, *, max_retries: int
    ) -> dict[str, int]:
        retrying = 0
        abandoned = 0
        now = datetime.now(tz=UTC)
        for msg in self.messages:
            if msg.channel_id != channel_id:
                continue
            if msg.extraction_status != "failed":
                continue
            if msg.attempt_count >= max_retries:
                abandoned += 1
            elif msg.next_attempt_at is not None and msg.next_attempt_at > now:
                retrying += 1
            else:
                # Still considered retrying — eligible for next claim.
                retrying += 1
        return {"retrying": retrying, "abandoned": abandoned}

    async def refresh_sync_progress_for_channel(self, channel_id: str) -> None:
        # Best-effort observability — no-op for the simulator.
        return None

    # ------------------------------------------------------------------
    # Sync API surface
    # ------------------------------------------------------------------

    async def get_sync_status(self, channel_id: str) -> Any:
        # Returns the active job, or None if no job exists.
        return self._sync_jobs.get(channel_id)

    async def get_sync_jobs_for_channel(self, *, channel_id: str, limit: int) -> list[Any]:
        job = self._sync_jobs.get(channel_id)
        return [job] if job else []

    async def complete_sync_job(self, **kwargs: Any) -> None:
        job_id = kwargs.get("job_id")
        for job in self._sync_jobs.values():
            if getattr(job, "id", None) == job_id:
                job.status = kwargs.get("status", "completed")
                job.completed_at = datetime.now(tz=UTC)
                if "errors" in kwargs:
                    job.errors = kwargs["errors"]

    # ------------------------------------------------------------------
    # BatchProcessor surface (called by integration tests via the worker)
    # ------------------------------------------------------------------

    async def update_sync_progress(self, *args: Any, **kwargs: Any) -> None:
        self.sync_progress_log.append(kwargs)

    async def update_batch_stage(self, *args: Any, **kwargs: Any) -> None:
        self.batch_stage_log.append(kwargs)

    async def push_activity_log_entry(self, *args: Any, **kwargs: Any) -> None:
        self.activity_log.append(kwargs)

    async def load_pipeline_checkpoint(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def save_pipeline_checkpoint(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def delete_pipeline_checkpoint(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def increment_batches_completed(self, *args: Any, **kwargs: Any) -> None:
        self.batches_completed_count += 1


# ---------------------------------------------------------------------------
# Synthetic message generator
# ---------------------------------------------------------------------------


def make_messages(
    channel_id: str,
    n: int,
    *,
    topics: list[str] | None = None,
    source_id: str = "src",
    channel_name: str = "general",
    base_time: datetime | None = None,
) -> list[_ChannelMessage]:
    """Build ``n`` synthetic ``_ChannelMessage`` rows.

    Each message rotates through the supplied ``topics`` so the
    deterministic LLM mock can route messages to facts. Returned rows
    have ``extraction_status="pending"`` so the worker's first claim
    picks them up.
    """
    if topics is None:
        topics = ["alpha", "beta", "gamma"]
    base_time = base_time or datetime(2026, 5, 1, tzinfo=UTC)
    out: list[_ChannelMessage] = []
    for i in range(n):
        topic = topics[i % len(topics)]
        out.append(
            _ChannelMessage(
                source_id=source_id,
                channel_id=channel_id,
                message_id=f"m{i:04d}",
                channel_name=channel_name,
                timestamp=base_time + timedelta(seconds=i * 5),
                author=f"user-{i % 3}",
                author_name=f"User {i % 3}",
                content=f"discussing {topic} item {i} with details",
                extraction_status="pending",
                attempt_count=0,
            )
        )
    return out


# ---------------------------------------------------------------------------
# SimStack — the umbrella object integration tests pin against.
# ---------------------------------------------------------------------------


class _RecordingMaintainer:
    """A WikiMaintainer-shape recorder.

    Captures every ``on_extraction_done`` invocation so tests can assert
    routing behaviour without standing up the real maintainer (whose
    routing depends on a live Weaviate). The one place this matters is
    Scenario C (incremental update) and the burst-coalesce assertion.

    The actual debounce-collapse semantics are covered by
    ``tests/services/test_wiki_maintainer_debounce.py`` so the harness
    only needs to record routing, not re-validate the debounce logic.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def on_extraction_done(self, channel_id: str, fact_ids: list[str]) -> None:
        self.calls.append((channel_id, list(fact_ids)))

    def reset(self) -> None:
        self.calls.clear()


@dataclass
class SimStack:
    """One-stop integration harness.

    Each integration test asks for a fresh ``SimStack`` via the
    ``sim_stack`` fixture. Tests inject messages, drive the worker, and
    assert against the various counters / mongo state the harness
    exposes.
    """

    mongo: _FakeMongo
    llm: LLMMock
    worker: ExtractionWorker
    subscriber: AutoOverviewSubscriber
    maintainer: _RecordingMaintainer
    wiki_pages_added: list[dict[str, Any]] = field(default_factory=list)
    overview_generator_calls: list[tuple[str, str]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Helpers used by tests
    # ------------------------------------------------------------------

    def inject_messages(
        self,
        channel_id: str,
        n: int,
        *,
        topics: list[str] | None = None,
    ) -> None:
        self.mongo.messages.extend(make_messages(channel_id, n, topics=topics))

    async def run_worker_until_quiet(
        self,
        channel_id: str,
        *,
        max_ticks: int = 20,
        include_retries: bool = False,
    ) -> dict[str, int]:
        """Drive the worker until pending+extracting are zero.

        ``include_retries=True`` keeps ticking while ``failed`` rows are
        eligible for retry (``next_attempt_at <= now`` and
        ``attempt_count < max_retries``). The default stops when the
        queue is quiet so happy-path tests do not loop forever.

        Returns the final counters dict from the last tick.
        """
        counters = {"claimed": 0, "succeeded": 0, "failed": 0, "channels": 0}
        for _ in range(max_ticks):
            counts = await self.mongo.count_channel_messages_by_status(channel_id)
            quiet = counts.get("pending", 0) == 0 and counts.get("extracting", 0) == 0
            if quiet and not include_retries:
                break
            if quiet and include_retries:
                # Stop when the failed rows have ALSO been re-claimed —
                # i.e. nothing is eligible for re-claim.
                now = datetime.now(tz=UTC)
                eligible = any(
                    m.channel_id == channel_id
                    and m.extraction_status == "failed"
                    and m.attempt_count < 5
                    and (m.next_attempt_at is None or m.next_attempt_at <= now)
                    for m in self.mongo.messages
                )
                if not eligible:
                    break
            counters = await self.worker.tick(channel_id=channel_id)
            await asyncio.sleep(0)
        return counters

    async def fact_count(self, channel_id: str) -> int:
        # The simulator does not stand up Weaviate; "facts" are produced
        # downstream of the LLM mock. We approximate with the count of
        # rows that successfully completed extraction — this is the
        # signal the user-facing scenarios actually care about (1 row →
        # >=1 fact).
        counts = await self.mongo.count_channel_messages_by_status(channel_id)
        return counts.get("done", 0)

    async def wiki_page_count(self, channel_id: str) -> int:
        return sum(1 for p in self.mongo.wiki_pages if p.get("channel_id") == channel_id)

    async def overview_exists(self, channel_id: str) -> bool:
        for p in self.mongo.wiki_pages:
            if p.get("channel_id") == channel_id and p.get("page_type") == "overview":
                return True
        return False

    def llm_call_count(self, provider: str | None = None, kind: str | None = None) -> int:
        return self.llm.call_count(provider=provider, kind=kind)

    def emit_pipeline_event(self, channel_id: str, stage: str, label: str) -> None:
        get_pipeline_events().record(channel_id=channel_id, stage=stage, label=label)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_sim_stack(
    *,
    debounce_seconds: float = 0.05,
    auto_overview_min_facts: int = 5,
    auto_overview_enabled: bool = True,
    overview_language: str = "en",
) -> SimStack:
    """Construct a ready-to-use ``SimStack``.

    Wires:
      * fake mongo populated by tests
      * an ``ExtractionWorker`` whose ``BatchProcessor`` is a
        deterministic mock (no real ADK runner in the simulator)
      * an ``AutoOverviewSubscriber`` whose ``generator`` is a stub that
        appends a fake overview row to the fake mongo
      * a ``_RecordingMaintainer`` registered on the worker
    """
    mongo = _FakeMongo()
    llm = LLMMock()

    # --- worker --------------------------------------------------------
    # The simulator's BatchProcessor is a mock that returns one
    # BatchResult per call: every claimed key transitions to "done"
    # with a synthetic fact_id. Sub-batch errors are configured by tests
    # via ``llm.add_rule`` and translated to BatchBreakdown errors here.
    from beever_atlas.services.batch_processor import (
        BatchBreakdown,
        BatchResult,
    )

    async def fake_process_messages(
        *,
        messages: list[Any],
        channel_id: str,
        channel_name: str,
        sync_job_id: str,
        ingestion_config: Any | None = None,
        use_batch_api: bool = False,
    ) -> BatchResult:
        # Re-derive keys from the NormalizedMessage objects so the
        # BatchBreakdown carries the exact tuples the worker uses.
        all_keys: list[tuple[str, str, str]] = []
        for m in messages:
            all_keys.append(
                (
                    str(getattr(m, "platform", "src")),
                    str(getattr(m, "channel_id", "")),
                    str(getattr(m, "message_id", "")),
                )
            )

        # Simulate one LLM completion + one embedding per ~25-msg
        # sub-batch, fanning faults out to the LLM mock so 429-aware
        # tests can observe the call ordering.
        sub_batch_size = 25
        breakdowns: list[BatchBreakdown] = []
        errors: list[dict[str, Any]] = []
        fact_ids: list[str] = []
        succeeded_keys: list[tuple[str, str, str]] = []
        failed_keys: list[tuple[str, str, str]] = []

        for batch_idx, start in enumerate(range(0, len(all_keys), sub_batch_size)):
            keys_slice = all_keys[start : start + sub_batch_size]
            try:
                # Hit the LLM mock so the fault rules + global 429
                # window apply to the simulated extraction path.
                await llm.acompletion(
                    model="gemini/gemini-2.5-flash",
                    messages=[
                        {
                            "role": "user",
                            "content": f"sub_batch={batch_idx + 1} channel={channel_id}",
                        }
                    ],
                    metadata={
                        "sub_batch": batch_idx + 1,
                        "channel_id": channel_id,
                    },
                )
                # One embedding call per sub-batch (predictable count
                # for Scenario A).
                await llm.aembedding(
                    model="gemini/text-embedding-004",
                    input=[f"k:{k}" for k in keys_slice],
                )
            except Exception as exc:  # noqa: BLE001
                err_msg = f"{type(exc).__name__}: {exc}"
                breakdowns.append(
                    BatchBreakdown(
                        batch_num=batch_idx + 1,
                        error=err_msg,
                        keys=list(keys_slice),
                    )
                )
                errors.append({"batch_index": batch_idx + 1, "error": err_msg})
                failed_keys.extend(keys_slice)
                continue

            # Success path — synthesise one fact_id per message.
            sub_fact_ids = [f"f-{ch}-{mid}" for (_s, ch, mid) in keys_slice]
            fact_ids.extend(sub_fact_ids)
            breakdowns.append(
                BatchBreakdown(
                    batch_num=batch_idx + 1,
                    error=None,
                    keys=list(keys_slice),
                    facts_count=len(sub_fact_ids),
                )
            )
            succeeded_keys.extend(keys_slice)

        # Pipeline-event emission so Scenario A's recent_events assertion
        # has signal. The real BatchProcessor emits these via
        # ``get_pipeline_events().record`` from inside its sub-batch loop.
        try:
            for stage, label in [
                ("preprocess", f"Preprocessed {len(messages)} messages"),
                ("extract_facts", f"Extracted facts from {len(messages)} messages"),
                (
                    "extract_entities",
                    f"Extracted entities from {len(messages)} messages",
                ),
                ("embed", f"Embedded {len(messages)} fact rows"),
                ("persist", f"Persisted {len(succeeded_keys)} extraction rows"),
            ]:
                get_pipeline_events().record(channel_id=channel_id, stage=stage, label=label)
        except Exception:  # noqa: BLE001
            pass

        return BatchResult(
            total_facts=len(fact_ids),
            errors=errors,
            batch_breakdowns=breakdowns,
            fact_ids=fact_ids,
        )

    # The worker constructs its own BatchProcessor by default; we hand it
    # a MagicMock-shaped one whose ``process_messages`` is the fake.
    fake_bp = MagicMock(name="SimBatchProcessor")
    fake_bp.process_messages = AsyncMock(side_effect=fake_process_messages)
    worker = ExtractionWorker(batch_processor=fake_bp)

    # --- patch the global stores access used inside the worker -------
    # The worker pulls ``get_stores().mongodb`` per tick. Rather than
    # globally monkeypatch, we assign the fake mongo onto a stand-in
    # stores object and the test fixture wires it via monkeypatch.
    worker._sim_stores = MagicMock(mongodb=mongo)  # type: ignore[attr-defined]

    # --- auto-overview subscriber -----------------------------------
    overview_pages: list[dict[str, Any]] = []

    async def fake_generator(channel_id: str, language: str) -> None:
        overview_pages.append({"channel_id": channel_id, "language": language})
        # Persist the overview row into the fake mongo so subsequent
        # gates see it.
        mongo.wiki_pages.append(
            {
                "channel_id": channel_id,
                "page_type": "overview",
                "language": language,
                "state": "done",
            }
        )

    async def feature_flag() -> bool:
        return auto_overview_enabled

    async def language_resolver(_channel_id: str) -> str:
        return overview_language

    subscriber = AutoOverviewSubscriber(
        min_facts_threshold=auto_overview_min_facts,
        feature_flag_resolver=feature_flag,
        language_resolver=language_resolver,
        generator=fake_generator,
    )
    subscriber._get_stores = lambda: MagicMock(mongodb=mongo)  # type: ignore[method-assign]

    maintainer_recorder = _RecordingMaintainer()

    # Wire subscriber + maintainer onto the worker's emission point.
    pending_subscriber_tasks: list[asyncio.Task[Any]] = []

    def _on_done(channel_id: str, fact_ids: list[str]) -> None:
        # The maintainer-shape recorder runs synchronously.
        maintainer_recorder.on_extraction_done(channel_id, fact_ids)
        # The auto-overview subscriber is async; spawn a task as the
        # production lifespan does.
        pending_subscriber_tasks.append(
            asyncio.create_task(subscriber.on_extraction_done(channel_id, fact_ids))
        )

    worker.subscribe_extraction_done(_on_done)
    # Give SimStack a hook to drain subscriber tasks after worker ticks.
    worker._sim_pending_tasks = pending_subscriber_tasks  # type: ignore[attr-defined]

    # Also push the burst-collapse signal to the maintainer recorder
    # via debounced semantics — we mimic the "1 maintainer LLM call per
    # debounce window per page" by tracking the count of calls. Tests
    # that need to verify debounce behaviour use the recorder's call
    # count as the proxy.

    return SimStack(
        mongo=mongo,
        llm=llm,
        worker=worker,
        subscriber=subscriber,
        maintainer=maintainer_recorder,
        wiki_pages_added=overview_pages,
    )


__all__ = [
    "FaultRule",
    "LLMMock",
    "SimStack",
    "_ChannelMessage",
    "_FakeMongo",
    "_StubRateLimit",
    "build_sim_stack",
    "make_messages",
]
