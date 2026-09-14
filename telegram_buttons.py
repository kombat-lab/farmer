from __future__ import annotations

from game_message import ReadableGameMessage
from text_normalization import normalize_text

ButtonPosition = tuple[int, int]


def get_button_texts(message: ReadableGameMessage) -> list[str]:
    if not message.buttons:
        return []
    return [
        button.text
        for row in message.buttons
        for button in row
        if button.text
    ]


def find_button(
    message: ReadableGameMessage,
    *,
    exact: str | None = None,
    contains: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> ButtonPosition | None:
    if not message.buttons:
        return None

    normalized_contains = tuple(normalize_text(value) for value in contains)
    normalized_exclude = tuple(normalize_text(value) for value in exclude)

    for row_index, row in enumerate(message.buttons):
        for column_index, button in enumerate(row):
            button_text = button.text
            if exact is not None and button_text == exact:
                return row_index, column_index

            normalized_text = normalize_text(button_text)
            if normalized_exclude and any(value in normalized_text for value in normalized_exclude):
                continue
            if normalized_contains and all(
                value in normalized_text for value in normalized_contains
            ):
                return row_index, column_index

    return None
