from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from game_input import InboundEvent
from message_snapshot import MessageSnapshot


class CombatEventKind(Enum):
    STARTED = "started"
    TURN = "turn"
    TARGET_SELECTION = "target_selection"
    FINISHED = "finished"
    INVITATION = "invitation"
    UPDATE = "update"


class CombatActionKind(Enum):
    ACTION = "action"
    TARGET = "target"


@dataclass(frozen=True, slots=True)
class CombatAction:
    kind: CombatActionKind
    position: tuple[int, int]
    label: str
    urgent: bool = False
    remaining_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CombatActionKind):
            raise ValueError("Нужен тип боевого действия CombatActionKind")
        if type(self.urgent) is not bool:
            raise ValueError("Признак срочности должен быть bool")
        if self.remaining_seconds is not None and (
            type(self.remaining_seconds) is not int or self.remaining_seconds < 0
        ):
            raise ValueError("Оставшееся время должно быть неотрицательным целым")
        if not isinstance(self.position, (tuple, list)) or len(self.position) != 2:
            raise ValueError("Позиция кнопки должна содержать строку и столбец")
        if any(type(value) is not int or value < 0 for value in self.position):
            raise ValueError("Строка и столбец кнопки должны быть неотрицательными целыми")
        object.__setattr__(self, "position", tuple(self.position))
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("Описание боевого действия не должно быть пустым")
        object.__setattr__(self, "label", self.label.strip())


class CombatObservation(Protocol):
    @property
    def event(self) -> InboundEvent: ...

    @property
    def authoritative(self) -> bool: ...

    @property
    def kind(self) -> CombatEventKind: ...

    @property
    def snapshot(self) -> MessageSnapshot: ...

    @property
    def observed_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class CombatStatus:
    active: bool
    target_name: str | None
    enemy_names: tuple[str, ...]
    pending_action_label: str | None
    observations: int

    def __post_init__(self) -> None:
        if type(self.active) is not bool:
            raise ValueError("Состояние активности должно быть bool")
        if type(self.observations) is not int or self.observations < 0:
            raise ValueError("Количество наблюдений должно быть неотрицательным целым")
        for label in (self.target_name, self.pending_action_label):
            if label is not None and (not isinstance(label, str) or not label.strip()):
                raise ValueError("Название должно быть непустой строкой или None")
        if not isinstance(self.enemy_names, (tuple, list)) or any(
            not isinstance(name, str) or not name.strip() for name in self.enemy_names
        ):
            raise ValueError("Противники должны быть списком непустых строк")
        object.__setattr__(self, "enemy_names", tuple(self.enemy_names))


class CombatController(Protocol):
    async def initialize(self) -> None: ...

    async def persist(self) -> None: ...

    def reset(self) -> None: ...

    def status(self) -> CombatStatus: ...

    def observe_message(self, event: InboundEvent) -> CombatObservation | None: ...

    async def handle_message(
        self,
        observation: CombatObservation,
    ) -> bool: ...
