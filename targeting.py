from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from models import ButtonPosition
from parser import normalize
from telegram_buttons import get_button_texts


@dataclass(frozen=True)
class MapTargetAnalysis:
    selected_target: Optional[str]
    target_counts: dict[str, tuple[int, int]]

    @property
    def all_matching_targets_are_occupied(self) -> bool:
        return bool(self.target_counts) and all(
            found > 0 and occupied >= found
            for found, occupied in self.target_counts.values()
        )


def analyze_map_targets(
    message,
    configured_targets: Iterable[str],
) -> MapTargetAnalysis:
    target_counts: dict[str, tuple[int, int]] = {}
    selected_target: Optional[str] = None
    button_texts = get_button_texts(message)

    for configured_target in configured_targets:
        normalized_target = normalize(configured_target)
        found = 0
        occupied = 0
        for button_text in button_texts:
            normalized_button = normalize(button_text)
            if "pvp:" in normalized_button:
                continue
            if normalized_target not in normalized_button:
                continue
            found += 1
            if "занят" in normalized_button:
                occupied += 1
            elif selected_target is None:
                selected_target = configured_target
        if found:
            target_counts[configured_target] = (found, occupied)

    return MapTargetAnalysis(selected_target, target_counts)


def select_combat_target(
    message,
    priorities: Iterable[str],
    active_target: Optional[str] = None,
) -> tuple[Optional[str], Optional[ButtonPosition]]:
    if not getattr(message, "buttons", None):
        return None, None

    candidates: list[tuple[str, int, int]] = []
    ignored = ("отмена", "к карте", "назад", "pvp:", "занят")

    for row_index, row in enumerate(message.buttons):
        for column_index, button in enumerate(row):
            text = getattr(button, "text", "").strip()
            normalized = normalize(text)
            if not text or any(value in normalized for value in ignored):
                continue
            candidates.append((text, row_index, column_index))

    if not candidates:
        return None, None

    ordered = list(priorities)
    if active_target and active_target not in ordered:
        ordered.append(active_target)

    for preferred in ordered:
        preferred_normalized = normalize(preferred)
        for text, row, column in candidates:
            if preferred_normalized in normalize(text):
                return preferred, (row, column)

    text, row, column = candidates[0]
    return text, (row, column)
