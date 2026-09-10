from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from config import (
    DEFAULT_ATTACK_DELAY_MAX,
    DEFAULT_ATTACK_DELAY_MIN,
    DEFAULT_BATTLE_START_HP_PERCENT,
    DEFAULT_COMBAT_PLANNER_MODE,
    DEFAULT_CYCLE_REST_MAX,
    DEFAULT_CYCLE_REST_MIN,
    DEFAULT_CYCLES_COUNT,
    DEFAULT_HEAL_THRESHOLD,
    DEFAULT_LONG_PAUSE_CHANCE,
    DEFAULT_LONG_PAUSE_MAX,
    DEFAULT_LONG_PAUSE_MIN,
    DEFAULT_MOVE_DELAY_MAX,
    DEFAULT_MOVE_DELAY_MIN,
    DEFAULT_MOVES_PER_CYCLE_MAX,
    DEFAULT_MOVES_PER_CYCLE_MIN,
    DEFAULT_SKILL_DELAY_MAX,
    DEFAULT_SKILL_DELAY_MIN,
    DEFAULT_TARGET_DELAY_MAX,
    DEFAULT_TARGET_DELAY_MIN,
)
from game_catalog import ALL_MONSTER_NAMES, get_monster_names
from numeric_validation import MAX_WAIT_SECONDS, bounded_integer, finite_number, finite_range
from storage import Storage

MAX_CYCLES_COUNT = 10_000
MAX_MOVES_PER_CYCLE = 1_000_000
MAX_HEAL_THRESHOLD = 1_000_000


class DelayKind(StrEnum):
    MOVE = "move_delay"
    ATTACK = "attack_delay"
    TARGET = "target_delay"
    SKILL = "skill_delay"
    LONG_PAUSE = "long_pause"
    CYCLE_REST = "cycle_rest"

    @property
    def limit_seconds(self) -> float:
        if self is DelayKind.CYCLE_REST:
            return MAX_WAIT_SECONDS
        if self is DelayKind.LONG_PAUSE:
            return 3600.0
        return 300.0

    @property
    def input_multiplier(self) -> float:
        return 60.0 if self is DelayKind.CYCLE_REST else 1.0

NON_UI_SETTING_KEYS = frozenset(
    {
        "farmer_stop_requested",
        "telegram_cooldown_until",
        "telegram_cooldown_reason",
        "navigation_model_version",
    }
)


@dataclass
class FarmerSettings:
    cycles_count: int = DEFAULT_CYCLES_COUNT
    moves_per_cycle_min: int = DEFAULT_MOVES_PER_CYCLE_MIN
    moves_per_cycle_max: int = DEFAULT_MOVES_PER_CYCLE_MAX

    enabled_targets: list[str] = field(default_factory=lambda: list(ALL_MONSTER_NAMES))
    treatment_enemy_targets: list[str] = field(default_factory=list)

    heal_threshold: int = DEFAULT_HEAL_THRESHOLD
    battle_start_hp_percent: int = DEFAULT_BATTLE_START_HP_PERCENT
    combat_planner_mode: str = DEFAULT_COMBAT_PLANNER_MODE
    blessing_enabled: bool = False

    move_delay_min: float = DEFAULT_MOVE_DELAY_MIN
    move_delay_max: float = DEFAULT_MOVE_DELAY_MAX

    attack_delay_min: float = DEFAULT_ATTACK_DELAY_MIN
    attack_delay_max: float = DEFAULT_ATTACK_DELAY_MAX

    target_delay_min: float = DEFAULT_TARGET_DELAY_MIN
    target_delay_max: float = DEFAULT_TARGET_DELAY_MAX

    skill_delay_min: float = DEFAULT_SKILL_DELAY_MIN
    skill_delay_max: float = DEFAULT_SKILL_DELAY_MAX

    long_pause_chance: float = DEFAULT_LONG_PAUSE_CHANCE
    long_pause_min: float = DEFAULT_LONG_PAUSE_MIN
    long_pause_max: float = DEFAULT_LONG_PAUSE_MAX

    cycle_rest_min: float = DEFAULT_CYCLE_REST_MIN
    cycle_rest_max: float = DEFAULT_CYCLE_REST_MAX


