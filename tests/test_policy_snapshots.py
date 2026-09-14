from __future__ import annotations

import asyncio
import math
import sqlite3
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from automation_policy import (
    DelayRange,
    IntegerRange,
    LegacyCombatPolicy,
    LegacyMapPolicy,
    RunPolicy,
    RuntimeTimingPolicy,
    TargetPolicy,
    parse_combat_planner_mode,
)
from game_catalog import ALL_MONSTER_NAMES, LOCATION_NAMES
from settings_service import FarmerSettings, SettingsService
from storage import Storage


class PolicyViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = Storage(Path(":memory:"))
        self.settings = SettingsService(self.storage)
        await self.settings.load()

    async def asyncTearDown(self) -> None:
        await self.storage.close()

    def views(self) -> tuple[object, ...]:
        return (
            self.settings.run_policy(),
            self.settings.target_policy(),
            self.settings.runtime_timing_policy(),
            self.settings.legacy_map_policy(),
            self.settings.legacy_combat_policy(),
        )

    async def test_views_are_independent_frozen_decision_snapshots(self) -> None:
        await self.settings.add_treatment_enemy_target("Фонарщик")
        before = self.views()
        run, targets, runtime, legacy_map, combat = before
        self.assertIsInstance(run, RunPolicy)
        self.assertIsInstance(targets, TargetPolicy)
        self.assertIsInstance(runtime, RuntimeTimingPolicy)
        self.assertIsInstance(legacy_map, LegacyMapPolicy)
        self.assertIsInstance(combat, LegacyCombatPolicy)

        await self.settings.toggle_target(targets.enabled[0])
        await self.settings.remove_treatment_enemy_target("Фонарщик")
        await self.settings.set_heal_threshold(123)
        await self.settings.set_cycles_count(2)

        self.assertEqual(run.cycles_count, FarmerSettings().cycles_count)
        self.assertEqual(combat.treatment_enemies, ("Фонарщик",))
        self.assertEqual(combat.heal_threshold, FarmerSettings().heal_threshold)
        self.assertNotEqual(self.views(), before)
        with self.assertRaises(FrozenInstanceError):
            runtime.long_pause.minimum = 100.0
        with self.assertRaises(FrozenInstanceError):
            targets.enabled = ()

    async def test_settings_snapshot_is_frozen_detached_and_read_only(self) -> None:
        source = [ALL_MONSTER_NAMES[0]]
        await self.settings.set_value("enabled_targets", source)
        published = self.settings.snapshot

        source.clear()

        self.assertIs(self.settings.values, published)
        self.assertEqual(published.enabled_targets, (ALL_MONSTER_NAMES[0],))
        self.assertEqual(
            await self.storage.get_setting("enabled_targets"),
            [ALL_MONSTER_NAMES[0]],
        )
        self.assertIsInstance(published.enabled_targets, tuple)
        snapshot_field = "cycles_count"
        with self.assertRaises(FrozenInstanceError):
            setattr(published, snapshot_field, 2)
        service_property = "values"
        with self.assertRaises(AttributeError):
            setattr(self.settings, service_property, FarmerSettings())

    async def test_successful_write_replaces_snapshot_without_mutating_old_alias(self) -> None:
        before = self.settings.snapshot

        await self.settings.set_cycles_count(before.cycles_count + 1)

        after = self.settings.snapshot
        self.assertIsNot(after, before)
        self.assertEqual(before.cycles_count, FarmerSettings().cycles_count)
        self.assertEqual(after.cycles_count, before.cycles_count + 1)

    async def test_future_settings_survive_load_and_known_values_are_normalized(self) -> None:
        future = {"rules_version": 9, "options": ["future-option", {"enabled": True}]}
        await self.storage.set_settings({
            "future.combat_settings": future,
            "combat_planner_mode": " ACTIVE ",
            "activity_profile": "old",
        })
        await self.settings.load()
        self.assertEqual(await self.storage.get_setting("future.combat_settings"), future)
        self.assertEqual(self.settings.legacy_combat_policy().planner_mode, "active")
        self.assertNotIn("activity_profile", await self.storage.get_settings())
        writes = self.storage.connection.total_changes
        await self.settings.load()
        self.assertEqual(self.storage.connection.total_changes, writes)

    async def test_only_explicit_legacy_move_setting_is_migrated(self) -> None:
        self.storage.connection.execute("DELETE FROM settings")
        self.storage.connection.commit()
        await self.storage.set_settings({"moves_per_cycle": 100, "future.limit": 5})
        await self.settings.load()
        moves = self.settings.legacy_map_policy().moves_per_cycle
        self.assertEqual((moves.minimum, moves.maximum), (80, 120))
        self.assertNotIn("moves_per_cycle", await self.storage.get_settings())
        self.assertEqual(await self.storage.get_setting("future.limit"), 5)

    async def test_mode_parser_and_service_share_canonical_modes(self) -> None:
        self.assertEqual(parse_combat_planner_mode(" GuArDeD "), "guarded")
        self.assertEqual(parse_combat_planner_mode("future-mode"), "shadow")
        with self.assertRaises(ValueError):
            parse_combat_planner_mode("unknown", default="future")
        await self.settings.set_value("combat_planner_mode", " ACTIVE ")
        self.assertEqual(self.settings.legacy_combat_policy().planner_mode, "active")
        with self.assertRaises(ValueError):
            await self.settings.set_value("combat_planner_mode", "future-mode")
        self.assertEqual(await self.settings.cycle_combat_planner_mode(), "shadow")

    async def test_failed_mutations_never_publish_unsaved_views(self) -> None:
        await self.settings.add_treatment_enemy_target("Моль")
        before = self.views()
        snapshot_before = self.settings.snapshot
        persisted = await self.storage.get_settings()
        self.storage.connection.executescript("""
            CREATE TRIGGER reject_setting BEFORE UPDATE ON settings
            BEGIN SELECT RAISE(ABORT, 'settings write failed'); END;
        """)
        operations = (
            self.settings.toggle_blessing,
            self.settings.cycle_combat_planner_mode,
            lambda: self.settings.add_treatment_enemy_target("Фонарщик"),
            lambda: self.settings.remove_treatment_enemy_target("Моль"),
            lambda: self.settings.toggle_target(ALL_MONSTER_NAMES[0]),
            lambda: self.settings.set_category_enabled(LOCATION_NAMES[0], False),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(sqlite3.IntegrityError):
                    await operation()
                self.assertIs(self.settings.snapshot, snapshot_before)
                self.assertEqual(self.views(), before)
                self.assertEqual(await self.storage.get_settings(), persisted)

    async def test_pending_and_concurrent_toggles_publish_only_committed_values(self) -> None:
        before = self.views()
        snapshot_before = self.settings.snapshot
        await self.storage.lock.acquire()
        first = asyncio.create_task(self.settings.toggle_blessing())
        second = asyncio.create_task(self.settings.toggle_blessing())
        try:
            await asyncio.sleep(0)
            self.assertIs(self.settings.snapshot, snapshot_before)
            self.assertEqual(self.views(), before)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
        finally:
            self.storage.lock.release()
        self.assertEqual(await asyncio.gather(first, second), [True, False])
        self.assertIsNot(self.settings.snapshot, snapshot_before)
        self.assertFalse(snapshot_before.blessing_enabled)
        self.assertEqual(self.views(), before)
        self.assertFalse(await self.storage.get_setting("blessing_enabled"))

    async def test_load_persists_normalization_and_deprecation_in_one_transaction(self) -> None:
        before = self.views()
        await self.storage.set_settings({"heal_threshold": 123, "activity_profile": "old"})
        persisted_before_load = await self.storage.get_settings()
        self.storage.connection.executescript("""
            CREATE TRIGGER reject_deprecated BEFORE DELETE ON settings
            BEGIN SELECT RAISE(ABORT, 'delete failed'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.settings.load()
        self.assertEqual(self.views(), before)
        self.assertEqual(await self.storage.get_settings(), persisted_before_load)
        self.assertFalse(self.storage.connection.in_transaction)

    async def test_failed_atomic_load_keeps_previously_published_views(self) -> None:
        await self.settings.set_heal_threshold(123)
        before = self.views()
        snapshot_before = self.settings.snapshot
        with patch.object(
            self.storage,
            "set_and_delete_settings",
            side_effect=sqlite3.OperationalError("write failed"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                await self.settings.load()
        self.assertIs(self.settings.snapshot, snapshot_before)
        self.assertEqual(self.views(), before)

    async def test_target_inputs_are_trimmed_casefold_deduplicated_and_strict(self) -> None:
        await self.settings.set_value(
            "treatment_enemy_targets",
            ["  Моль ", "моль", " Фонарщик "],
        )
        self.assertEqual(
            self.settings.legacy_combat_policy().treatment_enemies,
            ("Моль", "Фонарщик"),
        )
        await self.settings.set_value(
            "enabled_targets",
            [f" {ALL_MONSTER_NAMES[0].swapcase()} ", ALL_MONSTER_NAMES[0]],
        )
        self.assertEqual(self.settings.target_policy().enabled, (ALL_MONSTER_NAMES[0],))
        for invalid in ([""], [12], [{"unexpected": True}]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await self.settings.set_value("treatment_enemy_targets", invalid)
        with self.assertRaises(ValueError):
            await self.settings.set_category_enabled(LOCATION_NAMES[0], 1)

    async def test_corrupt_persisted_targets_and_boole_are_safely_normalized(self) -> None:
        self.storage.connection.executemany(
            """
            INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
            """,
            (
                ("blessing_enabled", "-7", "now"),
                (
                    "treatment_enemy_targets",
                    '[{"unexpected":true},"  Моль ","моль",""]',
                    "now",
                ),
            ),
        )
        self.storage.connection.commit()
        await self.settings.load()
        self.assertFalse(self.settings.legacy_map_policy().blessing_enabled)
        self.assertEqual(
            self.settings.legacy_combat_policy().treatment_enemies,
            ("Моль",),
        )

    async def test_category_result_cannot_mutate_published_settings(self) -> None:
        returned = await self.settings.set_category_enabled(LOCATION_NAMES[0], False)
        before = self.settings.target_policy()
        returned.clear()
        self.assertEqual(self.settings.target_policy(), before)
        self.assertEqual(
            await self.storage.get_setting("enabled_targets"),
            list(before.enabled),
        )


class PolicyRuntimeValidationTests(unittest.TestCase):
    def test_public_policy_constructors_reject_invalid_scalars(self) -> None:
        for value in (0, -1, True, False, 1.5, "1"):
            with self.subTest(cycles_count=value), self.assertRaises(ValueError):
                RunPolicy(value)

        valid_delay = DelayRange(0, 1)
        combat = LegacyCombatPolicy((), 100, 50, "shadow", valid_delay, valid_delay)
        for fields in (
            {"heal_threshold": 0},
            {"heal_threshold": True},
            {"battle_start_hp_percent": 75},
            {"battle_start_hp_percent": 50.0},
            {"planner_mode": " ACTIVE "},
            {"planner_mode": "unknown"},
            {"target_selection_delay": None},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(combat, **fields)

        legacy_map = LegacyMapPolicy(
            moves_per_cycle=IntegerRange(1, 2),
            blessing_enabled=False,
            move_delay=valid_delay,
            open_attack_delay=valid_delay,
            target_selection_delay=valid_delay,
        )
        for invalid in (1, 0, "false", None):
            with self.subTest(blessing=invalid), self.assertRaises(ValueError):
                replace(legacy_map, blessing_enabled=invalid)

    def test_names_are_canonical_and_nested_sections_are_typed(self) -> None:
        names = [" Моль ", "моль", " Фонарщик "]
        policy = TargetPolicy(names)
        names.clear()
        self.assertEqual(policy.enabled, ("Моль", "Фонарщик"))
        with self.assertRaises(ValueError):
            TargetPolicy("Моль")
        with self.assertRaises(ValueError):
            TargetPolicy((" ",))
        with self.assertRaises(ValueError):
            RuntimeTimingPolicy(None, 0.1, DelayRange(1, 2))

    def test_ranges_reject_nonfinite_reversed_bool_and_string_values(self) -> None:
        for minimum, maximum in (
            (math.nan, 1),
            (0, math.inf),
            (2, 1),
            (True, 2),
            ("1", 2),
        ):
            with self.subTest(minimum=minimum, maximum=maximum):
                with self.assertRaises(ValueError):
                    DelayRange(minimum, maximum)
        for minimum, maximum in ((0, 1), (True, 2), (2, 1)):
            with self.subTest(minimum=minimum, maximum=maximum):
                with self.assertRaises(ValueError):
                    IntegerRange(minimum, maximum)
        with self.assertRaises(ValueError):
            RuntimeTimingPolicy(DelayRange(0, 1), math.nan, DelayRange(1, 2))


if __name__ == "__main__":
    unittest.main()
