from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from automation_policy import (
    COMBAT_PLANNER_MODES,
    CombatPlannerMode,
    DelayRange,
    IntegerRange,
    LegacyCombatPolicy,
    LegacyMapPolicy,
    RunPolicy,
    RuntimeTimingPolicy,
    TargetPolicy,
    parse_combat_planner_mode,
)
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
from storage_types import JsonValue

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

# Remove only keys with an explicit migration; preserve future extension settings.
DEPRECATED_SETTING_KEYS = frozenset({"moves_per_cycle", "activity_profile"})


def _canonical_target_names(
    values: object,
    *,
    require_known: bool,
    discard_invalid: bool = False,
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        if discard_invalid:
            return ()
        raise ValueError("Нужен список или кортеж названий целей.")
    known = {target.casefold(): target for target in ALL_MONSTER_NAMES}
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            if discard_invalid:
                continue
            raise ValueError("Названия целей должны быть непустыми строками.")
        normalized = value.strip()
        folded = normalized.casefold()
        if require_known:
            canonical = known.get(folded)
            if canonical is None:
                if discard_invalid:
                    continue
                raise ValueError(f"Неизвестная цель: {normalized}")
            normalized = canonical
            folded = canonical.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(normalized)
    return (
        tuple(target for target in ALL_MONSTER_NAMES if target.casefold() in seen)
        if require_known
        else tuple(result)
    )


@dataclass(frozen=True, slots=True)
class FarmerSettings:
    cycles_count: int = DEFAULT_CYCLES_COUNT
    moves_per_cycle_min: int = DEFAULT_MOVES_PER_CYCLE_MIN
    moves_per_cycle_max: int = DEFAULT_MOVES_PER_CYCLE_MAX

    enabled_targets: tuple[str, ...] = ALL_MONSTER_NAMES
    treatment_enemy_targets: tuple[str, ...] = ()

    heal_threshold: int = DEFAULT_HEAL_THRESHOLD
    battle_start_hp_percent: int = DEFAULT_BATTLE_START_HP_PERCENT
    combat_planner_mode: CombatPlannerMode = parse_combat_planner_mode(DEFAULT_COMBAT_PLANNER_MODE)
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

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cycles_count",
            bounded_integer(self.cycles_count, maximum=MAX_CYCLES_COUNT),
        )
        minimum_moves = bounded_integer(
            self.moves_per_cycle_min,
            maximum=MAX_MOVES_PER_CYCLE,
        )
        maximum_moves = bounded_integer(
            self.moves_per_cycle_max,
            maximum=MAX_MOVES_PER_CYCLE,
        )
        if maximum_moves < minimum_moves:
            raise ValueError("Максимум перемещений не должен быть меньше минимума.")
        object.__setattr__(self, "moves_per_cycle_min", minimum_moves)
        object.__setattr__(self, "moves_per_cycle_max", maximum_moves)
        object.__setattr__(
            self,
            "enabled_targets",
            _canonical_target_names(self.enabled_targets, require_known=True),
        )
        object.__setattr__(
            self,
            "treatment_enemy_targets",
            _canonical_target_names(
                self.treatment_enemy_targets,
                require_known=False,
            ),
        )
        object.__setattr__(
            self,
            "heal_threshold",
            bounded_integer(self.heal_threshold, maximum=MAX_HEAL_THRESHOLD),
        )
        battle_start_hp = bounded_integer(
            self.battle_start_hp_percent,
            maximum=100,
        )
        if battle_start_hp not in {50, 100}:
            raise ValueError("Начальный HP должен быть 50 или 100 процентов.")
        object.__setattr__(self, "battle_start_hp_percent", battle_start_hp)
        if (
            not isinstance(self.combat_planner_mode, str)
            or self.combat_planner_mode.strip().casefold() not in COMBAT_PLANNER_MODES
        ):
            raise ValueError("Неизвестный режим боевого планировщика.")
        object.__setattr__(
            self,
            "combat_planner_mode",
            parse_combat_planner_mode(self.combat_planner_mode),
        )
        if type(self.blessing_enabled) is not bool:
            raise ValueError("Благословение должно быть включено или выключено.")

        for kind in DelayKind:
            minimum_key = f"{kind}_min"
            maximum_key = f"{kind}_max"
            minimum, maximum = finite_range(
                getattr(self, minimum_key),
                getattr(self, maximum_key),
                limit=kind.limit_seconds,
            )
            object.__setattr__(self, minimum_key, minimum)
            object.__setattr__(self, maximum_key, maximum)
        chance = finite_number(self.long_pause_chance)
        finite_range(chance, chance, limit=1.0)
        object.__setattr__(self, "long_pause_chance", chance)


