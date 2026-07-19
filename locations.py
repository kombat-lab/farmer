from __future__ import annotations

from dataclasses import dataclass
from config import AGGRESSIVE_MONSTERS, TARGET_MONSTER_CATEGORIES

@dataclass(frozen=True)
class Location:
    key: str
    name: str
    monsters: tuple[str, ...]
    aggressive_monsters: frozenset[str]

    def priority(self, monster: str) -> int:
        try: return self.monsters.index(monster)
        except ValueError: return len(self.monsters)

LOCATIONS = {
    name: Location(name.casefold().replace(" ", "_"), name, tuple(monsters), frozenset(m for m in monsters if m in AGGRESSIVE_MONSTERS))
    for name, monsters in TARGET_MONSTER_CATEGORIES.items()
}

def get_location(name: str) -> Location:
    try: return LOCATIONS[name]
    except KeyError as exc: raise ValueError(f"Неизвестная локация: {name}") from exc

def location_for_monster(monster: str) -> Location | None:
    return next((loc for loc in LOCATIONS.values() if monster in loc.monsters), None)
