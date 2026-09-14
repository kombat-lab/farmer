from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from game_message import ReadableGameMessage
from models import ButtonPosition
from parser import normalize

HP_PATTERN = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]")
OCCUPIED_PATTERN = re.compile(r"\bзанят(?:а|о|ы)?\b", re.IGNORECASE)
OCCUPIED_SUFFIX = re.compile(
    r"\s*(?:[·|—–-]\s*)?[([]?\s*занят(?:а|о|ы)?\s*[)\]]?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TargetButton:
    name: str
    current_hp: int | None
    occupied: bool


def parse_target_button(text: str) -> TargetButton | None:
    """Separate presentation metadata from the complete target name."""
    normalized = normalize(text)
    if not normalized or "pvp:" in normalized:
        return None
    hp_match = HP_PATTERN.search(text)
    name = HP_PATTERN.sub("", text).strip()
    occupied = OCCUPIED_PATTERN.search(name) is not None
    name = OCCUPIED_SUFFIX.sub("", name).strip()
    if normalize(name) in {"отмена", "к карте", "назад"}:
        return None
    return TargetButton(
        name=name,
        current_hp=int(hp_match.group(1)) if hp_match else None,
        occupied=occupied,
    )


@dataclass(frozen=True)
class MapTargetAnalysis:
    selected_target: str | None
    target_counts: dict[str, tuple[int, int]]
    selected_position: ButtonPosition | None = None

    @property
    def all_matching_targets_are_occupied(self) -> bool:
        return bool(self.target_counts) and all(
            found > 0 and occupied >= found for found, occupied in self.target_counts.values()
        )


def analyze_map_targets(
    message: ReadableGameMessage,
    configured_targets: Iterable[str],
) -> MapTargetAnalysis:
    target_counts: dict[str, tuple[int, int]] = {}
    selected_target: str | None = None
    selected_position: ButtonPosition | None = None
    parsed_buttons = [
        ((row_index, column_index), parsed)
        for row_index, row in enumerate(message.buttons or ())
        for column_index, button in enumerate(row)
        if (parsed := parse_target_button(button.text)) is not None
    ]
    for configured_target in configured_targets:
        expected = normalize(configured_target)
        matches = [
            (position, button)
            for position, button in parsed_buttons
            if normalize(button.name) == expected
        ]
        if not matches:
            continue
        target_counts[configured_target] = (
            len(matches), sum(button.occupied for _, button in matches)
        )
        if selected_target is None:
            for position, button in matches:
                if not button.occupied:
                    selected_target = configured_target
                    selected_position = position
                    break
    return MapTargetAnalysis(selected_target, target_counts, selected_position)


def select_combat_target(
    message: ReadableGameMessage,
    priorities: Iterable[str],
    active_target: str | None = None,
    *,
    preferred_target: Literal["self", "enemy"] | None = None,
    character_name: str | None = None,
) -> tuple[str | None, ButtonPosition | None]:
    ordered_priorities = list(priorities)
    if active_target and active_target not in ordered_priorities:
        ordered_priorities.append(active_target)
    priority_indexes = {normalize(target): index for index, target in enumerate(ordered_priorities)}
    priority_names = {normalize(target): target for target in ordered_priorities}
    candidates: list[tuple[int, int, int, int, int, str]] = []
    for row_index, row in enumerate(message.buttons or ()):
        for column_index, raw_button in enumerate(row):
            button = parse_target_button(raw_button.text)
            if button is None or button.occupied:
                continue
            normalized = normalize(button.name)
            is_character = bool(character_name and normalize(character_name) == normalized)
            if preferred_target == "self" and not is_character:
                continue
            if preferred_target == "enemy" and is_character:
                continue
            # Unknown enemies remain selectable when the game supplies their
            # HP. Other unrecognised menu controls are not combat targets.
            if (
                button.current_hp is None
                and normalized not in priority_indexes
                and not is_character
            ):
                continue
            candidates.append((
                int(button.current_hp is None),
                button.current_hp if button.current_hp is not None else 10**12,
                priority_indexes.get(normalized, len(ordered_priorities)),
                row_index,
                column_index,
                priority_names.get(normalized, button.name),
            ))
    if not candidates:
        return None, None
    _, _, _, row_index, column_index, name = min(candidates)
    return name, (row_index, column_index)
