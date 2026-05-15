"""Tests for the WikiMaintainer drift-comparator wiring (close-the-soak-loop §1).

Verifies:
  * Flag OFF — comparator NOT scheduled.
  * Flag ON — comparator scheduled exactly once per ``apply_update`` call.
  * Per-(channel, page) rate limiter (60s default).
  * ``WikiBuilder`` regenerate-factory failure does not propagate to
    ``apply_update``'s caller — the maintainer's primary path stays clean.
  * The ``wiki_drift_rate_limited`` structured log line fires when a call
    is skipped.

Convention: ``pyproject.toml`` sets ``asyncio_mode = "auto"`` so no
``@pytest.mark.asyncio`` decorators are required.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from beever_atlas.models.persistence import WikiPage, WikiPageSection
from beever_atlas.services import wiki_maintainer as wm_mod
from beever_atlas.services.wiki_maintainer import WikiMaintainer


# ---------------------------------------------------------------------------
# Settings + comparator stubs
# ---------------------------------------------------------------------------


def _patch_settings(monkeypatch, *, drift_ab: bool, rate_limit: int = 60) -> None:
    fake = SimpleNamespace(
        wiki_drift_ab=drift_ab,
        wiki_drift_ab_rate_limit_seconds=rate_limit,
        # Drift A/B tests exercise the legacy single-prompt apply_update
        # path; the per-kind dispatcher must stay OFF for them.
        wiki_llm_native_redesign=False,
    )
    monkeypatch.setattr("beever_atlas.infra.config.get_settings", lambda: fake)


def _patch_compare_capture(monkeypatch) -> list[dict[str, Any]]:
    """Replace ``compare_apply_update_vs_regenerate`` with a recorder so we
    can assert exactly when / how often the comparator was scheduled."""
    seen: list[dict[str, Any]] = []

    async def _fake_compare(*, channel_id, page_id, incremental_factory, regenerate_factory):
        # Resolve both factories so production-shaped failures (e.g. the
        # regenerate factory raising) surface here without propagating.
        try:
            inc = await incremental_factory()
        except Exception as exc:  # noqa: BLE001
            seen.append({"channel_id": channel_id, "page_id": page_id, "inc_err": str(exc)})
            return None
        try:
            regen = await regenerate_factory()
        except Exception as exc:  # noqa: BLE001
            seen.append(
                {
                    "channel_id": channel_id,
                    "page_id": page_id,
                    "regen_err": str(exc),
                }
            )
            return None
        seen.append(
            {
                "channel_id": channel_id,
                "page_id": page_id,
                "inc_title": inc.title if inc else None,
                "regen_title": regen.title if regen else None,
            }
        )
        return None

    monkeypatch.setattr(
        "beever_atlas.services.wiki_drift_comparator.compare_apply_update_vs_regenerate",
        _fake_compare,
    )
    return seen


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_page(page_id: str = "topic:auth") -> WikiPage:
    return WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id=page_id,
        title="Auth",
        slug=page_id.replace(":", "-"),
        sections=[WikiPageSection(id="overview", title="Overview", content_md="OIDC")],
    )


@pytest.fixture
def maintainer_with_save():
    """Build a maintainer whose ``apply_update`` always reaches the success
    branch by stubbing the LLM call + the page store."""
    page_store = AsyncMock()
    page_store.get_page = AsyncMock(return_value=None)  # first-touch path
    page_store.save_page = AsyncMock(return_value=None)
    m = WikiMaintainer(page_store=page_store)
    # Inject deterministic LLM output that yields one parsable section.
    m._invoke_apply_update_llm = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            '{"affected_sections":[{"id":"overview","title":"Overview",'
            '"content_md":"new"}],"reason":"test"}'
        )
    )
    # Inject a fact loader that returns one fake fact for any id list.
    m._load_facts = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {
                "id": "f1",
                "cluster_id": "auth",
                "entity_tags": [],
                "fact_type": "observation",
                "memory_text": "x",
                "source_message_id": "m1",
            }
        ]
    )
    # Avoid the cluster/entity title lookups touching the stores singleton.
    m._resolve_first_touch_title = AsyncMock(return_value="Auth")  # type: ignore[method-assign]
    return m


# ---------------------------------------------------------------------------
# 1.8 — Flag OFF means no comparator
# ---------------------------------------------------------------------------


async def test_flag_off_does_not_schedule_comparator(monkeypatch, maintainer_with_save):
    _patch_settings(monkeypatch, drift_ab=False)
    seen = _patch_compare_capture(monkeypatch)
    ok = await maintainer_with_save.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    assert ok is True
    assert seen == []


# ---------------------------------------------------------------------------
# 1.9 — Flag ON schedules exactly once
# ---------------------------------------------------------------------------


async def test_flag_on_schedules_comparator_once(monkeypatch, maintainer_with_save):
    _patch_settings(monkeypatch, drift_ab=True)
    seen = _patch_compare_capture(monkeypatch)
    # Stub the regenerate factory to a known page so the captured call has
    # both sides resolved.
    monkeypatch.setattr(
        WikiMaintainer,
        "_make_regenerate_factory",
        lambda self, channel_id, page_id, target_lang: lambda: _async_value(_make_page(page_id)),
    )
    ok = await maintainer_with_save.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    assert ok is True
    # Yield to let the create_task callback run.
    await _drain_pending_tasks()
    assert len(seen) == 1
    assert seen[0]["channel_id"] == "C1"
    assert seen[0]["page_id"] == "topic:auth"


# ---------------------------------------------------------------------------
# 1.10 — Rate limit blocks rapid second call
# ---------------------------------------------------------------------------


async def test_rate_limit_blocks_second_call_within_window(monkeypatch, maintainer_with_save):
    _patch_settings(monkeypatch, drift_ab=True, rate_limit=60)
    seen = _patch_compare_capture(monkeypatch)
    monkeypatch.setattr(
        WikiMaintainer,
        "_make_regenerate_factory",
        lambda self, channel_id, page_id, target_lang: lambda: _async_value(_make_page(page_id)),
    )

    await maintainer_with_save.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    await _drain_pending_tasks()
    # Reset last_facts_seen so the second apply_update isn't a no-op.
    maintainer_with_save._page_store.get_page = AsyncMock(return_value=None)
    await maintainer_with_save.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f2"],
        target_lang="en",
    )
    await _drain_pending_tasks()
    # Only the first call scheduled the comparator.
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# 1.11 — Rate limit elapses → second call schedules
# ---------------------------------------------------------------------------


async def test_rate_limit_window_elapsed_reschedules(monkeypatch, maintainer_with_save):
    _patch_settings(monkeypatch, drift_ab=True, rate_limit=60)
    seen = _patch_compare_capture(monkeypatch)
    monkeypatch.setattr(
        WikiMaintainer,
        "_make_regenerate_factory",
        lambda self, channel_id, page_id, target_lang: lambda: _async_value(_make_page(page_id)),
    )

    await maintainer_with_save.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    await _drain_pending_tasks()
    # Move the recorded timestamp 70s into the past.
    key = ("C1", "topic:auth")
    last = maintainer_with_save._drift_compare_last_run.get(key)
    assert last is not None
    maintainer_with_save._drift_compare_last_run[key] = last - 70.0

    maintainer_with_save._page_store.get_page = AsyncMock(return_value=None)
    await maintainer_with_save.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f2"],
        target_lang="en",
    )
    await _drain_pending_tasks()
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# 1.12 — Regenerate factory raising does not crash apply_update
# ---------------------------------------------------------------------------


async def test_regenerate_factory_failure_does_not_propagate(monkeypatch, maintainer_with_save):
    _patch_settings(monkeypatch, drift_ab=True)
    seen = _patch_compare_capture(monkeypatch)
    # Regenerate factory always raises.

    def _raising_factory(self, channel_id, page_id, target_lang):
        async def _bad():
            raise RuntimeError("regen kaboom")

        return _bad

    monkeypatch.setattr(WikiMaintainer, "_make_regenerate_factory", _raising_factory)

    ok = await maintainer_with_save.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    await _drain_pending_tasks()
    assert ok is True
    # The patched comparator captured the regen error rather than crashing.
    assert seen and "regen_err" in seen[0]
    # save_page was still invoked (page state landed normally).
    maintainer_with_save._page_store.save_page.assert_awaited()


# ---------------------------------------------------------------------------
# 1.13 — wiki_drift_rate_limited log emitted
# ---------------------------------------------------------------------------


async def test_rate_limited_emits_structured_log(monkeypatch, maintainer_with_save):
    _patch_settings(monkeypatch, drift_ab=True, rate_limit=60)
    _patch_compare_capture(monkeypatch)
    monkeypatch.setattr(
        WikiMaintainer,
        "_make_regenerate_factory",
        lambda self, channel_id, page_id, target_lang: lambda: _async_value(_make_page(page_id)),
    )

    # The maintainer module's logger uses a custom JSON handler that
    # bypasses ``caplog``; intercept ``logger.info`` directly so the test
    # reads the same call site the runtime emits.
    seen: list[str] = []
    real_info = wm_mod.logger.info

    def _capture(msg, *args, **kwargs):
        try:
            seen.append(msg % args if args else str(msg))
        except TypeError:
            seen.append(str(msg))
        real_info(msg, *args, **kwargs)

    monkeypatch.setattr(wm_mod.logger, "info", _capture)

    # First call schedules + records the timestamp.
    await maintainer_with_save.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f1"],
        target_lang="en",
    )
    await _drain_pending_tasks()

    maintainer_with_save._page_store.get_page = AsyncMock(return_value=None)
    await maintainer_with_save.apply_update(
        channel_id="C1",
        page_id="topic:auth",
        new_fact_ids=["f2"],
        target_lang="en",
    )
    await _drain_pending_tasks()
    assert any("event=wiki_drift_rate_limited" in m for m in seen)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _async_value(value):
    return value


async def _drain_pending_tasks() -> None:
    """Yield until all pending non-self tasks settle. The maintainer's
    fire-and-forget comparator scheduling uses ``loop.create_task``; tests
    need to let those tasks run before asserting on side-effects."""
    for _ in range(3):
        await asyncio.sleep(0)
