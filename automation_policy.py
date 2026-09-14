from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

CombatPlannerMode = Literal["shadow", "guarded", "active"]
COMBAT_PLANNER_MODES: tuple[CombatPlannerMode, ...] = (
    "shadow",
    "guarded",
    "active",
)


def parse_combat_planner_mode(
    value: object,
    *,
    default: CombatPlannerMode = "shadow",
) -> CombatPlannerMode:
    if default not in COMBAT_PLANNER_MODES:
        raise ValueError("Default combat planner mode must be canonical")
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized == "shadow":
            return "shadow"
        if normalized == "guarded":
            return "guarded"
        if normalized == "active":
            return "active"
    return default


def _positive_integer(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} должно быть положительным целым числом")
    return value


def _canonical_names(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{name} должны быть списком или кортежем строк")
    result: list[str] = []
    seen: set[str] = set()
    for candidate in value:
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError(f"{name} должны быть непустыми строками")
        normalized = candidate.strip()
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return tuple(result)


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Задержка должна быть конечным числом")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Задержка должна быть конечным числом")
    return result


def _finite_range(minimum: object, maximum: object) -> tuple[float, float]:
    low = _finite_number(minimum)
    high = _finite_number(maximum)
    if low < 0 or high < low:
        raise ValueError("Нужен конечный неотрицательный диапазон")
    return low, high


def _require_policy_type(value: object, expected: type[object]) -> None:
    if not isinstance(value, expected):
        raise ValueError(f"Ожидался объект политики {expected.__name__}")


@dataclass(frozen=True, slots=True)
class IntegerRange:
    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        _positive_integer(self.minimum, "Минимум")
        _positive_integer(self.maximum, "Максимум")
        if self.maximum < self.minimum:
            raise ValueError("Максимум не должен быть меньше минимума")


@dataclass(frozen=True, slots=True)
class DelayRange:
    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        low, high = _finite_range(self.minimum, self.maximum)
        object.__setattr__(self, "minimum", low)
        object.__setattr__(self, "maximum", high)


@dataclass(frozen=True, slots=True)
class RunPolicy:
    cycles_count: int

    def __post_init__(self) -> None:
        _positive_integer(self.cycles_count, "Количество циклов")


@dataclass(frozen=True, slots=True)
class TargetPolicy:
    """Targets available to discovery, independent of combat treatment rules."""

    enabled: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "enabled",
            _canonical_names(self.enabled, "Цели поиска"),
        )


@dataclass(frozen=True, slots=True)
class RuntimeTimingPolicy:
    """Mechanism-independent scheduling owned by the application runtime."""

    long_pause: DelayRange
    long_pause_chance: float
    cycle_rest: DelayRange

    def __post_init__(self) -> None:
        _require_policy_type(self.long_pause, DelayRange)
        _require_policy_type(self.cycle_rest, DelayRange)
        chance, _ = _finite_range(self.long_pause_chance, self.long_pause_chance)
        if chance > 1:
            raise ValueError("Вероятность паузы должна быть от 0 до 1")
        object.__setattr__(self, "long_pause_chance", chance)


@dataclass(frozen=True, slots=True)
class LegacyMapPolicy:
    """Settings owned only by the current map-based discovery adapter."""

    moves_per_cycle: IntegerRange
    blessing_enabled: bool
    move_delay: DelayRange
    open_attack_delay: DelayRange
    target_selection_delay: DelayRange

    def __post_init__(self) -> None:
        _require_policy_type(self.moves_per_cycle, IntegerRange)
        for delay in (
            self.move_delay,
            self.open_attack_delay,
            self.target_selection_delay,
        ):
            _require_policy_type(delay, DelayRange)
        if type(self.blessing_enabled) is not bool:
            raise ValueError("Признак благословения должен быть bool")


@dataclass(frozen=True, slots=True)
class LegacyCombatPolicy:
    """Settings owned only by the current HP/mana/skill combat adapter."""

    treatment_enemies: tuple[str, ...]
    heal_threshold: int
    battle_start_hp_percent: int
    planner_mode: CombatPlannerMode
    target_selection_delay: DelayRange
    skill_delay: DelayRange

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "treatment_enemies",
            _canonical_names(self.treatment_enemies, "Цели лечения противника"),
        )
        _positive_integer(self.heal_threshold, "Порог лечения")
        if (
            type(self.battle_start_hp_percent) is not int
            or self.battle_start_hp_percent not in (50, 100)
        ):
            raise ValueError("Начальный HP должен быть 50 или 100 процентов")
        if self.planner_mode not in COMBAT_PLANNER_MODES:
            raise ValueError("Нужен канонический режим боевого планировщика")
        _require_policy_type(self.target_selection_delay, DelayRange)
        _require_policy_type(self.skill_delay, DelayRange)