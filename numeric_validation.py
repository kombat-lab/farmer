from __future__ import annotations

import math

# Stored waits use seconds. A whole day is the maximum supported cycle rest.
MAX_WAIT_SECONDS = 24 * 60 * 60.0


def finite_number(value: object) -> float:
    """Convert a numeric setting without allowing NaN, infinity or overflow."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Введите конечное число.")
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError("Введите конечное число.") from error
    if not math.isfinite(number):
        raise ValueError("Введите конечное число.")
    return number


def finite_range(
    minimum: object,
    maximum: object,
    *,
    limit: float = MAX_WAIT_SECONDS,
) -> tuple[float, float]:
    low, high = finite_number(minimum), finite_number(maximum)
    if low < 0 or high < low or high > limit:
        raise ValueError(f"Нужен диапазон от 0 до {limit:g}, максимум не меньше минимума.")
    return low, high


def bounded_integer(value: object, *, maximum: int) -> int:
    """Accept integer settings and their persisted decimal representation."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"Введите целое число от 1 до {maximum}.")
    try:
        number = int(value)
    except ValueError as error:
        raise ValueError(f"Введите целое число от 1 до {maximum}.") from error
    if not 1 <= number <= maximum:
        raise ValueError(f"Введите целое число от 1 до {maximum}.")
    return number
