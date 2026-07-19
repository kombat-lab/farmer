from __future__ import annotations

from typing import Optional, Protocol, Sequence

from models import ButtonPosition
from parser import normalize


class ButtonLike(Protocol):
    text: str


ButtonRows = Sequence[Sequence[ButtonLike]]


def get_button_texts(message) -> list[str]:
    if not getattr(message, "buttons", None):
        return []
    return [
        getattr(button, "text", "")
        for row in message.buttons
        for button in row
        if getattr(button, "text", "")
    ]


def find_button(
    message,
    *,
    exact: Optional[str] = None,
    contains: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> Optional[ButtonPosition]:
    if not getattr(message, "buttons", None):
        return None

    normalized_contains = tuple(normalize(value) for value in contains)
    normalized_exclude = tuple(normalize(value) for value in exclude)

    for row_index, row in enumerate(message.buttons):
        for column_index, button in enumerate(row):
            button_text = getattr(button, "text", "")
            if exact is not None and button_text == exact:
                return row_index, column_index

            normalized_text = normalize(button_text)
            if normalized_exclude and any(
                value in normalized_text for value in normalized_exclude
            ):
                continue
            if normalized_contains and all(
                value in normalized_text for value in normalized_contains
            ):
                return row_index, column_index

    return None
