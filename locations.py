from __future__ import annotations

from dataclasses import dataclass

from game_catalog import get_location
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

def get_location_geometry(name: str) -> LocationGeometry:
    location = get_location(name)
    if location is None:
        return _DEFAULT_GEOMETRY
    return LocationGeometry(
        min_x=0,
        max_x=location.fallback_width - 1,
        min_y=0,
        max_y=location.fallback_height - 1,
        start_position=location.fallback_start,
        blocked_cells=frozenset(),
    )
