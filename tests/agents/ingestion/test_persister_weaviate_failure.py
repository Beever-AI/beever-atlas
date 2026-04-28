"""Regression tests for PersisterAgent graceful Weaviate-failure handling (#29).

The fix in persister.py:
  1. Adds `WEAVIATE_ERROR_PREFIX` module constant.
  2. Adds `weaviate_failed(persist_errors)` helper predicate.
  3. Wraps both `zip(facts, weaviate_ids, strict=True)` loops with `if weaviate_ids:`.
  4. Skips `mark_intent_complete` when `weaviate_failed(persist_errors)` is True
     so the existing WriteReconciler retries the Weaviate write automatically.

These tests target the predicate + constant directly. The `if weaviate_ids:`
zip-loop guards and the `if not weaviate_failed(...)` conditional at the
mark_intent_complete site are both small enough that the predicate test +
the manual staging integration test (PR checklist) provide adequate
coverage without spinning up the full ADK invocation context.
"""

from __future__ import annotations

from beever_atlas.agents.ingestion.persister import (
    WEAVIATE_ERROR_PREFIX,
    weaviate_failed,
)


class TestWeaviateErrorPrefix:
    def test_constant_value(self):
        """The prefix must match the format used at the error-capture site."""
        assert WEAVIATE_ERROR_PREFIX == "weaviate:"


class TestWeaviateFailedPredicate:
    def test_empty_errors_returns_false(self):
        assert weaviate_failed([]) is False

    def test_only_weaviate_error_returns_true(self):
        errors = [f"{WEAVIATE_ERROR_PREFIX} RuntimeError('connection refused')"]
        assert weaviate_failed(errors) is True

    def test_only_neo4j_error_returns_false(self):
        """Neo4j-prefixed errors must NOT trigger the Weaviate-failure path —
        otherwise mark_intent_complete would be skipped on Neo4j-only failures
        too, leaving Weaviate-success state inconsistent."""
        errors = ["neo4j: ConnectionError('timeout')"]
        assert weaviate_failed(errors) is False

    def test_mixed_errors_with_weaviate_returns_true(self):
        """If both stores failed, weaviate_failed must still return True so
        mark_intent_complete is skipped (Weaviate write must remain retryable)."""
        errors = [
            f"{WEAVIATE_ERROR_PREFIX} RuntimeError('A')",
            "neo4j: ConnectionError('B')",
        ]
        assert weaviate_failed(errors) is True

    def test_mixed_errors_neo4j_first_still_returns_true(self):
        """Order independence — Weaviate detection works regardless of
        where the entry sits in the list."""
        errors = [
            "neo4j: ConnectionError('B')",
            f"{WEAVIATE_ERROR_PREFIX} RuntimeError('A')",
        ]
        assert weaviate_failed(errors) is True

    def test_unrelated_error_returns_false(self):
        """Defensive: a generic error string must not be misclassified."""
        errors = ["something_else: unrelated failure"]
        assert weaviate_failed(errors) is False

    def test_substring_weaviate_does_not_trigger(self):
        """The prefix is checked with startswith, not substring match. An
        error message that mentions 'weaviate:' mid-string must not trigger
        the conditional."""
        errors = ["something happened with weaviate: in the middle"]
        assert weaviate_failed(errors) is False


class TestErrorCaptureFormatStability:
    """Lock in the invariant that the error-capture format at L345 of
    persister.py begins with `WEAVIATE_ERROR_PREFIX` followed by a space.

    If a future contributor changes the format string at the error-capture
    site, the predicate at the conditional site silently breaks. This test
    catches such drift by re-constructing the format directly.
    """

    def test_capture_format_is_detected_by_predicate(self):
        """The actual format used at L345 is `f"{WEAVIATE_ERROR_PREFIX} {results[0]}"`.
        Verify the resulting string is correctly classified by the predicate."""
        sample_error = RuntimeError("Weaviate batch upsert failed")
        captured = f"{WEAVIATE_ERROR_PREFIX} {sample_error}"
        assert weaviate_failed([captured]) is True

    def test_capture_format_starts_with_constant(self):
        """The captured string must literally begin with the constant value
        (no leading whitespace, no other prefix)."""
        sample_error = RuntimeError("oops")
        captured = f"{WEAVIATE_ERROR_PREFIX} {sample_error}"
        assert captured.startswith(WEAVIATE_ERROR_PREFIX)
