from __future__ import annotations

import re


def normalize_text(value: str) -> str:
    """Return a canonical form for human-facing game and button text."""

    if not isinstance(value, str):
        raise ValueError("Text normalization requires a string")
    normalized = " ".join(value.casefold().strip().split())
    normalized = re.sub(
        r"^[^\wа-яё]+",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized.strip()
