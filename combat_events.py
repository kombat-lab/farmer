from __future__ import annotations

import re
from dataclasses import dataclass

DEFEATED_RE = re.compile(r"💀\s*(.+?)\s+повержен(?:а|о|ы)?(?:\s*$|\n)", re.IGNORECASE | re.MULTILINE)
NEAR_DEATH_RE = re.compile(r"💀\s*(.+?)\s+на грани смерти", re.IGNORECASE)


@dataclass(frozen=True)
class CombatRoundEvents:
    defeated_enemies: tuple[str, ...] = ()
    near_death_enemies: tuple[str, ...] = ()


def parse_combat_round_events(text: str) -> CombatRoundEvents:
    return CombatRoundEvents(
        defeated_enemies=tuple(match.strip() for match in DEFEATED_RE.findall(text)),
        near_death_enemies=tuple(match.strip() for match in NEAR_DEATH_RE.findall(text)),
    )
