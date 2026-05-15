"""HMAC-SHA256 verifier for push-source ingest events.

Validates the ``X-Beever-Signature`` header on
``POST /api/sources/{source_id}/events``. Sister to the loader-token
verifier in ``infra/loader_token.py`` — that one is path-scoped and
read-only; this one is body-scoped and mutating, so the contract is
intentionally separate.

Header format: ``X-Beever-Signature: t=<unix_ts>,v1=<hex>``
  * ``t`` — unix timestamp at sign time (signed-message component)
  * ``v1`` — hex digest of HMAC-SHA256 over ``f"{t}.{body}"``

The 5-minute timestamp skew window mitigates replay attacks. The
caller adds an ``X-Beever-Idempotency-Key`` 24h replay cache for
within-skew replays.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/push-source-ingestion/``
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)


# 5 minutes — same window used across the design (D8).
DEFAULT_SKEW_SECONDS: Final[int] = 300


@dataclass(frozen=True)
class HMACVerifyResult:
    """Outcome of one verification attempt — caller uses ``.ok`` to gate."""

    ok: bool
    reason: str | None = None


def _parse_signature_header(header: str) -> tuple[int | None, str | None]:
    """Parse ``t=<int>,v1=<hex>`` into (timestamp, hex). Returns (None, None)
    on any structural fault — caller decides which 401 reason to log."""
    if not header:
        return None, None
    ts: int | None = None
    sig: str | None = None
    for part in header.split(","):
        if "=" not in part:
            continue
        k, _, v = part.strip().partition("=")
        k = k.strip()
        v = v.strip()
        if k == "t":
            try:
                ts = int(v)
            except ValueError:
                return None, None
        elif k == "v1":
            sig = v
    return ts, sig


def verify_push_signature(
    signature_header: str,
    body: bytes,
    secret: str,
    *,
    skew_seconds: int = DEFAULT_SKEW_SECONDS,
    now_unix: int | None = None,
) -> HMACVerifyResult:
    """Verify a push-source signature.

    ``body`` is the request body bytes — passed in by the FastAPI
    dependency at the boundary so the verifier is HTTP-stack agnostic
    and unit-testable. ``secret`` is the per-source HMAC key looked up
    from the ``external_sources`` collection.

    Returns an ``HMACVerifyResult`` with ``ok=True`` only when:
      * header parses cleanly
      * timestamp is within ``±skew_seconds`` of ``now_unix``
      * signature matches HMAC-SHA256(secret, f"{ts}.{body}")

    Failure cases return ``ok=False`` with a structured ``reason`` for
    logging — callers should NOT echo the reason in the HTTP response,
    since that would help an attacker triangulate the failure mode.
    """
    if not signature_header:
        return HMACVerifyResult(ok=False, reason="missing_header")
    if not secret:
        return HMACVerifyResult(ok=False, reason="missing_secret")

    ts, sig = _parse_signature_header(signature_header)
    if ts is None or sig is None:
        return HMACVerifyResult(ok=False, reason="malformed_header")

    current = int(time.time()) if now_unix is None else now_unix
    if abs(current - ts) > skew_seconds:
        return HMACVerifyResult(ok=False, reason="skew_exceeded")

    expected_bytes = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode() + body,
        hashlib.sha256,
    ).digest()
    try:
        provided_bytes = bytes.fromhex(sig)
    except ValueError:
        return HMACVerifyResult(ok=False, reason="malformed_signature")

    if not hmac.compare_digest(expected_bytes, provided_bytes):
        return HMACVerifyResult(ok=False, reason="signature_mismatch")

    return HMACVerifyResult(ok=True)


def hash_secret(secret: str) -> str:
    """Hash a per-source secret for ``external_sources`` storage.

    The wire format secret is plaintext (the source signs with it). We
    store only ``sha256(secret)`` so a Mongo dump doesn't leak active
    signing keys. Verification re-hashes the candidate and compares.
    Salt is intentionally omitted — secrets are 32+ bytes of random
    entropy, so HMAC's brute-force budget is already astronomical.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def secrets_equal(provided: str, stored_hash: str) -> bool:
    """Constant-time comparison of a candidate secret against its hash."""
    return hmac.compare_digest(hash_secret(provided), stored_hash)
