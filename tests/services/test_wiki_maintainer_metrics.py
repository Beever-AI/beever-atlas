"""Unit tests for the WikiMaintainer in-memory counters
(close-the-soak-loop §4 — the synchronous slice consumed by the admin
metrics endpoint).

Tests run directly against ``WikiMaintainer._in_memory_metrics_snapshot``
so they do not depend on FastAPI / Mongo setup. Endpoint-level tests are
in ``tests/api/test_admin_wiki_maintainer_metrics.py``.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

from beever_atlas.services.wiki_maintainer import (
    WikiMaintainer,
    _page_kind_from_id,
)


def _maintainer() -> WikiMaintainer:
    return WikiMaintainer(page_store=AsyncMock())


def test_page_kind_from_id_dispatches_role_pages():
    assert _page_kind_from_id("topic:auth") == "topic"
    assert _page_kind_from_id("entity:alice") == "entity"
    assert _page_kind_from_id("decisions") == "decisions"
    assert _page_kind_from_id("faq") == "faq"
    assert _page_kind_from_id("action-items") == "action_items"
    assert _page_kind_from_id("") == "other"
    assert _page_kind_from_id("unknown:weird") == "other"


def test_idle_maintainer_metrics_zero():
    snap = _maintainer()._in_memory_metrics_snapshot()
    assert snap["apply_update_count_5min"] == 0
    assert snap["apply_update_count_15min"] == 0
    assert snap["apply_update_count_60min"] == 0
    assert snap["mark_dirty_count_5min"] == 0
    assert snap["apply_update_failures"] == []
    assert snap["rewrite_count_by_page_kind"] == {
        "topic": 0,
        "entity": 0,
        "decisions": 0,
        "faq": 0,
        "action_items": 0,
    }


def test_apply_update_records_landed_in_correct_bucket():
    m = _maintainer()
    m._record_apply_update_success("topic:auth")
    m._record_apply_update_success("topic:billing")
    m._record_apply_update_success("decisions")
    m._record_apply_update_success("faq")
    m._record_apply_update_success("action-items")
    m._record_apply_update_success("entity:alice")
    snap = m._in_memory_metrics_snapshot()
    assert snap["apply_update_count_5min"] == 6
    by_kind = snap["rewrite_count_by_page_kind"]
    assert by_kind["topic"] == 2
    assert by_kind["entity"] == 1
    assert by_kind["decisions"] == 1
    assert by_kind["faq"] == 1
    assert by_kind["action_items"] == 1


def test_failures_capped_oldest_dropped():
    m = _maintainer()
    for i in range(15):
        m._record_apply_update_failure("C1", f"topic:{i}", RuntimeError(f"e{i}"))
    snap = m._in_memory_metrics_snapshot()
    assert len(snap["apply_update_failures"]) == 10
    # Oldest five (0..4) dropped — first remaining is index 5.
    page_ids = [f["page_id"] for f in snap["apply_update_failures"]]
    assert page_ids[0] == "topic:5"
    assert page_ids[-1] == "topic:14"


def test_rolling_window_trims_old_entries():
    m = _maintainer()
    fake_now = time.monotonic()
    # Older than 60 min → trimmed on next snapshot.
    m._apply_update_records.append((fake_now - 4000.0, "topic"))
    # Within 5 min — counts in 5/15/60.
    m._apply_update_records.append((fake_now, "entity"))
    # Within 30 min — counts in 60 only (not 5 or 15 since 30 < 60 but
    # > 15? Actually 30 min = 1800s > 900s window, so within 60 only).
    m._apply_update_records.append((fake_now - 1800.0, "decisions"))

    snap = m._in_memory_metrics_snapshot()
    assert snap["apply_update_count_60min"] == 2  # not the 4000s-old one
    assert snap["apply_update_count_15min"] == 1  # only ``fake_now``
    assert snap["apply_update_count_5min"] == 1


def test_mark_dirty_records_count_within_5min():
    m = _maintainer()
    m._record_mark_dirty(3)
    m._record_mark_dirty(2)
    snap = m._in_memory_metrics_snapshot()
    assert snap["mark_dirty_count_5min"] == 5


def test_mark_dirty_zero_count_noop():
    m = _maintainer()
    m._record_mark_dirty(0)
    snap = m._in_memory_metrics_snapshot()
    assert snap["mark_dirty_count_5min"] == 0
