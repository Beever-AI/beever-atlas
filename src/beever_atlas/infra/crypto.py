"""AES-256-GCM envelope encryption for credential storage."""

from __future__ import annotations

import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _get_master_key() -> bytes:
    """Load and decode the master key from CREDENTIAL_MASTER_KEY env var.

    The key must be a 64-character hex string (32 bytes / 256 bits).
    Raises RuntimeError if the key is missing or invalid.
    """
    from beever_atlas.infra.config import get_settings

    raw = get_settings().credential_master_key
    if not raw:
        raise RuntimeError(
            "CREDENTIAL_MASTER_KEY is not set. "
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    try:
        key = bytes.fromhex(raw)
    except ValueError as e:
        raise RuntimeError(f"CREDENTIAL_MASTER_KEY must be a 64-character hex string: {e}") from e
    if len(key) != 32:
        raise RuntimeError(
            f"CREDENTIAL_MASTER_KEY must be 32 bytes (64 hex chars), got {len(key)} bytes."
        )
    return key


def encrypt_credentials(plaintext: dict) -> tuple[bytes, bytes, bytes]:
    """Encrypt a credentials dict with AES-256-GCM.

    Returns:
        (ciphertext, iv, tag) — all bytes.
        The tag is appended by AESGCM to the ciphertext; we split it out
        so each field maps clearly to the PlatformConnection schema.
    """
    key = _get_master_key()
    iv = os.urandom(12)  # 96-bit nonce recommended for GCM
    aesgcm = AESGCM(key)
    data = json.dumps(plaintext, separators=(",", ":")).encode()
    # AESGCM.encrypt returns ciphertext + 16-byte tag concatenated
    encrypted = aesgcm.encrypt(iv, data, None)
    ciphertext = encrypted[:-16]
    tag = encrypted[-16:]
    return ciphertext, iv, tag


def decrypt_credentials(ciphertext: bytes, iv: bytes, tag: bytes) -> dict:
    """Decrypt AES-256-GCM ciphertext back to a credentials dict.

    Args:
        ciphertext: Encrypted payload (without tag).
        iv: 12-byte nonce used during encryption.
        tag: 16-byte authentication tag.

    Returns:
        Original credentials dict.

    Raises:
        cryptography.exceptions.InvalidTag: If the ciphertext or tag is corrupted.
    """
    key = _get_master_key()
    aesgcm = AESGCM(key)
    # Reconstitute the AESGCM-expected format: ciphertext + tag
    combined = ciphertext + tag
    data = aesgcm.decrypt(iv, combined, None)
    return json.loads(data.decode())
