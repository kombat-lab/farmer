from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, Self

from battle_records import SourceEventId
from bounded_values import require_int64


class ReadableButton(Protocol):
    @property
    def text(self) -> str: ...


class ReadableMessage(Protocol):
    """Data needed to capture an update; no RPC capability is required."""

    @property
    def id(self) -> int: ...

    @property
    def raw_text(self) -> str | None: ...

    @property
    def edit_date(self) -> datetime | None: ...

    @property
    def buttons(self) -> Sequence[Sequence[ReadableButton]] | None: ...


@dataclass(frozen=True, slots=True)
class ButtonSnapshot:
    text: str
    callback_data: bytes | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("Button text must be a string")
        if self.callback_data is not None and not isinstance(self.callback_data, bytes):
            raise ValueError("Callback data must be immutable bytes or None")

    @classmethod
    def from_button(cls, button: ReadableButton) -> Self:
        data = getattr(button, "data", None)
        if data is None:
            data = getattr(button, "callback_data", None)
        return cls(button.text, data if isinstance(data, bytes) else None)


def _canonical_button_rows(value: object) -> tuple[tuple[ButtonSnapshot, ...], ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError("Message buttons must be a two-dimensional list or tuple")
    rows: list[tuple[ButtonSnapshot, ...]] = []
    for row in value:
        if not isinstance(row, (tuple, list)):
            raise ValueError("Message button rows must be lists or tuples")
        buttons = tuple(row)
        if any(not isinstance(button, ButtonSnapshot) for button in buttons):
            raise ValueError("Message buttons must contain ButtonSnapshot values")
        rows.append(buttons)
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class MessageSnapshot:
    id: int
    raw_text: str = ""
    buttons: tuple[tuple[ButtonSnapshot, ...], ...] = ()
    edit_date: datetime | None = None

    def __post_init__(self) -> None:
        require_int64(self.id, "Message id", minimum=1)
        if not isinstance(self.raw_text, str):
            raise ValueError("Message text must be a string")
        if self.edit_date is not None and (
            not isinstance(self.edit_date, datetime)
            or self.edit_date.tzinfo is None
            or self.edit_date.utcoffset() is None
        ):
            raise ValueError("Message edit time must contain a timezone")
        object.__setattr__(self, "buttons", _canonical_button_rows(self.buttons))

    @classmethod
    def from_message(cls, message: ReadableMessage) -> Self:
        raw_text = message.raw_text
        if raw_text is None:
            raw_text = ""
        return cls(
            id=message.id,
            raw_text=raw_text,
            buttons=tuple(
                tuple(ButtonSnapshot.from_button(button) for button in row)
                for row in (message.buttons or ())
            ),
            edit_date=message.edit_date,
        )

    @property
    def revision_timestamp(self) -> float:
        return self.edit_date.timestamp() if self.edit_date is not None else 0.0


def canonical_source_scope(source_scope: str) -> str:
    if type(source_scope) is not str:
        raise ValueError("Source scope must be a string")
    if not source_scope or source_scope != source_scope.strip():
        raise ValueError("Source scope must be a nonblank canonical string")
    if len(source_scope.encode("utf-8")) > 255:
        raise ValueError("Source scope must not exceed 255 UTF-8 bytes")
    if any(not character.isprintable() for character in source_scope):
        raise ValueError("Source scope must not contain control characters")
    return source_scope


def derive_source_event_id(
    source_scope: str, snapshot: MessageSnapshot
) -> SourceEventId:
    """Derive a stable opaque identity from one immutable source revision."""
    scope = canonical_source_scope(source_scope)
    if not isinstance(snapshot, MessageSnapshot):
        raise ValueError("Source identity requires a MessageSnapshot")
    edited_at = (
        snapshot.edit_date.astimezone(UTC).isoformat(timespec="microseconds")
        if snapshot.edit_date is not None
        else None
    )
    buttons = [
        [
            [
                button.text,
                (
                    base64.urlsafe_b64encode(button.callback_data).decode("ascii")
                    if button.callback_data is not None
                    else None
                ),
            ]
            for button in row
        ]
        for row in snapshot.buttons
    ]
    envelope = ["source-revision-v1", scope, snapshot.id, edited_at, snapshot.raw_text, buttons]
    canonical = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=")
    return SourceEventId("src1:" + digest.decode("ascii"))


def legacy_v4_message_source_event_id(message_id: int) -> SourceEventId:
    """Identify a pre-v5 battle row for the one-time compatibility bridge."""
    identifier = require_int64(message_id, "Legacy message id", minimum=1)
    material = f"fog-farmer:v4:telegram-message:{identifier}".encode("ascii")
    digest = base64.urlsafe_b64encode(hashlib.sha256(material).digest()).rstrip(b"=")
    return SourceEventId("legacy-v4:" + digest.decode("ascii"))
