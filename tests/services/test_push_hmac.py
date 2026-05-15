"""Unit tests for the push-source HMAC verifier (PR-D).

Sister to the loader-token tests — these cover the body-scoped HMAC
that gates ``POST /api/sources/{source_id}/events``. Verifies the
±5-minute skew window, the constant-time signature comparison, and
each failure mode's structured reason.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/push-source-ingestion/``
"""

from __future__ import annotations

import hashlib
import hmac
import time

from beever_atlas.services.push_hmac import (
    hash_secret,
    secrets_equal,
    verify_push_signature,
)


def _sign(secret: str, ts: int, body: bytes) -> str:
    """Build a valid signature header for a (ts, body) pair."""
    sig = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={ts},v1={sig}"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_signature_within_skew_passes() -> None:
    secret = "supersecret"
    body = b'{"events":[{"message_id":"m1"}]}'
    ts = int(time.time())
    header = _sign(secret, ts, body)
    result = verify_push_signature(header, body, secret)
    assert result.ok is True
    assert result.reason is None


# ---------------------------------------------------------------------------
# Skew window
# ---------------------------------------------------------------------------


def test_old_timestamp_rejected_with_skew_exceeded() -> None:
    """Spec scenario: ``Timestamp outside skew window``."""
    secret = "s"
    body = b"hi"
    ts = int(time.time()) - 600  # 10 minutes ago
    header = _sign(secret, ts, body)
    result = verify_push_signature(header, body, secret)
    assert result.ok is False
    assert result.reason == "skew_exceeded"


def test_future_timestamp_rejected_with_skew_exceeded() -> None:
    """Future timestamps must also fall outside the skew window — a
    clock-skewed sender shouldn't be able to extend their replay window."""
    secret = "s"
    body = b"hi"
    ts = int(time.time()) + 600
    header = _sign(secret, ts, body)
    result = verify_push_signature(header, body, secret)
    assert result.ok is False
    assert result.reason == "skew_exceeded"


def test_timestamp_at_boundary_inclusive() -> None:
    """Exactly at the 300s boundary should pass — be liberal on the edge
    so legitimate calls with minor drift don't 401."""
    secret = "s"
    body = b"hi"
    now = int(time.time())
    header = _sign(secret, now - 300, body)
    result = verify_push_signature(header, body, secret, now_unix=now)
    assert result.ok is True


# ---------------------------------------------------------------------------
# Tampering
# ---------------------------------------------------------------------------


def test_modified_body_fails_signature() -> None:
    """Spec scenario: ``Invalid signature``."""
    secret = "s"
    body = b'{"x":1}'
    ts = int(time.time())
    header = _sign(secret, ts, body)
    tampered = b'{"x":2}'
    result = verify_push_signature(header, tampered, secret)
    assert result.ok is False
    assert result.reason == "signature_mismatch"


def test_wrong_secret_fails_signature() -> None:
    body = b"x"
    ts = int(time.time())
    header = _sign("real-secret", ts, body)
    result = verify_push_signature(header, body, "different-secret")
    assert result.ok is False
    assert result.reason == "signature_mismatch"


# ---------------------------------------------------------------------------
# Malformed input
# ---------------------------------------------------------------------------


def test_missing_header_returns_missing_header() -> None:
    """Spec scenario: ``Missing signature header``."""
    result = verify_push_signature("", b"x", "s")
    assert result.ok is False
    assert result.reason == "missing_header"


def test_empty_secret_returns_missing_secret() -> None:
    """Defensive: caller forgot to look up the source's secret."""
    result = verify_push_signature("t=1,v1=abc", b"x", "")
    assert result.ok is False
    assert result.reason == "missing_secret"


def test_malformed_header_no_separator_rejected() -> None:
    result = verify_push_signature("not-a-signature", b"x", "s")
    assert result.ok is False
    assert result.reason == "malformed_header"


def test_malformed_header_no_v1_rejected() -> None:
    result = verify_push_signature("t=1234", b"x", "s")
    assert result.ok is False
    assert result.reason == "malformed_header"


def test_non_integer_timestamp_rejected() -> None:
    result = verify_push_signature("t=abc,v1=def", b"x", "s")
    assert result.ok is False
    assert result.reason == "malformed_header"


def test_non_hex_signature_rejected() -> None:
    """``v1=...`` must be valid hex — a bytes.fromhex error is reason
    ``malformed_signature`` not ``signature_mismatch`` so operators
    can distinguish encoding bugs from actual tampering."""
    ts = int(time.time())
    result = verify_push_signature(f"t={ts},v1=zzz_not_hex", b"x", "s")
    assert result.ok is False
    assert result.reason == "malformed_signature"


# ---------------------------------------------------------------------------
# Secret hashing helpers
# ---------------------------------------------------------------------------


def test_hash_secret_is_deterministic() -> None:
    assert hash_secret("abc") == hash_secret("abc")
    assert hash_secret("abc") != hash_secret("abd")


def test_secrets_equal_constant_time_compare() -> None:
    secret = "rotated-key"
    stored = hash_secret(secret)
    assert secrets_equal(secret, stored) is True
    assert secrets_equal("wrong-key", stored) is False
