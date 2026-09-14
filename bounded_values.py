from __future__ import annotations

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def require_int64(value: object, name: str, *, minimum: int = INT64_MIN) -> int:
    """Validate an integer crossing a signed 64-bit persistence boundary."""
    if type(value) is not int or not minimum <= value <= INT64_MAX:
        raise ValueError(f"{name} must be an integer in [{minimum}, {INT64_MAX}]")
    return value
