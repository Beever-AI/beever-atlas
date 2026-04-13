"""Backend-neutral error hierarchy for GraphStore implementations.

Callers of GraphStore should catch these exceptions instead of raw
driver-specific exceptions (``neo4j.exceptions.*``, nebula3 tuples, etc.).
Each backend wraps its native errors via :func:`_translate_errors`.
"""

from __future__ import annotations


class GraphStoreError(Exception):
    """Base class for all GraphStore-level errors."""


class GraphNotFound(GraphStoreError):
    """Point-query returned no result where one was expected."""


class GraphConflict(GraphStoreError):
    """Constraint violation or authorisation refusal from the backend."""


class GraphBackendUnavailable(GraphStoreError):
    """Backend is unreachable, session expired, or request is transient."""


__all__ = [
    "GraphStoreError",
    "GraphNotFound",
    "GraphConflict",
    "GraphBackendUnavailable",
]
