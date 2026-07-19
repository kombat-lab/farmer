from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


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
    found_target: Optional[str]
    current_hp: Optional[int]
    max_hp: Optional[int]
    movement_finished: bool

    @property
    def displayed_monster_count(self) -> int:
        return len(self.monsters)

    @property
    def has_hidden_monsters(self) -> bool:
        return self.monster_count > self.displayed_monster_count


@dataclass(frozen=True)
class MovePlan:
    origin: Position
    destination: Position
    button: str
    direction_before: RouteDirection
    direction_after_success: RouteDirection


@dataclass
class RuntimeContext:
    current_position: Optional[Position] = None
    current_hp: Optional[int] = None
    max_hp: Optional[int] = None

    active_target: Optional[str] = None
    battle_target: Optional[str] = None
    combat_enemies: list[str] = None  # type: ignore[assignment]
    pending_skill: Optional[str] = None
    checked_empty_position: Optional[Position] = None
    checking_hidden_monsters: bool = False

    pending_move: Optional[MovePlan] = None
    failed_move_attempts: int = 0

    move_count: int = 0
    kill_count: int = 0

    def __post_init__(self) -> None:
        if self.combat_enemies is None:
            self.combat_enemies = []

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
            enemy for enemy in self.combat_enemies
            if " ".join(enemy.casefold().split()) != normalized
        ]
        if self.active_target and " ".join(self.active_target.casefold().split()) == normalized:
            self.active_target = self.combat_enemies[0] if self.combat_enemies else None

    def clear_combat(self) -> None:
        self.active_target = None
        self.battle_target = None
        self.combat_enemies.clear()
        self.pending_skill = None
