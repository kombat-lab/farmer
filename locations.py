from __future__ import annotations

from dataclasses import dataclass

from models import Position


@dataclass(frozen=True)
class LocationGeometry:
    min_x: int
    max_x: int
    min_y: int
    max_y: int
    start_position: Position
    blocked_cells: frozenset[Position]


_DEFAULT_GEOMETRY = LocationGeometry(
    min_x=0,
    max_x=8,
    min_y=0,
    max_y=8,
    start_position=(0, 4),
    blocked_cells=frozenset(),
)

_LOCATION_GEOMETRY: dict[str, LocationGeometry] = {
    "Мертвый лес": LocationGeometry(
        min_x=0,
        max_x=11,
        min_y=0,
        max_y=11,
        start_position=(0, 0),
        blocked_cells=frozenset(
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
    ),
}

def get_location_geometry(name: str) -> LocationGeometry:
    # Для новых локаций без отдельной геометрии используется безопасная карта 9×9.
    return _LOCATION_GEOMETRY.get(name, _DEFAULT_GEOMETRY)
