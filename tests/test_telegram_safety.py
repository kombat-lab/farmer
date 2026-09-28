from __future__ import annotations

import asyncio
import unittest
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from telethon.errors import BotResponseTimeoutError

from farmer import Farmer
from models import BotState
from storage import Storage
from telegram_safety import (
    RollingAttemptGuard,
    StateRefreshGate,
    TelegramActionLimiter,
    TelegramActionTelemetry,
    message_state_key,
)
from tests.fog_test_fixtures import FakeMessage, make_offline_farmer


class TelegramSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_passive_health_notice_does_not_cancel_map_action(self) -> None:
        map_message = FakeMessage(
            "🗺️ Поляна\nПозиция: (2, 1)\nМонстры на клетке: 0",
            [["➡️"]],
            message_id=10,
        )
        health_message = FakeMessage(
            "❤️ Ваше здоровье полностью восстановлено: 755/755.",
            [],
            message_id=11,
        )
        farmer = make_offline_farmer(AsyncMock(spec=Storage))
        try:
            await farmer.enqueue_message(map_message)
            await farmer.enqueue_message(health_message)
            latest = farmer.latest_received_message
            assert latest is not None
            self.assertEqual(latest.id, map_message.id)
            self.assertTrue(farmer.is_latest_message(map_message))
            self.assertEqual(farmer.ingress.generation, 1)
            self.assertEqual(farmer.ingress.qsize(), 2)
        finally:
            await farmer.stop("test cleanup")

    def test_flood_wait_uses_only_server_delay_and_small_buffer(self) -> None:
        farmer = Farmer.__new__(Farmer)
        farmer.telegram_flood_incidents = deque()

        first_pause, first_count = farmer.flood_wait_pause(3)
        second_pause, second_count = farmer.flood_wait_pause(3)
        third_pause, third_count = farmer.flood_wait_pause(3)

        self.assertEqual((first_pause, first_count), (5.0, 1))
        self.assertEqual((second_pause, second_count), (5.0, 2))
        self.assertEqual((third_pause, third_count), (5.0, 3))

    async def test_callback_timeout_is_recorded_without_retry_pause_or_stop(self) -> None:
        with TemporaryDirectory() as directory:
            message = FakeMessage("🎯 Раунд 8\nХод Kombat", [["Атака"]])
            click_count = 0

            async def click(_row: int, _column: int) -> None:
                nonlocal click_count
                click_count += 1
                raise BotResponseTimeoutError(request=None)

            message.click = click
            storage = Storage(Path(directory) / "test.sqlite3")
            farmer = make_offline_farmer(storage)
            farmer.state = BotState.COMBAT
            farmer.telegram_action_limiter = TelegramActionLimiter(min_interval=0.0)
            try:
                await farmer.enqueue_message(message)
                clicked = await farmer.press_button(message, 0, 0, "атака")
                self.assertFalse(clicked)
                self.assertTrue(farmer.running)
                self.assertEqual(click_count, 1)
                self.assertEqual(farmer.telegram_cooldown_remaining(), 0.0)
                self.assertIsNone(await storage.get_setting("telegram_cooldown_until"))
                repeated = await farmer.press_button(message, 0, 0, "другая атака")
                self.assertFalse(repeated)
                self.assertTrue(farmer.running)
                self.assertEqual(click_count, 1)
            finally:
                await farmer.stop("test cleanup")
                await storage.close()

    async def test_hanging_callback_is_bounded_by_application_timeout(self) -> None:
        message = FakeMessage("🎯 Раунд 8\nХод Kombat", [["Атака"]])
        never_finishes = asyncio.Event()

        async def click(_row: int, _column: int) -> None:
            await never_finishes.wait()

        message.click = click
        with patch("farmer.TELEGRAM_CALLBACK_RPC_TIMEOUT", 0.01):
            farmer = make_offline_farmer(AsyncMock(spec=Storage))
            farmer.telegram_action_limiter = TelegramActionLimiter(min_interval=0.0)
            farmer.record_callback_timeout = AsyncMock()
            try:
                await farmer.enqueue_message(message)
                clicked = await farmer.press_button(message, 0, 0, "атака")
                self.assertFalse(clicked)
                farmer.record_callback_timeout.assert_awaited_once()
            finally:
                await farmer.stop("test cleanup")

    def test_outgoing_action_telemetry_uses_only_local_clock(self) -> None:
        now = 100.0
        telemetry = TelegramActionTelemetry(clock=lambda: now)
        telemetry.record("inline_callback")
        now = 150.0
        snapshot = telemetry.record("map_message")

        self.assertEqual(snapshot["total"], 2)
        self.assertEqual(snapshot["last_minute"], 2)
        self.assertEqual(
            snapshot["by_kind"],
            {"inline_callback": 1, "map_message": 1},
        )

    def test_noop_edit_has_same_semantic_state(self) -> None:
        first = FakeMessage(
            "Ход игрока",
            [["Атака"]],
            edit_date=datetime(2026, 8, 15, tzinfo=UTC),
        )
        second = FakeMessage(
            "Ход игрока",
            [["Атака"]],
            edit_date=datetime(2026, 8, 15, tzinfo=UTC) + timedelta(seconds=1),
        )

        self.assertEqual(message_state_key(first), message_state_key(second))

    def test_countdown_only_edit_has_same_semantic_state(self) -> None:
        first = FakeMessage("🎯 Раунд 8\n⏳ Осталось: 23 сек.", [["Атака"]])
        second = FakeMessage("🎯 Раунд 8\n⏳ Осталось: 18 сек.", [["Атака"]])

        self.assertEqual(message_state_key(first), message_state_key(second))

    def test_meaningful_round_change_has_different_semantic_state(self) -> None:
        first = FakeMessage("🎯 Раунд 8\n⏳ Осталось: 23 сек.", [["Атака"]])
        second = FakeMessage("🎯 Раунд 9\n⏳ Осталось: 23 сек.", [["Атака"]])

        self.assertNotEqual(message_state_key(first), message_state_key(second))

    def test_state_refresh_is_reserved_once_per_inbound_generation(self) -> None:
        gate = StateRefreshGate()
        self.assertTrue(gate.reserve(7))
        gate.finish(sent=True)
        self.assertFalse(gate.reserve(7))
        self.assertTrue(gate.reserve(8))
        gate.finish(sent=True)

    def test_recovery_attempts_are_bounded_in_a_rolling_window(self) -> None:
        now = 0.0
        guard = RollingAttemptGuard(
            max_attempts=3,
            window_seconds=600.0,
            clock=lambda: now,
        )

        self.assertTrue(guard.allow())
        self.assertTrue(guard.allow())
        self.assertTrue(guard.allow())
        self.assertFalse(guard.allow())
        now = 601.0
        self.assertTrue(guard.allow())

    async def test_action_limiter_only_spaces_neighboring_requests(self) -> None:
        now = 0.0

        def clock() -> float:
            return now

        async def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds

        limiter = TelegramActionLimiter(
            min_interval=1.0,
            clock=clock,
            sleep=sleep,
        )

        self.assertEqual(await limiter.acquire(), 0.0)
        self.assertEqual(await limiter.acquire(), 1.0)
        self.assertEqual(await limiter.acquire(), 1.0)
        self.assertEqual(now, 2.0)
        self.assertFalse(limiter.pending)

    async def test_long_activity_never_creates_a_rolling_budget_pause(self) -> None:
        now = 0.0

        async def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds

        limiter = TelegramActionLimiter(
            min_interval=1.0,
            clock=lambda: now,
            sleep=sleep,
        )
        waits = [await limiter.acquire() for _ in range(100)]

        self.assertEqual(waits[0], 0.0)
        self.assertTrue(all(wait <= 1.0 for wait in waits))
        self.assertEqual(now, 99.0)
