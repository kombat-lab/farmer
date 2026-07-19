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


@dataclass(frozen=True)
class BattleReward:
    dust: int
    xp: int
    items: tuple[str, ...]


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
