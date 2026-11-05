"""API key generation and hashing.

Format:  tg_<48 hex chars>   (192 bits of entropy from secrets.token_hex)
Storage: only the SHA-256 hex digest (64 chars) and a non-secret prefix.

Why SHA-256 and not bcrypt/argon2: those exist to slow down brute force of
LOW-entropy human passwords. A 192-bit random key has no dictionary to
guess — a fast hash is correct here, and it keeps authentication to a
single indexed lookup per request.
"""

from __future__ import annotations

import hashlib
import secrets

KEY_NAMESPACE = "tg"


def generate_api_key() -> tuple[str, str, str]:
    """Create a new API key.

    Returns (plaintext_key, sha256_hex, display_prefix).
    The plaintext is shown to the operator ONCE and never stored.
    """
    plaintext = f"{KEY_NAMESPACE}_{secrets.token_hex(24)}"
    return plaintext, hash_api_key(plaintext), display_prefix(plaintext)


def hash_api_key(plaintext: str) -> str:
    """SHA-256 hex digest of the key. Deterministic → index lookup on auth."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def display_prefix(plaintext: str) -> str:
    """First 11 chars ('tg_' + 8) — safe to show in logs/dashboards."""
    return plaintext[:11]
