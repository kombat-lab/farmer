from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from models import ButtonPosition
from parser import normalize
from telegram_buttons import get_button_texts


HP_PATTERN = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]")


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


def _extract_current_hp(button_text: str) -> Optional[int]:
    match = HP_PATTERN.search(button_text)
    if match is None:
        return None
    return int(match.group(1))


def _resolve_target_name(
    button_text: str,
    ordered_priorities: list[str],
) -> str:
    normalized_button = normalize(button_text)
    for preferred in ordered_priorities:
        if normalize(preferred) in normalized_button:
            return preferred

    return HP_PATTERN.sub("", button_text).strip()


def select_combat_target(
    message,
    priorities: Iterable[str],
    active_target: Optional[str] = None,
) -> tuple[Optional[str], Optional[ButtonPosition]]:
    if not getattr(message, "buttons", None):
        return None, None

    ordered_priorities = list(priorities)
    if active_target and active_target not in ordered_priorities:
        ordered_priorities.append(active_target)

    priority_indexes = {
        normalize(target): index
        for index, target in enumerate(ordered_priorities)
    }
    ignored = ("отмена", "к карте", "назад", "pvp:", "занят")
    candidates: list[tuple[int, int, int, int, str]] = []

    for row_index, row in enumerate(message.buttons):
        for column_index, button in enumerate(row):
            text = getattr(button, "text", "").strip()
            normalized = normalize(text)
            if not text or any(value in normalized for value in ignored):
                continue

            current_hp = _extract_current_hp(text)
            priority_index = len(ordered_priorities)
            for target, index in priority_indexes.items():
                if target in normalized:
                    priority_index = index
                    break

            # Цели с распознанным HP всегда идут раньше кнопок без HP.
            # Затем выбирается минимальное текущее HP, после чего сохраняется
            # исходный приоритет мобов и порядок кнопок.
            hp_missing = 1 if current_hp is None else 0
            hp_sort_value = current_hp if current_hp is not None else 10**12
            candidates.append(
                (
                    hp_missing,
                    hp_sort_value,
                    priority_index,
                    row_index * 1000 + column_index,
                    text,
                )
            )

    if not candidates:
        return None, None

    _, _, _, flat_position, button_text = min(candidates)
    row_index, column_index = divmod(flat_position, 1000)
    target_name = _resolve_target_name(button_text, ordered_priorities)
    return target_name, (row_index, column_index)
