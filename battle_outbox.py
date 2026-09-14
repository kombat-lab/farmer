from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from bounded_values import require_int64
from json_types import JsonValue, canonical_json_object


def _nonblank(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class BattleEvent:
    """Immutable intent for idempotent consumers; this API provides no delivery lease.

    External side effects need a single consumer or separate claim/deduplication.
    Valid requested intents are durable facts committed with their battle.
    """

    namespace: str
    idempotency_key: str
    event_type: str
    schema_version: int
    payload_json: str

    def __post_init__(self) -> None:
        for field in ("namespace", "idempotency_key", "event_type"):
            object.__setattr__(self, field, _nonblank(getattr(self, field), field))
        require_int64(self.schema_version, "schema_version", minimum=1)
        if not isinstance(self.payload_json, str):
            raise ValueError("payload_json must be serialized JSON")
        payload: object = json.loads(self.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("event payload must be a JSON object")
        canonical = canonical_json_object(cast(Mapping[str, JsonValue], payload))
        object.__setattr__(self, "payload_json", canonical)

    @classmethod
    def from_payload(
        cls,
        *,
        namespace: str,
        idempotency_key: str,
        event_type: str,
        schema_version: int,
        payload: Mapping[str, JsonValue],
    ) -> BattleEvent:
        return cls(
            namespace, idempotency_key, event_type, schema_version, canonical_json_object(payload)
        )

    def decoded_payload(self) -> dict[str, JsonValue]:
        """Return a detached mutable copy, preserving the immutable envelope."""
        return cast(dict[str, JsonValue], json.loads(self.payload_json))


@dataclass(frozen=True, slots=True)
class BattleOutboxEnvelope:
    id: int
    battle_id: int
    created_at: str
    event: BattleEvent
    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        require_int64(self.id, "id", minimum=1)
        require_int64(self.battle_id, "battle_id", minimum=1)
        require_int64(self.attempts, "attempts", minimum=0)
        if not isinstance(self.event, BattleEvent):
            raise ValueError("event must be an immutable BattleEvent")
        if self.last_error is not None and not isinstance(self.last_error, str):
            raise ValueError("last_error must be a string or None")
        for field in ("created_at", "next_attempt_at"):
            raw = getattr(self, field)
            if field == "next_attempt_at" and raw is None:
                continue
            if not isinstance(raw, str):
                raise ValueError(f"{field} must be an aware ISO timestamp")
            moment = datetime.fromisoformat(raw)
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError(f"{field} must be an aware ISO timestamp")
            object.__setattr__(self, field, moment.astimezone(UTC).isoformat())


@dataclass(frozen=True, slots=True)
class InvalidBattleOutboxEntry:
    """An undecodable row, retaining its exact SQLite identity for quarantine.

    No corrupt value is coerced into a valid envelope. SQLite row IDs can be
    signed even though valid domain envelopes require strictly positive IDs.
    """

    id: int
    namespace: str
    decode_error: str

    def __post_init__(self) -> None:
        require_int64(self.id, "id")
        if not isinstance(self.namespace, str) or not self.namespace.strip():
            raise ValueError("namespace must be nonblank")
        if not isinstance(self.decode_error, str) or not self.decode_error.strip():
            raise ValueError("decode_error must be nonblank")
