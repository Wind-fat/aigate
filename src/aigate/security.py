"""API key lifecycle. Plaintext is shown once; only a SHA-256 digest is stored."""

from __future__ import annotations

import hashlib
import secrets

KEY_PREFIX = "sk-ag-"
PREFIX_DISPLAY_LEN = 14  # "sk-ag-" + 8 chars, enough to identify a key in a list


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext, display_prefix, sha256_digest)."""
    raw = f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"
    return raw, raw[:PREFIX_DISPLAY_LEN], hash_api_key(raw)


def is_well_formed(raw: str) -> bool:
    return raw.startswith(KEY_PREFIX) and len(raw) > len(KEY_PREFIX) + 16