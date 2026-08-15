from __future__ import annotations

from dataclasses import dataclass

from combat_round import CombatRoundState, parse_combat_round


@dataclass(frozen=True)
class CombatRoundEvents:
    defeated_enemies: tuple[str, ...] = ()
    near_death_enemies: tuple[str, ...] = ()


def parse_combat_round_events(
    text: str,
    round_state: CombatRoundState | None = None,
) -> CombatRoundEvents:
    parsed = round_state or parse_combat_round(text)
    if parsed is None:
        return CombatRoundEvents()
    return CombatRoundEvents(
        defeated_enemies=parsed.defeated,
        near_death_enemies=parsed.near_death,
    )
