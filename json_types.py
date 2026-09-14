from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import TypeAlias

from bounded_values import require_int64

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


def _normalized_json(value: object) -> JsonValue:
    if value is None or isinstance(value, str) or type(value) is bool:
        return value
    if type(value) is int:
        return require_int64(value, "JSON integer")
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_normalized_json(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            result[key] = _normalized_json(item)
        return result
    raise ValueError("Expected a finite JSON value")


def canonical_json_value(value: object) -> str:
    """Validate and detach a JSON value before crossing a persistence boundary."""
    return json.dumps(
        _normalized_json(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_object(payload: Mapping[str, JsonValue]) -> str:
    """Validate a JSON object and detach it into deterministic finite JSON text."""
    if not isinstance(payload, Mapping):
        raise ValueError("Expected a JSON object")
    return canonical_json_value(payload)
