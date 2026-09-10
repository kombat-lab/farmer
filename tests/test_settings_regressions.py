from __future__ import annotations

import math
import sqlite3
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from control_bot import ControlBot
from human_delays import ActivityBreakPlanner, HumanDelayModel
from settings_service import (
    MAX_CYCLES_COUNT,
    MAX_HEAL_THRESHOLD,
    MAX_MOVES_PER_CYCLE,
    DelayKind,
    FarmerSettings,
    SettingsService,
)
from storage import Storage


class SettingsRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.storage = Storage(Path(":memory:"))
        self.settings = SettingsService(self.storage)
        await self.settings.load()

    async def asyncTearDown(self) -> None:
        await self.storage.close()

    async def test_invalid_ui_delays_never_change_settings(self) -> None:
        control = ControlBot(SimpleNamespace(), self.storage, SimpleNamespace(), self.settings)
        control._retry_input = AsyncMock()
        control._finish_input = AsyncMock()
        handler = next(
            item.callback
            for item in control.router.message.handlers
            if item.callback.__name__ == "delay_input"
        )
        state = SimpleNamespace(get_data=AsyncMock(return_value={"input_kind": "delay:cycle_rest"}))
        before = await self.storage.get_settings()
        for raw in ("nan nan", "inf inf", "-inf 1", "1e309 1e309", "1e308 1e308", "0 1441"):
            with self.subTest(raw=raw):
                await handler(SimpleNamespace(text=raw), state)
                self.assertEqual(await self.storage.get_settings(), before)
                self.assertEqual(asdict(self.settings.values), before)
        self.assertEqual(control._retry_input.await_count, 6)
        control._finish_input.assert_not_awaited()
        await handler(SimpleNamespace(text="1,5 2"), state)
        self.assertEqual(self.settings.get_delay_range(DelayKind.CYCLE_REST), (90.0, 120.0))
        control._finish_input.assert_awaited_once()

    async def test_direct_api_rejects_nonfinite_or_out_of_range_values(self) -> None:
        before = await self.storage.get_settings()
        for kind in DelayKind:
            for minimum, maximum in (
                (math.nan, 1.0),
                (0.0, math.nan),
                (math.inf, math.inf),
                (-1.0, 1.0),
                (2.0, 1.0),
                (0.0, kind.limit_seconds + 1.0),
            ):
                with self.subTest(kind=kind, minimum=minimum, maximum=maximum):
                    with self.assertRaises(ValueError):
                        await self.settings.set_delay_range(kind, minimum, maximum)
        for key, invalid in (
            ("move_delay_min", math.nan),
            ("long_pause_chance", math.inf),
            ("cycles_count", MAX_CYCLES_COUNT + 1),
            ("heal_threshold", MAX_HEAL_THRESHOLD + 1),
            ("moves_per_cycle_max", MAX_MOVES_PER_CYCLE + 1),
        ):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    await self.settings.set_value(key, invalid)
        self.assertEqual(await self.storage.get_settings(), before)
        self.assertEqual(asdict(self.settings.values), before)

    async def test_loading_repairs_invalid_persisted_ranges_and_numbers(self) -> None:
        invalid: dict[str, object] = {
            "move_delay_min": math.nan,
            "attack_delay_max": math.inf,
            "target_delay_min": "not a number",
            "skill_delay_max": 301,
            "long_pause_max": 3601,
            "cycle_rest_min": 200.0,
            "cycle_rest_max": 100.0,
            "cycles_count": math.inf,
            "heal_threshold": math.nan,
            "moves_per_cycle_max": math.inf,
            "long_pause_chance": math.nan,
            "farmer_stop_requested": True,
        }
        await self.storage.set_settings(invalid)
        await self.settings.load()
        self.assertEqual(asdict(self.settings.values), asdict(FarmerSettings()))
        repaired = await self.storage.get_settings()
        self.assertTrue(repaired.pop("farmer_stop_requested"))
        self.assertEqual(repaired, asdict(FarmerSettings()))
        writes = self.storage.connection.total_changes
        await self.settings.load()
        self.assertEqual(self.storage.connection.total_changes, writes)

    async def test_pair_write_rolls_back_when_second_setting_fails(self) -> None:
        before = self.settings.get_delay_range(DelayKind.MOVE)
        self.storage.connection.executescript("""
            CREATE TRIGGER reject_move_max BEFORE UPDATE ON settings
            WHEN NEW.key = 'move_delay_max'
            BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.settings.set_delay_range(DelayKind.MOVE, 10.0, 20.0)
        await self.storage.add_event("TEST", "later unrelated commit")
        self.assertEqual(self.settings.get_delay_range(DelayKind.MOVE), before)
        self.assertEqual(await self.storage.get_setting("move_delay_min"), before[0])
        self.assertEqual(await self.storage.get_setting("move_delay_max"), before[1])

    async def test_valid_boundary_values_survive_reload(self) -> None:
        for kind in DelayKind:
            await self.settings.set_delay_range(kind, 0.0, kind.limit_seconds)
        await self.settings.set_cycles_count(MAX_CYCLES_COUNT)
        await self.settings.set_heal_threshold(MAX_HEAL_THRESHOLD)
        await self.settings.set_moves_range(1, MAX_MOVES_PER_CYCLE)
        await self.settings.load()
        for kind in DelayKind:
            self.assertEqual(self.settings.get_delay_range(kind), (0.0, kind.limit_seconds))
        self.assertEqual(self.settings.values.cycles_count, MAX_CYCLES_COUNT)
        self.assertEqual(self.settings.values.moves_per_cycle_max, MAX_MOVES_PER_CYCLE)


class DelayModelRegressionTests(unittest.TestCase):
    def test_models_reject_nonfinite_waits_without_starting_a_timer(self) -> None:
        model = HumanDelayModel()
        planner = ActivityBreakPlanner()
        for invalid in (math.nan, math.inf, -math.inf):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    model.action_delay(invalid, invalid)
                with self.assertRaises(ValueError):
                    planner.duration(0.0, invalid)
                with self.assertRaises(ValueError):
                    model.should_take_long_pause(invalid)


if __name__ == "__main__":
    unittest.main()
