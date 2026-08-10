from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

Position = tuple[int, int]
ButtonPosition = tuple[int, int]


class BotState(Enum):
    STARTING = auto()
    MAP = auto()
    MOVING = auto()
    TARGET_SELECTION = auto()
    COMBAT = auto()
    RECOVERY = auto()
    PAUSED = auto()
    RESTING = auto()
    WAITING_FOR_HEALTH = auto()
    STOPPED = auto()


class ActionType(Enum):
    MOVE = auto()
    OPEN_ATTACK = auto()
    SELECT_TARGET = auto()
    USE_SKILL = auto()


class MessageKind(Enum):
    MAP = auto()
    MOVE_STARTED = auto()
    TARGET_SELECTION = auto()
    COMBAT_TARGET_SELECTION = auto()
    COMBAT_STARTED = auto()
    PLAYER_TURN = auto()
    BATTLE_FINISHED = auto()
    BATTLE_INVITE = auto()
    TARGET_GONE = auto()
    OTHER = auto()


class RouteDirection(Enum):
    DOWN = auto()
    UP = auto()


@dataclass(frozen=True)
class MapInfo:
    position: Position
    monster_count: int
    monsters: tuple[str, ...]
    found_target: str | None
    current_hp: int | None
    max_hp: int | None
    movement_finished: bool
    location_name: str | None = None
    map_size: tuple[int, int] | None = None
    status: str | None = None

    @property
    def displayed_monster_count(self) -> int:
        return len(self.monsters)

    @property
    def has_hidden_monsters(self) -> bool:
        return self.monster_count > self.displayed_monster_count

    @property
    def movement_blocked(self) -> bool:
        return bool(self.status and "туда пройти нельзя" in self.status.casefold())


@dataclass(frozen=True)
class MovePlan:
    origin: Position
    destination: Position
    button: str
    direction_before: RouteDirection
    direction_after_success: RouteDirection


@dataclass
class RuntimeContext:
    current_position: Position | None = None
    current_hp: int | None = None
    max_hp: int | None = None
    active_target: str | None = None
    battle_target: str | None = None
    combat_enemies: list[str] = field(default_factory=list)
    pending_skill: str | None = None
    checked_empty_position: Position | None = None
    checking_hidden_monsters: bool = False
    pending_move: MovePlan | None = None
    failed_move_attempts: int = 0
    move_count: int = 0
    kill_count: int = 0

    def add_combat_enemy(self, name: str) -> None:
        if not name:
            return
        if self.battle_target is None:
            self.battle_target = name
        if name not in self.combat_enemies:
            self.combat_enemies.append(name)

    def remove_combat_enemy(self, name: str) -> None:
        normalized = " ".join(name.casefold().split())
        self.combat_enemies = [
            enemy
            for enemy in self.combat_enemies
            if " ".join(enemy.casefold().split()) != normalized
        ]
        if self.active_target and " ".join(self.active_target.casefold().split()) == normalized:
            self.active_target = self.combat_enemies[0] if self.combat_enemies else None

    def clear_combat(self) -> None:
        self.active_target = None
        self.battle_target = None
        self.combat_enemies.clear()
        self.pending_skill = None
