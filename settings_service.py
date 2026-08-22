from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from config import (
    ACTIVITY_PROFILE_FAST,
    ACTIVITY_PROFILE_NORMAL,
    DEFAULT_ACTIVITY_PROFILE,
    DEFAULT_ATTACK_DELAY_MAX,
    DEFAULT_ATTACK_DELAY_MIN,
    DEFAULT_BATTLE_START_HP_PERCENT,
    DEFAULT_CYCLE_REST_MAX,
    DEFAULT_CYCLE_REST_MIN,
    DEFAULT_CYCLES_COUNT,
    DEFAULT_HEAL_THRESHOLD,
    DEFAULT_LONG_PAUSE_CHANCE,
    DEFAULT_LONG_PAUSE_MAX,
    DEFAULT_LONG_PAUSE_MIN,
    DEFAULT_MOVE_DELAY_MAX,
    DEFAULT_MOVE_DELAY_MIN,
    DEFAULT_MOVES_PER_CYCLE,
    DEFAULT_SKILL_DELAY_MAX,
    DEFAULT_SKILL_DELAY_MIN,
    DEFAULT_TARGET_DELAY_MAX,
    DEFAULT_TARGET_DELAY_MIN,
)
from game_catalog import ALL_MONSTER_NAMES, get_monster_names
from storage import Storage

NON_UI_SETTING_KEYS = frozenset({"farmer_stop_requested"})


@dataclass
class FarmerSettings:
    cycles_count: int = DEFAULT_CYCLES_COUNT
    moves_per_cycle: int = DEFAULT_MOVES_PER_CYCLE
    activity_profile: str = DEFAULT_ACTIVITY_PROFILE

    enabled_targets: list[str] = field(default_factory=lambda: list(ALL_MONSTER_NAMES))
    treatment_enemy_targets: list[str] = field(default_factory=list)

    heal_threshold: int = DEFAULT_HEAL_THRESHOLD
    battle_start_hp_percent: int = DEFAULT_BATTLE_START_HP_PERCENT
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

        for key, value in stored.items():
            if hasattr(self.values, key):
                setattr(self.values, key, value)

        self._normalize_enabled_targets()
        self._normalize_treatment_enemy_targets()
        self._normalize_character()
        self._normalize_activity_profile()
        self.values.blessing_enabled = self._normalize_bool(
            self.values.blessing_enabled,
            default=False,
        )
        normalized = asdict(self.values)
        await self.storage.set_settings(normalized)
        await self.storage.delete_settings(
            set(stored) - set(normalized) - NON_UI_SETTING_KEYS
        )

    async def set_value(self, key: str, value: Any) -> None:
        if not hasattr(self.values, key):
            raise KeyError(key)

        setattr(self.values, key, value)
        await self.storage.set_setting(key, value)

    async def toggle_blessing(self) -> bool:
        enabled = not self.values.blessing_enabled
        self.values.blessing_enabled = enabled
        await self.storage.set_setting("blessing_enabled", enabled)
        return enabled

    async def set_activity_profile(self, profile: str) -> str:
        normalized = str(profile).strip().casefold()
        if normalized not in {ACTIVITY_PROFILE_NORMAL, ACTIVITY_PROFILE_FAST}:
            raise ValueError(f"Неизвестный профиль активности: {profile}")
        self.values.activity_profile = normalized
        await self.storage.set_setting("activity_profile", normalized)
        return normalized

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
            threshold = int(self.values.heal_threshold)
        except (TypeError, ValueError):
            threshold = 1
        self.values.heal_threshold = max(1, threshold)

        try:
            battle_start_hp = int(self.values.battle_start_hp_percent)
        except (TypeError, ValueError):
            battle_start_hp = DEFAULT_BATTLE_START_HP_PERCENT
        if battle_start_hp not in {50, 100}:
            battle_start_hp = DEFAULT_BATTLE_START_HP_PERCENT
        self.values.battle_start_hp_percent = battle_start_hp

    def _normalize_activity_profile(self) -> None:
        profile = str(self.values.activity_profile).strip().casefold()
        if profile not in {ACTIVITY_PROFILE_NORMAL, ACTIVITY_PROFILE_FAST}:
            profile = DEFAULT_ACTIVITY_PROFILE
        self.values.activity_profile = profile

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
        if value < 1:
            raise ValueError("Значение должно быть больше нуля.")

    @staticmethod
    def validate_range(minimum: float, maximum: float) -> None:
        if minimum < 0 or maximum < minimum:
            raise ValueError("Минимум должен быть >= 0, максимум >= минимума.")
