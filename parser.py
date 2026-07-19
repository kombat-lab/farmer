from __future__ import annotations

import re
from typing import Iterable, Optional

from models import MapInfo, MessageKind


POSITION_RE = re.compile(
    r"Позиция:\s*\((\d+)\s*,\s*(\d+)\)",
    re.IGNORECASE,
)

MONSTERS_RE = re.compile(
    r"Монстры на клетке:\s*(\d+)(?:\s*\((.*?)\))?",
    re.IGNORECASE,
)

HEART_HP_RE = re.compile(
    r"❤️\s*(\d+)\s*/\s*(\d+)"
)


def normalize(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def parse_monsters(raw_monsters: Optional[str]) -> tuple[str, ...]:
    if not raw_monsters:
        return ()

    return tuple(
        monster.strip()
        for monster in raw_monsters.split(",")
        if monster.strip()
    )


def find_configured_target(
    names: Iterable[str],
    configured_targets: Iterable[str],
) -> Optional[str]:
    normalized_names = {
        normalize(name): name
        for name in names
    }

    for configured_target in configured_targets:
        if normalize(configured_target) in normalized_names:
            return configured_target

    return None


def extract_player_hp(
    text: str,
    character_name: str,
) -> Optional[tuple[int, int]]:
    escaped_name = re.escape(character_name)

    map_match = re.search(
        rf"{escaped_name}\s*\((\d+)\s*/\s*(\d+)\)",
        text,
        re.IGNORECASE,
    )
    if map_match:
        return int(map_match.group(1)), int(map_match.group(2))

    lines = [line.strip() for line in text.splitlines()]

    for index, line in enumerate(lines):
        if character_name.casefold() not in line.casefold():
            continue

        for nearby_line in lines[index + 1:index + 4]:
            hp_match = HEART_HP_RE.search(nearby_line)
            if hp_match:
                return int(hp_match.group(1)), int(hp_match.group(2))

    return None


COMBAT_TARGET_PATTERNS = (
    re.compile(
        r"Вы напали:\s*\n?\s*(.+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"На вас напали:\s*\n?\s*(.+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"На помощь врагу присоединился\s+(.+)",
        re.IGNORECASE,
    ),
)


def extract_combat_target(text: str) -> Optional[str]:
    for pattern in COMBAT_TARGET_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue

        target = match.group(1).strip().splitlines()[0].strip()
        return target or None

    return None


def parse_map(
    text: str,
    configured_targets: Iterable[str],
    character_name: str,
) -> Optional[MapInfo]:
    position_match = POSITION_RE.search(text)
    monsters_match = MONSTERS_RE.search(text)

    if not position_match or not monsters_match:
        return None

    monsters = parse_monsters(monsters_match.group(2))
    hp = extract_player_hp(text, character_name)

    return MapInfo(
        position=(
            int(position_match.group(1)),
            int(position_match.group(2)),
        ),
        monster_count=int(monsters_match.group(1)),
        monsters=monsters,
        found_target=find_configured_target(
            monsters,
            configured_targets,
        ),
        current_hp=hp[0] if hp else None,
        max_hp=hp[1] if hp else None,
        movement_finished=(
            "Переход между клетками завершён" in text
        ),
    )


def classify_message(
    text: str,
    configured_targets: Iterable[str],
    character_name: str,
) -> MessageKind:
    if parse_map(text, configured_targets, character_name) is not None:
        return MessageKind.MAP

    if "Шаг начат" in text:
        return MessageKind.MOVE_STARTED

    if "Выбери цель для нападения" in text:
        return MessageKind.TARGET_SELECTION

    if "Выберите цель для" in text or "Выбери цель для" in text:
        return MessageKind.COMBAT_TARGET_SELECTION

    if (
        "Вы напали:" in text
        or "На вас напали:" in text
        or "На помощь врагу присоединился" in text
    ):
        return MessageKind.COMBAT_STARTED

    if "Выберите навык:" in text:
        return MessageKind.PLAYER_TURN

    if "Бой завершён" in text:
        return MessageKind.BATTLE_FINISHED

    normalized = normalize(text)

    if (
        "монстр не найден в текущей клетке" in normalized
        or "монстр не найден на текущей клетке" in normalized
    ):
        return MessageKind.TARGET_GONE
    if "приглаш" in normalized and "бой" in normalized:
        return MessageKind.BATTLE_INVITE

    return MessageKind.OTHER
