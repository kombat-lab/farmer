from __future__ import annotations

from dataclasses import asdict, dataclass

from config import (
    DEFAULT_ATTACK_DELAY_MAX,
    DEFAULT_ATTACK_DELAY_MIN,
    DEFAULT_CYCLES_COUNT,
    DEFAULT_CYCLE_REST_MAX,
    DEFAULT_CYCLE_REST_MIN,
    DEFAULT_LONG_PAUSE_CHANCE,
    DEFAULT_MAX_HP,
    DEFAULT_MAX_MANA,
    DEFAULT_HEAL_AMOUNT,
    DEFAULT_LONG_PAUSE_MAX,
    DEFAULT_LONG_PAUSE_MIN,
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

    enabled_targets: list[str] | None = None

    max_hp: int = DEFAULT_MAX_HP
    max_mana: int = DEFAULT_MAX_MANA
    heal_amount: int = DEFAULT_HEAL_AMOUNT

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
        if self.enabled_targets is None:
            self.enabled_targets = list(DEFAULT_TARGET_MONSTERS)


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
        self._normalize_character()
        await self.save_all()

    async def save_all(self) -> None:
        for key, value in asdict(self.values).items():
            await self.storage.set_setting(key, value)

    async def set_value(self, key: str, value) -> None:
        if not hasattr(self.values, key):
            raise KeyError(key)

        setattr(self.values, key, value)
        await self.storage.set_setting(key, value)

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
                target
                for target in current_targets
                if target not in category_target_set
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
        category_fully_enabled = all(
            target in selected
            for target in category_targets
        )
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

        return all(
            target in selected
            for target in category_targets
        )

    def is_category_partially_enabled(self, category: str) -> bool:
        category_targets = TARGET_MONSTER_CATEGORIES.get(category)

        if not category_targets:
            return False

        selected = set(self.values.enabled_targets or [])
        enabled_count = sum(
            target in selected
            for target in category_targets
        )

        return 0 < enabled_count < len(category_targets)

    def get_category_enabled_count(self, category: str) -> tuple[int, int]:
        category_targets = TARGET_MONSTER_CATEGORIES.get(category, [])
        selected = set(self.values.enabled_targets or [])

        enabled_count = sum(
            target in selected
            for target in category_targets
        )

        return enabled_count, len(category_targets)

    def _normalize_enabled_targets(self) -> None:
        enabled_targets = self.values.enabled_targets

        if not isinstance(enabled_targets, list):
            enabled_targets = list(DEFAULT_TARGET_MONSTERS)

        self.values.enabled_targets = self._sort_targets(enabled_targets)

    @staticmethod
    def _sort_targets(targets: list[str]) -> list[str]:
        selected = set(targets)

        return [
            target
            for target in DEFAULT_TARGET_MONSTERS
            if target in selected
        ]


    def _normalize_character(self) -> None:
        for key in ("max_hp", "max_mana", "heal_amount"):
            value = getattr(self.values, key)
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = 1
            setattr(self.values, key, max(1, value))

    @property
    def heal_threshold(self) -> int:
        return max(1, self.values.max_hp - self.values.heal_amount)

    @staticmethod
    def validate_character_value(value: int) -> None:
        if value < 1:
            raise ValueError("Значение должно быть больше нуля.")

    @staticmethod
    def validate_range(minimum: float, maximum: float) -> None:
        if minimum < 0 or maximum < minimum:
            raise ValueError(
                "Минимум должен быть >= 0, максимум >= минимума."
            )
