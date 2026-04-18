"""Typed domain exceptions for the framework-neutral capabilities layer.

These exceptions are raised by ``src/beever_atlas/capabilities/`` functions
instead of FastAPI ``HTTPException`` so both the ADK-tool layer and the
forthcoming MCP layer can translate them independently:

- REST endpoints translate them into ``HTTPException`` with the
  appropriate HTTP status code (e.g. 403 / 429 / 404).
- MCP tool wrappers translate them into structured JSON errors like
  ``{"error": "channel_access_denied"}``.

Keeping this translation at the boundary (rather than in the capability)
means the business logic remains framework-neutral and can be exercised
in unit tests without any web-framework machinery.
"""

from __future__ import annotations


class CapabilityError(Exception):
    """Base class for all capability-layer domain errors."""


class ChannelAccessDenied(CapabilityError):
    """The principal may not access the requested channel."""

    def __init__(self, channel_id: str) -> None:
        super().__init__(f"Channel access denied: {channel_id!r}")
        self.channel_id = channel_id


class ConnectionAccessDenied(CapabilityError):
    """The principal may not access the requested platform connection."""

    def __init__(self, connection_id: str) -> None:
        super().__init__(f"Connection access denied: {connection_id!r}")
        self.connection_id = connection_id


class CooldownActive(CapabilityError):
    """An operation is currently in cooldown and must retry after ``retry_after_seconds``."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            f"Cooldown active; retry after {retry_after_seconds}s"
        )
        self.retry_after_seconds = int(retry_after_seconds)


class JobNotFound(CapabilityError):
    """The job id does not exist or is not visible to this principal.

    Intentionally raised for "job exists but is owned by someone else"
    so the MCP layer can return ``job_not_found`` without leaking that
    the id is valid for another principal.
    """

    def __init__(self, job_id: str) -> None:
        super().__init__(f"Job not found: {job_id!r}")
        self.job_id = job_id


__all__ = [
    "CapabilityError",
    "ChannelAccessDenied",
    "ConnectionAccessDenied",
    "CooldownActive",
    "JobNotFound",
]
