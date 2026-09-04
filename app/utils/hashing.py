"""Stable hashing used for cache keys and de-duplication.

The hashes are deterministic across processes (unlike :func:`hash`) so a cached
search from an earlier run is still addressable, and they are one-way, so
storing them does not store the query itself.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable

HASH_LENGTH = 32


def normalize_token(value: str | None) -> str:
    """Casefold, strip and collapse whitespace; ``ё`` is folded to ``е``."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    text = text.replace("ё", "е")
    return " ".join(text.split())


def stable_hash(*parts: str | None) -> str:
    """Hash of normalized parts, truncated to a comfortable storage width."""
    payload = "|".join(normalize_token(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def stable_hash_of(parts: Iterable[str | None]) -> str:
    return stable_hash(*parts)
