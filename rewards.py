from __future__ import annotations

import re
from dataclasses import dataclass

DUST_RE = re.compile(
    r"•\s*\+\s*(\d+)\s*ед\."
    r"(?:\s*\([^)]*\))?\s*"
    r"Туманной\s+пыли",
    re.IGNORECASE,
)

XP_RE = re.compile(
    r"•\s*\+\s*(\d+)\s*XP"
    r"(?:\s*\([^)]*\))?",
    re.IGNORECASE,
)

ITEMS_HEADER = "Предметы:"
ITEM_STACK_RE = re.compile(
    r"^(?P<name>.+?)\s+[xх×]\s*(?P<quantity>\d+)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BattleReward:
    dust: int
    xp: int
    items: tuple[str, ...]


def parse_item_stack(item: str) -> tuple[str, int]:
    """Separates an item name from a trailing stack marker such as ``x3``."""
    cleaned = item.strip()
    match = ITEM_STACK_RE.fullmatch(cleaned)
    if match is None:
        return cleaned, 1

    quantity = int(match.group("quantity"))
    if quantity < 1:
        return cleaned, 1
    return match.group("name").strip(), quantity


def parse_battle_reward(text: str) -> BattleReward:
    """
    Разбирает сообщение о победе.

    Значения в скобках, например (🪬 2), игнорируются.
    """
    dust_match = DUST_RE.search(text)
    xp_match = XP_RE.search(text)

    items: list[str] = []

    if ITEMS_HEADER in text:
        _, raw_items = text.split(ITEMS_HEADER, 1)

        for line in raw_items.splitlines():
            item = line.strip()

            if item:
                items.append(item)

    return BattleReward(
        dust=int(dust_match.group(1)) if dust_match else 0,
        xp=int(xp_match.group(1)) if xp_match else 0,
        items=tuple(items),
    )
