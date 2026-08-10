from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from config import (
    DEFAULT_ATTACK_DELAY_MAX,
    DEFAULT_ATTACK_DELAY_MIN,
    DEFAULT_CYCLE_REST_MAX,
    DEFAULT_CYCLE_REST_MIN,
    DEFAULT_CYCLES_COUNT,
    DEFAULT_HEAL_AMOUNT,
    DEFAULT_HEAL_THRESHOLD,
    DEFAULT_LONG_PAUSE_CHANCE,
    DEFAULT_LONG_PAUSE_MAX,
    DEFAULT_LONG_PAUSE_MIN,
    DEFAULT_MAX_MANA,
    DEFAULT_MOVE_DELAY_MAX,
    DEFAULT_MOVE_DELAY_MIN,
    DEFAULT_MOVES_PER_CYCLE,
    DEFAULT_SKILL_DELAY_MAX,
    DEFAULT_SKILL_DELAY_MIN,
    DEFAULT_TARGET_DELAY_MAX,
    DEFAULT_TARGET_DELAY_MIN,
    DEFAULT_TARGET_MONSTERS,
    TARGET_MONSTER_CATEGORIES,
)
from storage import Storage


@dataclass
class FarmerSettings:
    cycles_count: int = DEFAULT_CYCLES_COUNT
    moves_per_cycle: int = DEFAULT_MOVES_PER_CYCLE

    enabled_targets: list[str] = field(default_factory=lambda: list(DEFAULT_TARGET_MONSTERS))

    heal_threshold: int = DEFAULT_HEAL_THRESHOLD
    max_mana: int = DEFAULT_MAX_MANA
    heal_amount: int = DEFAULT_HEAL_AMOUNT
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

        # Preserve the old effective behaviour during an upgrade. Previously
        # healing started at max_hp - heal_amount; the user can then adjust the
        # resulting explicit threshold directly in the control bot.
        if "heal_threshold" not in stored and "max_hp" in stored:
            try:
                stored["heal_threshold"] = max(
                    1,
                    int(stored["max_hp"]) - int(stored.get("heal_amount", DEFAULT_HEAL_AMOUNT)),
                )
            except (TypeError, ValueError):
                stored["heal_threshold"] = DEFAULT_HEAL_THRESHOLD

        for key, value in stored.items():
            if hasattr(self.values, key):
                setattr(self.values, key, value)

        self._normalize_enabled_targets()
        self._normalize_character()
        self.values.blessing_enabled = self._normalize_bool(
            self.values.blessing_enabled,
            default=False,
        )
        await self.save_all()

    async def save_all(self) -> None:
        for key, value in asdict(self.values).items():
            await self.storage.set_setting(key, value)

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

    async def toggle_target(self, target: str) -> bool:
        if target not in DEFAULT_TARGET_MONSTERS:
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
        category_targets = TARGET_MONSTER_CATEGORIES.get(category)

        if category_targets is None:
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

    async def toggle_category(self, category: str) -> bool:
        category_targets = TARGET_MONSTER_CATEGORIES.get(category)

        if category_targets is None:
            raise ValueError(f"Неизвестная категория мобов: {category}")

        selected = set(self.values.enabled_targets or [])
        category_fully_enabled = all(target in selected for target in category_targets)
        new_enabled_state = not category_fully_enabled

        await self.set_category_enabled(
            category,
            new_enabled_state,
        )

        return new_enabled_state

    def is_category_enabled(self, category: str) -> bool:
        category_targets = TARGET_MONSTER_CATEGORIES.get(category)

        if not category_targets:
            return False

        selected = set(self.values.enabled_targets or [])

        return all(target in selected for target in category_targets)

    def is_category_partially_enabled(self, category: str) -> bool:
        category_targets = TARGET_MONSTER_CATEGORIES.get(category)

        if not category_targets:
            return False

        selected = set(self.values.enabled_targets or [])
        enabled_count = sum(target in selected for target in category_targets)

        return 0 < enabled_count < len(category_targets)

    def get_category_enabled_count(self, category: str) -> tuple[int, int]:
        category_targets = TARGET_MONSTER_CATEGORIES.get(category, [])
        selected = set(self.values.enabled_targets or [])

        enabled_count = sum(target in selected for target in category_targets)

        return enabled_count, len(category_targets)

    def _normalize_enabled_targets(self) -> None:
        self.values.enabled_targets = self._coerce_enabled_targets(self.values.enabled_targets)

    @classmethod
    def _coerce_enabled_targets(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return list(DEFAULT_TARGET_MONSTERS)
        return cls._sort_targets([target for target in value if isinstance(target, str)])

    @staticmethod
    def _sort_targets(targets: list[str]) -> list[str]:
        selected = set(targets)

        return [target for target in DEFAULT_TARGET_MONSTERS if target in selected]

    def _normalize_character(self) -> None:
        for key in ("heal_threshold", "max_mana", "heal_amount"):
            value = getattr(self.values, key)
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = 1
            setattr(self.values, key, max(1, value))

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