class SettingsService:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.values = FarmerSettings()

    async def load(self) -> None:
        stored = await self.storage.get_settings()
        self.values = FarmerSettings()

        for key, value in stored.items():
            if key in FarmerSettings.__dataclass_fields__:
                setattr(self.values, key, value)

        self._upgrade_legacy_moves_setting(stored)

        self._normalize_enabled_targets()
        self._normalize_treatment_enemy_targets()
        self._normalize_character()
        self._normalize_moves_range()
        self._normalize_numeric_settings()
        self.values.blessing_enabled = self._normalize_bool(
            self.values.blessing_enabled,
            default=False,
        )
        self._normalize_combat_planner_mode()
        normalized = asdict(self.values)
        await self.storage.set_settings(normalized)
        await self.storage.delete_settings(
            set(stored) - set(normalized) - NON_UI_SETTING_KEYS
        )

    async def set_value(self, key: str, value: object) -> None:
        """Validated compatibility API; paired ranges are written together."""
        if key not in FarmerSettings.__dataclass_fields__:
            raise KeyError(key)
        for kind in DelayKind:
            minimum, maximum = self.get_delay_range(kind)
            if key == f"{kind}_min":
                await self.set_delay_range(kind, finite_number(value), maximum)
                return
            if key == f"{kind}_max":
                await self.set_delay_range(kind, minimum, finite_number(value))
                return
        if key in {"moves_per_cycle_min", "moves_per_cycle_max"}:
            number = bounded_integer(value, maximum=MAX_MOVES_PER_CYCLE)
            minimum_moves = (
                number if key.endswith("_min") else self.values.moves_per_cycle_min
            )
            maximum_moves = (
                number if key.endswith("_max") else self.values.moves_per_cycle_max
            )
            await self.set_moves_range(minimum_moves, maximum_moves)
            return
        if key == "cycles_count":
            value = bounded_integer(value, maximum=MAX_CYCLES_COUNT)
        elif key == "heal_threshold":
            value = bounded_integer(value, maximum=MAX_HEAL_THRESHOLD)
        elif key == "long_pause_chance":
            value = finite_number(value)
            finite_range(value, value, limit=1.0)
        elif key == "battle_start_hp_percent":
            value = bounded_integer(value, maximum=100)
            if value not in {50, 100}:
                raise ValueError("Начальный HP должен быть 50 или 100 процентов.")
        elif key == "combat_planner_mode":
            if not isinstance(value, str) or value not in {"shadow", "guarded", "active"}:
                raise ValueError("Неизвестный режим боевого планировщика.")
        elif key == "blessing_enabled":
            if not isinstance(value, bool):
                raise ValueError("Благословение должно быть включено или выключено.")
        elif key in {"enabled_targets", "treatment_enemy_targets"}:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError("Нужен список названий целей.")
            if key == "enabled_targets" and any(item not in ALL_MONSTER_NAMES for item in value):
                raise ValueError("Неизвестная цель.")
            value = list(dict.fromkeys(value))
        await self.storage.set_setting(key, value)
        setattr(self.values, key, value)

    async def set_cycles_count(self, value: int) -> None:
        await self.set_value("cycles_count", value)

    async def set_heal_threshold(self, value: int) -> None:
        await self.set_value("heal_threshold", value)

    async def set_long_pause_chance(self, value: float) -> None:
        await self.set_value("long_pause_chance", value)

    def get_delay_range(self, kind: DelayKind) -> tuple[float, float]:
        s = self.values
        ranges: dict[DelayKind, tuple[float, float]] = {
            DelayKind.MOVE: (s.move_delay_min, s.move_delay_max),
            DelayKind.ATTACK: (s.attack_delay_min, s.attack_delay_max),
            DelayKind.TARGET: (s.target_delay_min, s.target_delay_max),
            DelayKind.SKILL: (s.skill_delay_min, s.skill_delay_max),
            DelayKind.LONG_PAUSE: (s.long_pause_min, s.long_pause_max),
            DelayKind.CYCLE_REST: (s.cycle_rest_min, s.cycle_rest_max),
        }
        return ranges[kind]

    @staticmethod
    def parse_delay_range(kind: DelayKind, text: str) -> tuple[float, float]:
        parts = text.replace(",", ".").split()
        if len(parts) != 2:
            raise ValueError("Введите два числа: минимум и максимум.")
        # Validate stored seconds after converting minutes, including overflow.
        try:
            minimum, maximum = (finite_number(part) * kind.input_multiplier for part in parts)
            return finite_range(minimum, maximum, limit=kind.limit_seconds)
        except ValueError as error:
            unit = "мин." if kind is DelayKind.CYCLE_REST else "сек."
            raise ValueError(
                f"Введите конечные числа от 0 до "
                f"{kind.limit_seconds / kind.input_multiplier:g} {unit}; "
                "максимум не меньше минимума."
            ) from error

    async def set_delay_range(
        self,
        kind: DelayKind,
        minimum: float,
        maximum: float,
    ) -> None:
        low, high = finite_range(minimum, maximum, limit=kind.limit_seconds)
        values = {f"{kind}_min": low, f"{kind}_max": high}
        await self.storage.set_settings(values)
        for key, value in values.items():
            setattr(self.values, key, value)

    async def set_moves_range(self, minimum: int, maximum: int) -> None:
        self.validate_moves_range(minimum, maximum)
        await self.storage.set_settings(
            {
                "moves_per_cycle_min": minimum,
                "moves_per_cycle_max": maximum,
            }
        )
        self.values.moves_per_cycle_min = minimum
        self.values.moves_per_cycle_max = maximum

    async def toggle_blessing(self) -> bool:
        enabled = not self.values.blessing_enabled
        self.values.blessing_enabled = enabled
        await self.storage.set_setting("blessing_enabled", enabled)
        return enabled

    async def cycle_combat_planner_mode(self) -> str:
        modes = ("shadow", "guarded", "active")
        current = self.values.combat_planner_mode
        try:
            next_index = (modes.index(current) + 1) % len(modes)
        except ValueError:
            next_index = 0
        selected = modes[next_index]
        self.values.combat_planner_mode = selected
        await self.storage.set_setting("combat_planner_mode", selected)
        return selected

    async def add_treatment_enemy_target(self, target: str) -> bool:
        normalized = target.strip()
        known = {item.casefold() for item in self.values.treatment_enemy_targets}
        if not normalized or normalized.casefold() in known:
            return False
        self.values.treatment_enemy_targets.append(normalized)
        await self.storage.set_setting(
            "treatment_enemy_targets",
            self.values.treatment_enemy_targets,
        )
        return True

    async def remove_treatment_enemy_target(self, target: str) -> bool:
        normalized = target.strip().casefold()
        updated = [
            item
            for item in self.values.treatment_enemy_targets
            if item.casefold() != normalized
        ]
        if len(updated) == len(self.values.treatment_enemy_targets):
            return False
        self.values.treatment_enemy_targets = updated
        await self.storage.set_setting("treatment_enemy_targets", updated)
        return True

    async def toggle_target(self, target: str) -> bool:
        if target not in ALL_MONSTER_NAMES:
            raise ValueError(f"Неизвестный моб: {target}")

        targets = list(self.values.enabled_targets or [])

        if target in targets:
            targets.remove(target)
            enabled = False
        else:
            targets.append(target)
            enabled = True

        targets = self._sort_targets(targets)

        self.values.enabled_targets = targets
        await self.storage.set_setting("enabled_targets", targets)

        return enabled

    async def set_category_enabled(
        self,
        category: str,
        enabled: bool,
    ) -> list[str]:
        category_targets = get_monster_names(category)

        if not category_targets:
            raise ValueError(f"Неизвестная категория мобов: {category}")

        current_targets = list(self.values.enabled_targets or [])

        if enabled:
            selected = set(current_targets)
            selected.update(category_targets)
            current_targets = list(selected)
        else:
            category_target_set = set(category_targets)
            current_targets = [
                target for target in current_targets if target not in category_target_set
            ]

        current_targets = self._sort_targets(current_targets)

        self.values.enabled_targets = current_targets
        await self.storage.set_setting(
            "enabled_targets",
            current_targets,
        )

        return current_targets

    def get_category_enabled_count(self, category: str) -> tuple[int, int]:
        category_targets = get_monster_names(category)
        selected = set(self.values.enabled_targets or [])

        enabled_count = sum(target in selected for target in category_targets)

        return enabled_count, len(category_targets)

    def _normalize_enabled_targets(self) -> None:
        self.values.enabled_targets = self._coerce_enabled_targets(self.values.enabled_targets)

    def _normalize_treatment_enemy_targets(self) -> None:
        raw_targets: object = self.values.treatment_enemy_targets
        if not isinstance(raw_targets, list):
            self.values.treatment_enemy_targets = []
            return
        normalized: list[str] = []
        seen: set[str] = set()
        for value in raw_targets:
            target = str(value).strip()
            key = target.casefold()
            if not target or key in seen:
                continue
            seen.add(key)
            normalized.append(target)
        self.values.treatment_enemy_targets = normalized

    @classmethod
    def _coerce_enabled_targets(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return list(ALL_MONSTER_NAMES)
        return cls._sort_targets([target for target in value if isinstance(target, str)])

    @staticmethod
    def _sort_targets(targets: list[str]) -> list[str]:
        selected = set(targets)

        return [target for target in ALL_MONSTER_NAMES if target in selected]

    def _normalize_character(self) -> None:
        try:
            threshold = bounded_integer(self.values.heal_threshold, maximum=MAX_HEAL_THRESHOLD)
        except ValueError:
            threshold = DEFAULT_HEAL_THRESHOLD
        self.values.heal_threshold = threshold

        try:
            battle_start_hp = bounded_integer(self.values.battle_start_hp_percent, maximum=100)
        except ValueError:
            battle_start_hp = DEFAULT_BATTLE_START_HP_PERCENT
        if battle_start_hp not in {50, 100}:
            battle_start_hp = DEFAULT_BATTLE_START_HP_PERCENT
        self.values.battle_start_hp_percent = battle_start_hp

    def _normalize_combat_planner_mode(self) -> None:
        mode = str(self.values.combat_planner_mode or "").strip().casefold()
        if mode not in {"shadow", "guarded", "active"}:
            mode = DEFAULT_COMBAT_PLANNER_MODE
        self.values.combat_planner_mode = mode

    def _upgrade_legacy_moves_setting(self, stored: dict[str, Any]) -> None:
        if (
            "moves_per_cycle_min" in stored
            or "moves_per_cycle_max" in stored
            or "moves_per_cycle" not in stored
        ):
            return
        try:
            previous = bounded_integer(stored["moves_per_cycle"], maximum=MAX_MOVES_PER_CYCLE)
        except ValueError:
            previous = 100
        self.values.moves_per_cycle_min = max(1, previous - 20)
        self.values.moves_per_cycle_max = previous + 20

    def _normalize_moves_range(self) -> None:
        try:
            minimum = bounded_integer(self.values.moves_per_cycle_min, maximum=MAX_MOVES_PER_CYCLE)
            maximum = bounded_integer(self.values.moves_per_cycle_max, maximum=MAX_MOVES_PER_CYCLE)
            self.validate_moves_range(minimum, maximum)
        except (TypeError, ValueError):
            minimum = DEFAULT_MOVES_PER_CYCLE_MIN
            maximum = DEFAULT_MOVES_PER_CYCLE_MAX
        self.values.moves_per_cycle_min = minimum
        self.values.moves_per_cycle_max = maximum

    def _normalize_numeric_settings(self) -> None:
        defaults = FarmerSettings()
        try:
            self.values.cycles_count = bounded_integer(
                self.values.cycles_count, maximum=MAX_CYCLES_COUNT
            )
        except ValueError:
            self.values.cycles_count = defaults.cycles_count
        try:
            chance = finite_number(self.values.long_pause_chance)
            finite_range(chance, chance, limit=1.0)
            self.values.long_pause_chance = chance
        except ValueError:
            self.values.long_pause_chance = defaults.long_pause_chance
        for kind in DelayKind:
            try:
                low, high = finite_range(*self.get_delay_range(kind), limit=kind.limit_seconds)
            except ValueError:
                low = getattr(defaults, f"{kind}_min")
                high = getattr(defaults, f"{kind}_max")
            setattr(self.values, f"{kind}_min", low)
            setattr(self.values, f"{kind}_max", high)

    @staticmethod
    def _normalize_bool(value: object, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"1", "true", "yes", "on", "да", "вкл"}:
                return True
            if normalized in {"0", "false", "no", "off", "нет", "выкл"}:
                return False
        return default

    @staticmethod
    def validate_character_value(value: int) -> None:
        bounded_integer(value, maximum=MAX_HEAL_THRESHOLD)

    @staticmethod
    def validate_moves_range(minimum: int, maximum: int) -> None:
        bounded_integer(minimum, maximum=MAX_MOVES_PER_CYCLE)
        bounded_integer(maximum, maximum=MAX_MOVES_PER_CYCLE)
        if maximum < minimum:
            raise ValueError("Минимум должен быть больше нуля, максимум — не меньше минимума.")

    @classmethod
    def parse_moves_range(cls, value: str) -> tuple[int, int]:
        match = re.fullmatch(
            r"\s*(\d+)\s*(?:[-–—:;]|\s+)\s*(\d+)\s*",
            value,
        )
        if match is None:
            raise ValueError("Нужно указать минимум и максимум.")
        minimum, maximum = map(int, match.groups())
        cls.validate_moves_range(minimum, maximum)
        return minimum, maximum

    @staticmethod
    def validate_range(minimum: float, maximum: float) -> None:
        finite_range(minimum, maximum)
