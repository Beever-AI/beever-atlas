"""Provider outage circuit breaker — superseded by PR-C.

The module-level globals these tests exercised
(``_consecutive_503_count``, ``_consecutive_503_lock``) were removed in
PR-C of the OSS pipeline + wiki redesign. The breaker is now an
injectable :class:`CircuitBreaker` class with proper state transitions
and full unit-test coverage at:

  * ``tests/services/test_circuit_breaker.py`` (16 tests):
    state transitions, cooldown / half-open recovery, concurrency
    (only one probe at a time), structured log emission, singleton
    wiring, read-only is_open() accessor.

  * ``tests/llm/test_provider_failover_seam.py`` (6 tests):
    the failover seam in LLMProvider.resolve_model when the breaker
    is open AND ``llm.provider._FAILOVER_ENABLED`` is True (enterprise
    enablement path; OSS default keeps the seam disabled — the env
    var that previously gated this was retired in commit 2aaaf1e).

This file is retained as a deprecation marker so a future reviewer
sees that the old AC #5 / #6 / #10 / #11 tests were intentionally
replaced rather than dropped. The integrated end-to-end path (sync
runner → BatchProcessor → breaker → ProviderOutageError) is exercised
by the spec close-out E2E test in section 10 of tasks.md, deferred to
the cross-cutting verification PR.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(
    reason=(
        "PR-C of oss-pipeline-and-wiki-redesign removed the module-level "
        "_consecutive_503_count / _consecutive_503_lock globals these tests "
        "depended on. State-transition coverage moved to "
        "tests/services/test_circuit_breaker.py; failover seam coverage to "
        "tests/llm/test_provider_failover_seam.py. The integrated path is "
        "covered by the spec close-out E2E (tasks.md section 10)."
    )
)
def test_legacy_breaker_tests_superseded_by_pr_c() -> None:
    """Placeholder — see module docstring for migration pointers."""
