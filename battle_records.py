from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, TypeAlias

from bounded_values import require_int64

BattleResult: TypeAlias = Literal["VICTORY", "DEFEAT"]
MIST_CRYSTAL_CODE = "mist_crystals"


class IdempotencyConflict(RuntimeError):
    """The same source event claimed different mandatory battle facts."""


def _nonnegative_amount(value: int, name: str) -> None:
    require_int64(value, name, minimum=0)


def _positive_identifier(value: int, name: str) -> None:
    require_int64(value, name, minimum=1)


def _display_name(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} не должно быть пустым")
    return value.strip()


@dataclass(frozen=True, slots=True)
class SourceEventId:
    """Opaque, restart-stable identity assigned by the source adapter."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise ValueError("Source event id must be a string")
        if not self.value or self.value != self.value.strip():
            raise ValueError("Source event id must be a nonblank canonical string")
        if len(self.value.encode("utf-8")) > 255:
            raise ValueError("Source event id must not exceed 255 UTF-8 bytes")
        if any(not character.isprintable() for character in self.value):
            raise ValueError("Source event id must not contain control characters")


@dataclass(frozen=True, slots=True)
class ItemDrop:
    name: str
    quantity: int = 1
    is_card: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _display_name(self.name, "Название предмета"))
        _positive_identifier(self.quantity, "Количество предметов")
        if not isinstance(self.is_card, bool):
            raise ValueError("Признак карты должен быть bool")


@dataclass(frozen=True, slots=True)
class RewardBundle:
    xp: int = 0
    dust: int = 0
    crystals: int = 0
    items: tuple[ItemDrop, ...] = ()

    def __post_init__(self) -> None:
        _nonnegative_amount(self.xp, "Опыт")
        _nonnegative_amount(self.dust, "Пыль")
        _nonnegative_amount(self.crystals, "Кристаллы")
        object.__setattr__(self, "items", tuple(self.items))
        if any(not isinstance(item, ItemDrop) for item in self.items):
            raise ValueError("Нужны нормализованные предметы ItemDrop")



@dataclass(frozen=True, slots=True)
class BattleOutcome:
    """Mandatory battle facts, independent of map and decision-trace schemas."""

    source_event_id: SourceEventId
    source_message_id: int | None
    session_id: int | None
    target_name: str
    result: BattleResult
    rewards: RewardBundle = field(default_factory=RewardBundle)
    position: tuple[int, int] | None = None
    happened_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.source_event_id, SourceEventId):
            raise ValueError("Нужен нормализованный SourceEventId")
        if self.source_message_id is not None:
            _positive_identifier(self.source_message_id, "ID исходного сообщения")
        if self.session_id is not None:
            _positive_identifier(self.session_id, "ID сессии")
        object.__setattr__(self, "target_name", _display_name(self.target_name, "Название цели"))
        if self.result not in ("VICTORY", "DEFEAT"):
            raise ValueError("Неизвестный исход боя")
        if not isinstance(self.rewards, RewardBundle):
            raise ValueError("Нужна нормализованная награда RewardBundle")
        if (
            not isinstance(self.happened_at, datetime)
            or self.happened_at.tzinfo is None
            or self.happened_at.utcoffset() is None
        ):
            raise ValueError("Время исхода боя должно содержать часовой пояс")
        if self.position is not None:
            if not isinstance(self.position, (tuple, list)) or len(self.position) != 2:
                raise ValueError("Позиция должна содержать две целочисленные координаты")
            if any(
                isinstance(value, bool) or not isinstance(value, int) for value in self.position
            ):
                raise ValueError("Позиция должна содержать две целочисленные координаты")
            for coordinate in self.position:
                require_int64(coordinate, "Координата")
            object.__setattr__(self, "position", tuple(self.position))


@dataclass(frozen=True, slots=True)
class RecordBattleResult:
    inserted: bool
    battle_id: int
    cards: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.inserted) is not bool:
            raise ValueError("inserted must be bool")
        _positive_identifier(self.battle_id, "ID боя")
        if not isinstance(self.cards, (tuple, list)):
            raise ValueError("cards must be a collection of nonblank names")
        object.__setattr__(self, "cards", tuple(
            _display_name(name, "Карта") for name in self.cards
        ))
