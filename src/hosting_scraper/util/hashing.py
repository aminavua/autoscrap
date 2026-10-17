from __future__ import annotations

import hashlib
import re


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", errors="replace"))


def short_hash(value: str, length: int = 12) -> str:
    return sha256_text(value)[:length]


def slugify(value: str, *, max_length: int = 80) -> str:
    """Turn arbitrary text into a filesystem/URL-safe slug."""
    value = value.strip().lower()
    value = re.sub(r"https?://", "", value)
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-._")
    return (value or "item")[:max_length]
