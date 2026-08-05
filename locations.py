from __future__ import annotations

from dataclasses import dataclass

from config import AGGRESSIVE_MONSTERS, TARGET_MONSTER_CATEGORIES
from models import Position


@dataclass(frozen=True)
class Location:
    key: str
    name: str
    monsters: tuple[str, ...]
    aggressive_monsters: frozenset[str]
    min_x: int
    max_x: int
    min_y: int
    max_y: int
    start_position: Position
    blocked_cells: frozenset[Position]

    def priority(self, monster: str) -> int:
        try:
            return self.monsters.index(monster)
        except ValueError:
            return len(self.monsters)


_DEFAULT_GEOMETRY = {
    "min_x": 0,
    "max_x": 8,
    "min_y": 0,
    "max_y": 8,
    "start_position": (0, 4),
    "blocked_cells": frozenset(),
}

_LOCATION_GEOMETRY: dict[str, dict[str, object]] = {
    "Мертвый лес": {
        "min_x": 0,
        "max_x": 11,
        "min_y": 0,
        "max_y": 11,
        "start_position": (0, 0),
        "blocked_cells": frozenset(
            {
                (5, 1),
                (9, 1),
                (9, 2),
                (10, 2),
                (1, 9),
                (2, 10),
                (4, 11),
            }
        ),
    },
}


def _build_location(name: str, monsters: list[str]) -> Location:
    geometry = {
        **_DEFAULT_GEOMETRY,
        **_LOCATION_GEOMETRY.get(name, {}),
    }
    return Location(
        key=name.casefold().replace(" ", "_"),
        name=name,
        monsters=tuple(monsters),
        aggressive_monsters=frozenset(
            monster
            for monster in monsters
            if monster in AGGRESSIVE_MONSTERS
        ),
        min_x=int(geometry["min_x"]),
        max_x=int(geometry["max_x"]),
        min_y=int(geometry["min_y"]),
        max_y=int(geometry["max_y"]),
        start_position=geometry["start_position"],  # type: ignore[arg-type]
        blocked_cells=geometry["blocked_cells"],  # type: ignore[arg-type]
    )


LOCATIONS = {
    name: _build_location(name, monsters)
    for name, monsters in TARGET_MONSTER_CATEGORIES.items()
}


def get_location(name: str) -> Location:
    location = LOCATIONS.get(name)
    if location is None:
        # New game locations must not crash navigation. Until explicit
        # geometry is configured, use the safe default rectangular map.
        location = _build_location(name, [])
        LOCATIONS[name] = location
    return location


def location_for_monster(monster: str) -> Location | None:
    return next(
        (
            location
            for location in LOCATIONS.values()
            if monster in location.monsters
        ),
        None,
    )