class SettingsService:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._snapshot = FarmerSettings()
        self._mutation_lock = asyncio.Lock()

    @property
    def snapshot(self) -> FarmerSettings:
        return self._snapshot

    @property
    def values(self) -> FarmerSettings:
        """Compatibility read view; settings are published as immutable snapshots."""
        return self._snapshot

    async def load(self) -> None:
        async with self._mutation_lock:
            stored = await self.storage.get_settings()
            candidate = self._decode_snapshot(stored)
            await self.storage.set_and_delete_settings(
                self._storage_values(candidate),
                set(stored) & DEPRECATED_SETTING_KEYS,
            )
            self._snapshot = candidate

    @staticmethod
    def _storage_values(values: FarmerSettings) -> dict[str, JsonValue]:
        return {
            "cycles_count": values.cycles_count,
            "moves_per_cycle_min": values.moves_per_cycle_min,
            "moves_per_cycle_max": values.moves_per_cycle_max,
            "enabled_targets": list(values.enabled_targets),
            "treatment_enemy_targets": list(values.treatment_enemy_targets),
            "heal_threshold": values.heal_threshold,
            "battle_start_hp_percent": values.battle_start_hp_percent,
            "combat_planner_mode": values.combat_planner_mode,
            "blessing_enabled": values.blessing_enabled,
            "move_delay_min": values.move_delay_min,
            "move_delay_max": values.move_delay_max,
            "attack_delay_min": values.attack_delay_min,
            "attack_delay_max": values.attack_delay_max,
            "target_delay_min": values.target_delay_min,
            "target_delay_max": values.target_delay_max,
            "skill_delay_min": values.skill_delay_min,
            "skill_delay_max": values.skill_delay_max,
            "long_pause_chance": values.long_pause_chance,
            "long_pause_min": values.long_pause_min,
            "long_pause_max": values.long_pause_max,
            "cycle_rest_min": values.cycle_rest_min,
            "cycle_rest_max": values.cycle_rest_max,
        }

    @classmethod
    def _decode_snapshot(cls, stored: Mapping[str, JsonValue]) -> FarmerSettings:
        defaults = FarmerSettings()

        def bounded(key: str, maximum: int, default: int) -> int:
            try:
                return bounded_integer(stored.get(key, default), maximum=maximum)
            except ValueError:
                return default

        minimum_moves_raw: object = stored.get(
            "moves_per_cycle_min",
            defaults.moves_per_cycle_min,
        )
        maximum_moves_raw: object = stored.get(
            "moves_per_cycle_max",
            defaults.moves_per_cycle_max,
        )
        if (
            "moves_per_cycle_min" not in stored
            and "moves_per_cycle_max" not in stored
            and "moves_per_cycle" in stored
        ):
            try:
                previous = bounded_integer(
                    stored["moves_per_cycle"],
                    maximum=MAX_MOVES_PER_CYCLE,
                )
            except ValueError:
                previous = 100
            minimum_moves_raw = max(1, previous - 20)
            maximum_moves_raw = min(MAX_MOVES_PER_CYCLE, previous + 20)
        try:
            minimum_moves = bounded_integer(
                minimum_moves_raw,
                maximum=MAX_MOVES_PER_CYCLE,
            )
            maximum_moves = bounded_integer(
                maximum_moves_raw,
                maximum=MAX_MOVES_PER_CYCLE,
            )
            cls.validate_moves_range(minimum_moves, maximum_moves)
        except ValueError:
            minimum_moves = defaults.moves_per_cycle_min
            maximum_moves = defaults.moves_per_cycle_max

        enabled_raw = stored.get("enabled_targets", list(defaults.enabled_targets))
        enabled_targets = (
            _canonical_target_names(
                enabled_raw,
                require_known=True,
                discard_invalid=True,
            )
            if isinstance(enabled_raw, list)
            else defaults.enabled_targets
        )
        treatment_raw = stored.get("treatment_enemy_targets", [])
        treatment_targets = (
            _canonical_target_names(
                treatment_raw,
                require_known=False,
                discard_invalid=True,
            )
            if isinstance(treatment_raw, list)
            else defaults.treatment_enemy_targets
        )

        battle_start_hp = bounded(
            "battle_start_hp_percent",
            100,
            defaults.battle_start_hp_percent,
        )
        if battle_start_hp not in {50, 100}:
            battle_start_hp = defaults.battle_start_hp_percent

        try:
            long_pause_chance = finite_number(
                stored.get("long_pause_chance", defaults.long_pause_chance)
            )
            finite_range(long_pause_chance, long_pause_chance, limit=1.0)
        except ValueError:
            long_pause_chance = defaults.long_pause_chance

        delays: dict[DelayKind, tuple[float, float]] = {}
        for kind in DelayKind:
            default_minimum, default_maximum = cls._delay_range(defaults, kind)
            try:
                delays[kind] = finite_range(
                    stored.get(f"{kind}_min", default_minimum),
                    stored.get(f"{kind}_max", default_maximum),
                    limit=kind.limit_seconds,
                )
            except ValueError:
                delays[kind] = (default_minimum, default_maximum)

        return FarmerSettings(
            cycles_count=bounded(
                "cycles_count",
                MAX_CYCLES_COUNT,
                defaults.cycles_count,
            ),
            moves_per_cycle_min=minimum_moves,
            moves_per_cycle_max=maximum_moves,
            enabled_targets=enabled_targets,
            treatment_enemy_targets=treatment_targets,
            heal_threshold=bounded(
                "heal_threshold",
                MAX_HEAL_THRESHOLD,
                defaults.heal_threshold,
            ),
            battle_start_hp_percent=battle_start_hp,
            combat_planner_mode=parse_combat_planner_mode(
                stored.get("combat_planner_mode"),
                default=defaults.combat_planner_mode,
            ),
            blessing_enabled=cls._normalize_bool(
                stored.get("blessing_enabled", defaults.blessing_enabled),
                default=defaults.blessing_enabled,
            ),
            move_delay_min=delays[DelayKind.MOVE][0],
            move_delay_max=delays[DelayKind.MOVE][1],
            attack_delay_min=delays[DelayKind.ATTACK][0],
            attack_delay_max=delays[DelayKind.ATTACK][1],
            target_delay_min=delays[DelayKind.TARGET][0],
            target_delay_max=delays[DelayKind.TARGET][1],
            skill_delay_min=delays[DelayKind.SKILL][0],
            skill_delay_max=delays[DelayKind.SKILL][1],
            long_pause_chance=long_pause_chance,
            long_pause_min=delays[DelayKind.LONG_PAUSE][0],
            long_pause_max=delays[DelayKind.LONG_PAUSE][1],
            cycle_rest_min=delays[DelayKind.CYCLE_REST][0],
            cycle_rest_max=delays[DelayKind.CYCLE_REST][1],
        )

    def run_policy(self) -> RunPolicy:
        values = self.values
        return RunPolicy(cycles_count=values.cycles_count)

    def target_policy(self) -> TargetPolicy:
        values = self.values
        return TargetPolicy(enabled=tuple(values.enabled_targets))

    def runtime_timing_policy(self) -> RuntimeTimingPolicy:
        values = self.values
        return RuntimeTimingPolicy(
            long_pause=DelayRange(values.long_pause_min, values.long_pause_max),
            long_pause_chance=values.long_pause_chance,
            cycle_rest=DelayRange(values.cycle_rest_min, values.cycle_rest_max),
        )

    def legacy_map_policy(self) -> LegacyMapPolicy:
        values = self.values
        return LegacyMapPolicy(
            moves_per_cycle=IntegerRange(
                values.moves_per_cycle_min,
                values.moves_per_cycle_max,
            ),
            blessing_enabled=values.blessing_enabled,
            move_delay=DelayRange(values.move_delay_min, values.move_delay_max),
            target_selection_delay=DelayRange(
                values.target_delay_min,
                values.target_delay_max,
            ),
            open_attack_delay=DelayRange(
                values.attack_delay_min,
                values.attack_delay_max,
            ),
        )

    def legacy_combat_policy(self) -> LegacyCombatPolicy:
        values = self.values
        return LegacyCombatPolicy(
            treatment_enemies=tuple(values.treatment_enemy_targets),
            heal_threshold=values.heal_threshold,
            battle_start_hp_percent=values.battle_start_hp_percent,
            planner_mode=values.combat_planner_mode,
            target_selection_delay=DelayRange(
                values.target_delay_min,
                values.target_delay_max,
            ),
            skill_delay=DelayRange(values.skill_delay_min, values.skill_delay_max),
        )

    async def set_value(self, key: str, value: object) -> None:
        """Validated compatibility API; paired ranges are written together."""
        async with self._mutation_lock:
            await self._set_value_unlocked(key, value)

    async def _set_value_unlocked(self, key: str, value: object) -> None:
        if key not in FarmerSettings.__dataclass_fields__:
            raise KeyError(key)
        for kind in DelayKind:
            minimum, maximum = self.get_delay_range(kind)
            if key == f"{kind}_min":
                await self._set_delay_range_unlocked(kind, finite_number(value), maximum)
                return
            if key == f"{kind}_max":
                await self._set_delay_range_unlocked(kind, minimum, finite_number(value))
                return
        if key in {"moves_per_cycle_min", "moves_per_cycle_max"}:
            number = bounded_integer(value, maximum=MAX_MOVES_PER_CYCLE)
            minimum_moves = (
                number if key.endswith("_min") else self.values.moves_per_cycle_min
            )
            maximum_moves = (
                number if key.endswith("_max") else self.values.moves_per_cycle_max
            )
            await self._set_moves_range_unlocked(minimum_moves, maximum_moves)
            return
        if key == "cycles_count":
            candidate = replace(
                self._snapshot,
                cycles_count=bounded_integer(value, maximum=MAX_CYCLES_COUNT),
            )
        elif key == "heal_threshold":
            candidate = replace(
                self._snapshot,
                heal_threshold=bounded_integer(value, maximum=MAX_HEAL_THRESHOLD),
            )
        elif key == "long_pause_chance":
            chance = finite_number(value)
            finite_range(chance, chance, limit=1.0)
            candidate = replace(self._snapshot, long_pause_chance=chance)
        elif key == "battle_start_hp_percent":
            hp_percent = bounded_integer(value, maximum=100)
            if hp_percent not in {50, 100}:
                raise ValueError("Начальный HP должен быть 50 или 100 процентов.")
            candidate = replace(
                self._snapshot,
                battle_start_hp_percent=hp_percent,
            )
        elif key == "combat_planner_mode":
            if not isinstance(value, str) or value.strip().casefold() not in COMBAT_PLANNER_MODES:
                raise ValueError("Неизвестный режим боевого планировщика.")
            candidate = replace(
                self._snapshot,
                combat_planner_mode=parse_combat_planner_mode(value),
            )
        elif key == "blessing_enabled":
            if not isinstance(value, bool):
                raise ValueError("Благословение должно быть включено или выключено.")
            candidate = replace(self._snapshot, blessing_enabled=value)
        elif key == "enabled_targets":
            if not isinstance(value, list):
                raise ValueError("Нужен список названий целей.")
            candidate = replace(
                self._snapshot,
                enabled_targets=_canonical_target_names(value, require_known=True),
            )
        elif key == "treatment_enemy_targets":
            if not isinstance(value, list):
                raise ValueError("Нужен список названий целей.")
            candidate = replace(
                self._snapshot,
                treatment_enemy_targets=_canonical_target_names(
                    value,
                    require_known=False,
                ),
            )
        else:
            raise AssertionError(f"Необработанная настройка: {key}")
        stored_value = self._storage_values(candidate)[key]
        await self.storage.set_setting(key, stored_value)
        self._snapshot = candidate

    async def set_cycles_count(self, value: int) -> None:
        await self.set_value("cycles_count", value)

    async def set_heal_threshold(self, value: int) -> None:
        await self.set_value("heal_threshold", value)

    async def set_long_pause_chance(self, value: float) -> None:
        await self.set_value("long_pause_chance", value)

    def get_delay_range(self, kind: DelayKind) -> tuple[float, float]:
        return self._delay_range(self.values, kind)

    @staticmethod
    def _delay_range(s: FarmerSettings, kind: DelayKind) -> tuple[float, float]:
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
        async with self._mutation_lock:
            await self._set_delay_range_unlocked(kind, minimum, maximum)

    async def _set_delay_range_unlocked(
        self, kind: DelayKind, minimum: float, maximum: float
    ) -> None:
        low, high = finite_range(minimum, maximum, limit=kind.limit_seconds)
        minimum_key = f"{kind}_min"
        maximum_key = f"{kind}_max"
        if kind is DelayKind.MOVE:
            candidate = replace(self._snapshot, move_delay_min=low, move_delay_max=high)
        elif kind is DelayKind.ATTACK:
            candidate = replace(
                self._snapshot,
                attack_delay_min=low,
                attack_delay_max=high,
            )
        elif kind is DelayKind.TARGET:
            candidate = replace(
                self._snapshot,
                target_delay_min=low,
                target_delay_max=high,
            )
        elif kind is DelayKind.SKILL:
            candidate = replace(
                self._snapshot,
                skill_delay_min=low,
                skill_delay_max=high,
            )
        elif kind is DelayKind.LONG_PAUSE:
            candidate = replace(
                self._snapshot,
                long_pause_min=low,
                long_pause_max=high,
            )
        else:
            candidate = replace(
                self._snapshot,
                cycle_rest_min=low,
                cycle_rest_max=high,
            )
        stored = self._storage_values(candidate)
        await self.storage.set_settings(
            {
                minimum_key: stored[minimum_key],
                maximum_key: stored[maximum_key],
            }
        )
        self._snapshot = candidate

    async def set_moves_range(self, minimum: int, maximum: int) -> None:
        async with self._mutation_lock:
            await self._set_moves_range_unlocked(minimum, maximum)

    async def _set_moves_range_unlocked(self, minimum: int, maximum: int) -> None:
        self.validate_moves_range(minimum, maximum)
        candidate = replace(
            self._snapshot,
            moves_per_cycle_min=minimum,
            moves_per_cycle_max=maximum,
        )
        await self.storage.set_settings(
            {
                "moves_per_cycle_min": candidate.moves_per_cycle_min,
                "moves_per_cycle_max": candidate.moves_per_cycle_max,
            }
        )
        self._snapshot = candidate

    async def toggle_blessing(self) -> bool:
        async with self._mutation_lock:
            enabled = not self.values.blessing_enabled
            await self._set_value_unlocked("blessing_enabled", enabled)
            return enabled

    async def cycle_combat_planner_mode(self) -> CombatPlannerMode:
        async with self._mutation_lock:
            modes = COMBAT_PLANNER_MODES
            current = self.values.combat_planner_mode
            try:
                next_index = (modes.index(current) + 1) % len(modes)
            except ValueError:
                next_index = 0
            selected = modes[next_index]
            await self._set_value_unlocked("combat_planner_mode", selected)
            return selected

    async def add_treatment_enemy_target(self, target: str) -> bool:
        if not isinstance(target, str):
            raise ValueError("Название цели должно быть строкой.")
        async with self._mutation_lock:
            normalized = target.strip()
            known = {item.casefold() for item in self.values.treatment_enemy_targets}
            if not normalized or normalized.casefold() in known:
                return False
            updated = [*self.values.treatment_enemy_targets, normalized]
            await self._set_value_unlocked("treatment_enemy_targets", updated)
            return True

    async def remove_treatment_enemy_target(self, target: str) -> bool:
        if not isinstance(target, str):
            raise ValueError("Название цели должно быть строкой.")
        async with self._mutation_lock:
            normalized = target.strip().casefold()
            updated = [
                item for item in self.values.treatment_enemy_targets
                if item.casefold() != normalized
            ]
            if len(updated) == len(self.values.treatment_enemy_targets):
                return False
            await self._set_value_unlocked("treatment_enemy_targets", updated)
            return True

    async def toggle_target(self, target: str) -> bool:
        if target not in ALL_MONSTER_NAMES:
            raise ValueError(f"Неизвестный моб: {target}")
        async with self._mutation_lock:
            targets = list(self.values.enabled_targets)
            if target in targets:
                targets.remove(target)
                enabled = False
            else:
                targets.append(target)
                enabled = True
            await self._set_value_unlocked(
                "enabled_targets",
                list(self._sort_targets(targets)),
            )
            return enabled

    async def set_category_enabled(self, category: str, enabled: bool) -> list[str]:
        if type(enabled) is not bool:
            raise ValueError("Состояние категории должно быть bool.")
        category_targets = get_monster_names(category)
        if not category_targets:
            raise ValueError(f"Неизвестная категория мобов: {category}")
        async with self._mutation_lock:
            selected = set(self.values.enabled_targets)
            if enabled:
                selected.update(category_targets)
            else:
                selected.difference_update(category_targets)
            updated = self._sort_targets(list(selected))
            await self._set_value_unlocked("enabled_targets", list(updated))
            return list(updated)

    def get_category_enabled_count(self, category: str) -> tuple[int, int]:
        category_targets = get_monster_names(category)
        selected = set(self.values.enabled_targets)

        enabled_count = sum(target in selected for target in category_targets)

        return enabled_count, len(category_targets)

    @staticmethod
    def _sort_targets(targets: list[str]) -> tuple[str, ...]:
        return _canonical_target_names(targets, require_known=True)

    @staticmethod
    def _normalize_bool(value: object, *, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if type(value) is int and value in (0, 1):
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
