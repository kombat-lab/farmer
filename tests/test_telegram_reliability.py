from __future__ import annotations

import asyncio
import unittest
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from event_cache import BoundedKeyCache
from event_ingress import EventIngress
from game_input import ActionOutcome
from legacy_combat_controller import LegacyCombatController
from legacy_fog_mechanisms import ManagedLegacyCombatController
from legacy_map_controller import LegacyMapController
from models import BotState, MovePlan, RouteDirection
from notifications import Notifier
from settings_service import SettingsService
from storage import Storage
from telegram_safety import (
    StateRefreshGate,
    TelegramActionTelemetry,
    message_state_key,
)
from tests.legacy_fog_factory import legacy_farmer, legacy_runtime


@dataclass(frozen=True)
class Button:
    text: str


@dataclass
class Message:
    id: int
    raw_text: str
    buttons: tuple[tuple[Button, ...], ...] = ()
    edit_date: datetime | None = None
    clicks: list[tuple[int, int]] = field(default_factory=list)

    async def click(self, row: int, column: int) -> object:
        self.clicks.append((row, column))
        return None


class RefreshGateTests(unittest.TestCase):
    def test_unanswered_request_can_retry_after_deadline(self) -> None:
        now = 0.0
        gate = StateRefreshGate(retry_after=30, clock=lambda: now)
        self.assertTrue(gate.reserve(1))
        gate.finish(sent=True)
        now = 29.9
        self.assertFalse(gate.reserve(1))
        now = 30.0
        self.assertTrue(gate.reserve(1))
        gate.finish(sent=True)

    def test_new_state_and_force_never_overlap_inflight_rpc(self) -> None:
        gate = StateRefreshGate()
        self.assertTrue(gate.reserve(1))
        self.assertFalse(gate.reserve(2))
        self.assertFalse(gate.reserve(2, force=True))
        gate.finish(sent=True)
        self.assertTrue(gate.reserve(2))
        gate.finish(sent=True)

    def test_unsent_request_does_not_consume_retry_deadline(self) -> None:
        gate = StateRefreshGate()
        self.assertTrue(gate.reserve(1))
        gate.finish(sent=False)
        self.assertTrue(gate.reserve(1))
        gate.finish(sent=True)

    def test_nonfinite_intervals_are_rejected(self) -> None:
        for value in (float('nan'), float('inf'), -1, 0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                StateRefreshGate(retry_after=value)


class TelemetryWindowTests(unittest.TestCase):
    def test_idle_snapshot_expires_old_requests_at_window_boundary(self) -> None:
        now = 0.0
        telemetry = TelegramActionTelemetry(clock=lambda: now)
        telemetry.record('map_message')
        now = 60
        self.assertEqual(telemetry.snapshot()['last_minute'], 0)
        now = 600
        self.assertEqual(telemetry.snapshot()['last_ten_minutes'], 0)
        self.assertEqual(telemetry.snapshot()['total'], 1)

    def test_changed_button_layout_is_a_different_action(self) -> None:
        first = Message(1, 'prompt', ((Button('a'), Button('b')),))
        second = Message(1, 'prompt', ((Button('a'),), (Button('b'),)))
        self.assertNotEqual(message_state_key(first), message_state_key(second))

    def test_cache_rejects_zero_capacity(self) -> None:
        with self.assertRaises(ValueError):
            BoundedKeyCache[str](0)


class EventFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.storage = Storage(Path(self.directory.name) / 'test.sqlite3')
        settings = SettingsService(self.storage)
        await settings.load()
        self.client = MagicMock()
        self.client.disconnect = AsyncMock()
        self.client.is_connected.return_value = False
        self.client.send_message = AsyncMock()
        notifier = MagicMock(spec=Notifier)
        with patch('tests.legacy_fog_factory.create_test_client', return_value=self.client):
            self.farmer = legacy_farmer(self.storage, notifier, settings)
        self.farmer.game_bot = 'offline-test-peer'
        self.legacy = legacy_runtime(self.farmer)
        await self.legacy.initialize()
        self.legacy.start_cycle(1)
        self.legacy.context.current_hp = 780
        self.legacy.context.max_hp = 780
        self.farmer.click_button = AsyncMock(return_value=False)
        self.farmer.click_button_outcome = AsyncMock(return_value=ActionOutcome.REJECTED)
        managed = self.legacy.combat
        assert isinstance(managed, ManagedLegacyCombatController)
        self.combat: LegacyCombatController = managed.legacy_controller
        self.map = self.legacy.discovery
        assert isinstance(self.map, LegacyMapController)

    async def asyncTearDown(self) -> None:
        await self.farmer.stop('test cleanup')
        await self.storage.close()
        self.directory.cleanup()

    async def drain(self, *messages: Message) -> None:
        for message in messages:
            await self.farmer.enqueue_message(message)
        self.farmer.worker_task = self.farmer._start_background(
            self.farmer.event_worker(), name="test-events"
        )
        await asyncio.wait_for(self.farmer.ingress.join(), timeout=2)
        self.farmer.worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.farmer.worker_task

    async def test_victory_is_recorded_before_following_map(self) -> None:
        await self.drain(
            Message(1, 'Бой завершён. Победа'),
            Message(2, 'Позиция: (1, 1)\nМонстры на клетке: 0'),
        )
        self.assertEqual(self.legacy.statistics.session_wins, 1)
        count = self.storage.connection.execute('SELECT COUNT(*) FROM battles').fetchone()[0]
        self.assertEqual(count, 1)

    async def test_source_mutation_after_admission_cannot_change_recorded_fact(self) -> None:
        source = Message(771, "Бой завершён. Победа")
        await self.farmer.enqueue_message(source)
        source.id = 999
        source.raw_text = "Бой завершён. Поражение"
        source.buttons = ((Button("mutated"),),)
        self.farmer.worker_task = self.farmer._start_background(
            self.farmer.event_worker(), name="immutable-input-worker"
        )
        await asyncio.wait_for(self.farmer.ingress.join(), timeout=2)
        self.assertEqual(self.legacy.statistics.session_wins, 1)
        row = self.storage.connection.execute(
            "SELECT source_message_id, result FROM battles"
        ).fetchone()
        self.assertEqual(tuple(row), (771, "VICTORY"))
        latest = self.farmer.latest_received_message
        assert latest is not None
        self.assertEqual(latest.id, 771)
        self.assertEqual(latest.raw_text, "Бой завершён. Победа")
        self.assertEqual(latest.buttons, ())

    async def test_snapshot_action_uses_original_rpc_after_source_mutation(self) -> None:
        self.farmer.reserve_telegram_action_slot = AsyncMock(return_value=True)
        source = Message(772, "prompt", ((Button("attack"),),))
        await self.farmer.enqueue_message(source)
        source.id = 999
        source.raw_text = "mutated"
        source.buttons = ()
        latest = self.farmer.latest_received_message
        assert latest is not None
        self.assertTrue(self.farmer.is_latest_message(latest))
        self.assertTrue(await self.farmer.press_button(source, 0, 0, "snapshot action"))
        self.assertEqual(source.clicks, [(0, 0)])
        self.assertEqual(latest.buttons[0][0].text, "attack")

    async def test_processed_stale_rpc_is_released_while_latest_is_reprocessable(self) -> None:
        old = Message(1, "old")
        latest_source = Message(2, "latest")
        self.farmer.handle_message = AsyncMock()
        await self.drain(old, latest_source)
        self.assertIsNone(self.farmer.input_event(old))
        latest = self.farmer.latest_received_message
        assert latest is not None
        self.assertIs(latest.rpc, latest_source)
        self.assertIs(self.farmer.input_event(latest), latest.event)
        self.assertEqual(len(self.farmer._event_messages), 1)
        self.farmer.enqueue_latest_for_reprocessing()
        self.assertEqual(self.farmer.ingress.qsize(), 1)

    async def test_defeat_starts_recovery_even_with_a_newer_map(self) -> None:
        await self.drain(
            Message(1, 'Бой завершён. Поражение'),
            Message(2, 'Позиция: (1, 1)\nМонстры на клетке: 0'),
        )
        self.assertEqual(self.legacy.statistics.session_defeats, 1)
        self.assertIs(self.farmer.state, BotState.RECOVERY)
        self.assertIsNotNone(self.legacy.recovery_task)

    async def test_different_revisions_preserve_battle_result(self) -> None:
        await self.drain(
            Message(1, 'Бой завершён. Победа'),
            Message(1, 'Позиция: (1, 1)\nМонстры на клетке: 0'),
        )
        self.assertEqual(self.legacy.statistics.session_wins, 1)

    async def test_reprocessing_prompt_does_not_learn_round_twice(self) -> None:
        message = Message(1, '⚔️ Раунд 6\nПравая сторона\nKombat получает 20 урона')
        await self.farmer.enqueue_message(message)
        await self.farmer.handle_message(message)
        first_samples = self.combat.memory.incoming_damage.samples
        await self.farmer.handle_message(message)
        self.assertEqual(first_samples, 1)
        self.assertEqual(self.combat.memory.incoming_damage.samples, first_samples)

    async def test_stale_combat_prompt_cannot_start_a_skill_action(self) -> None:
        await self.drain(
            Message(1, '⚔️ Раунд 6\nВыберите навык:', ((Button('Атака аколита'),),)),
            Message(2, 'Бой завершён. Победа'),
        )
        self.farmer.click_button_outcome.assert_not_awaited()

    async def test_full_queue_backpressures_without_dropping_battle_result(self) -> None:
        self.farmer.ingress = EventIngress(self.farmer.input_policy, capacity=1)
        victory = Message(1, 'Бой завершён. Победа')
        following = Message(2, 'Позиция: (1, 1)\nМонстры на клетке: 0')
        await self.farmer.enqueue_message(victory)
        producer = asyncio.create_task(self.farmer.enqueue_message(following))
        await asyncio.sleep(0)
        self.assertFalse(producer.done())
        self.farmer.worker_task = self.farmer._start_background(
            self.farmer.event_worker(), name="test-events"
        )
        await asyncio.wait_for(producer, timeout=2)
        await asyncio.wait_for(self.farmer.ingress.join(), timeout=2)
        self.assertEqual(self.legacy.statistics.session_wins, 1)

    async def test_refresh_retries_after_no_response_without_replaying_buttons(self) -> None:
        now = 0.0
        self.farmer.state_refresh_gate = StateRefreshGate(clock=lambda: now)
        self.assertTrue(await self.farmer.request_current_state())
        self.assertFalse(await self.farmer.request_current_state())
        now = 31
        self.assertTrue(await self.farmer.request_current_state())
        self.assertEqual(self.client.send_message.await_count, 2)

    async def test_refresh_failure_is_bounded_and_retryable(self) -> None:
        now = 0.0
        self.farmer.state_refresh_gate = StateRefreshGate(clock=lambda: now)
        self.client.send_message.side_effect = OSError('temporary offline failure')
        self.assertFalse(await self.farmer.request_current_state())
        self.client.send_message.side_effect = None
        now = 31
        self.assertTrue(await self.farmer.request_current_state())

    async def test_hanging_refresh_times_out_and_releases_gate(self) -> None:
        now = 0.0
        self.farmer.state_refresh_gate = StateRefreshGate(clock=lambda: now)
        async def hang(*_args: object) -> None:
            await asyncio.Event().wait()
        self.client.send_message.side_effect = hang
        with patch('farmer.TELEGRAM_STATE_RPC_TIMEOUT', 0.01):
            self.assertFalse(await self.farmer.request_current_state())
        now = 31
        self.client.send_message.side_effect = None
        self.assertTrue(await self.farmer.request_current_state())

    async def test_recovery_stops_after_bounded_unsuccessful_attempts(self) -> None:
        self.farmer.watchdog.restore(recovery_attempts=3)
        self.farmer.stop = AsyncMock()
        self.assertFalse(await self.farmer.recover_latest_state('no response'))
        self.farmer.stop.assert_awaited_once()
        self.client.send_message.assert_not_awaited()
        del self.farmer.stop

    async def test_keyboard_edit_updates_ui_without_learning_damage_twice(self) -> None:
        text = "⚔️ Раунд 6\nПравая сторона\nKombat получает 20 урона\nМана: 12/12"
        first = Message(1, text, ((Button("Атака аколита"),),))
        edited = Message(1, text, ((Button("Лечение [Мана 4]"),),))
        await self.farmer.enqueue_message(first)
        await self.farmer.handle_message(first)
        await self.farmer.enqueue_message(edited)
        await self.farmer.handle_message(edited)
        self.assertEqual(self.combat.memory.incoming_damage.samples, 1)
        latest = self.combat.memory.latest_round
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertIn("лечение", latest.castable_skills())
        self.assertNotIn("атака аколита", latest.castable_skills())

    async def test_gate_rejection_does_not_spend_recovery_attempts(self) -> None:
        self.farmer.state_refresh_gate = StateRefreshGate(clock=lambda: 0)
        for _ in range(5):
            await self.farmer.recover_latest_state("same unanswered state")
        self.assertEqual(self.client.send_message.await_count, 1)
        self.assertEqual(self.farmer.watchdog.recovery_attempts, 1)
        self.assertTrue(self.farmer.running)

    async def test_cooldown_started_while_waiting_prevents_callback(self) -> None:
        import time

        message = Message(1, "prompt", ((Button("attack"),),))
        await self.farmer.enqueue_message(message)

        async def cooldown_during_wait(_action: str) -> bool:
            self.farmer.telegram_cooldown_until = time.monotonic() + 60
            return True

        self.farmer.reserve_telegram_action_slot = cooldown_during_wait
        self.assertFalse(await self.farmer.press_button(message, 0, 0, "attack"))
        self.assertEqual(message.clicks, [])
        event = self.farmer.input_event(message)
        assert event is not None
        self.assertNotIn(event.action_key, self.farmer.attempted_actions)

    async def test_background_failure_reaches_runner_and_cleans_up(self) -> None:
        async def fail() -> None:
            raise RuntimeError("watchdog failed")

        async def session() -> None:
            self.farmer.watchdog_task = self.farmer._start_background(
                fail(), name="test-watchdog"
            )
            await asyncio.Event().wait()

        self.farmer._run_session = session
        with self.assertRaisesRegex(RuntimeError, "watchdog failed"):
            await self.farmer.run()
        self.assertFalse(self.farmer.running)
        self.assertTrue(self.farmer.shutdown_complete)
        self.client.disconnect.assert_not_awaited()

    async def test_shutdown_joins_task_that_cleared_public_handle(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def timer() -> None:
            self.farmer.activity_break_task = None
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.farmer.activity_break_task = self.farmer._start_background(
            timer(), name="test-timer"
        )
        await started.wait()
        await self.farmer.stop("test stop")
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.farmer.shutdown_complete)

    async def test_keyboard_only_map_edit_does_not_reject_pending_move(self) -> None:
        text = "Позиция: (8, 0)\nМонстры на клетке: 0"
        first = Message(1, text, ((Button("a"),),))
        await self.farmer.enqueue_message(first)
        await self.farmer.handle_message(first)
        plan = self.map.navigator.plan((8, 0))
        self.legacy.context.pending_move = plan
        edited = Message(1, text, ((Button("b"),),))
        await self.farmer.enqueue_message(edited)
        await self.farmer.handle_message(edited)
        self.assertEqual(self.legacy.context.failed_move_attempts, 0)
        self.assertIs(self.legacy.context.pending_move, plan)


    async def test_keyboard_only_map_churn_does_not_reset_liveness(self) -> None:
        first = Message(1, "Позиция: (8, 0)\nМонстры на клетке: 0", ((Button("a"),),))
        await self.farmer.enqueue_message(first)
        await self.farmer.handle_message(first)
        self.farmer.watchdog.restore(last_progress_at=123.0)
        self.farmer.watchdog.restore(recovery_attempts=2)
        for button in ("b", "c", "a"):
            edited = Message(1, first.raw_text, ((Button(button),),))
            await self.farmer.enqueue_message(edited)
            await self.farmer.handle_message(edited)
        self.assertEqual(self.farmer.watchdog.last_progress_at, 123.0)
        self.assertEqual(self.farmer.watchdog.recovery_attempts, 2)

    async def test_map_roundtrip_observes_return_even_with_identical_keyboard(self) -> None:
        first = Message(1, "Позиция: (8, 0)\nМонстры на клетке: 0")
        middle = Message(1, "Позиция: (7, 0)\nМонстры на клетке: 0")
        last = Message(1, first.raw_text)
        await self.drain(first, middle, last)
        self.assertEqual(self.farmer.ingress.generation, 3)
        self.assertEqual(self.legacy.context.current_position, (8, 0))
        self.assertFalse(self.farmer.is_latest_message(first))
        self.assertTrue(self.farmer.is_latest_message(last))

    async def test_map_roundtrip_confirms_return_move_with_changed_keyboard(self) -> None:
        await self.drain(Message(1, "Позиция: (8, 0)\nМонстры на клетке: 0"))
        await self.drain(Message(1, "Позиция: (7, 0)\nМонстры на клетке: 0"))
        self.legacy.context.pending_move = MovePlan(
            (7, 0), (8, 0), "➡️", RouteDirection.DOWN, RouteDirection.DOWN
        )
        await self.drain(Message(1, "Позиция: (8, 0)\nМонстры на клетке: 0", ((Button("b"),),)))
        self.assertEqual(self.legacy.context.current_position, (8, 0))
        self.assertIsNone(self.legacy.context.pending_move)
        self.assertEqual(self.legacy.context.move_count, 1)
        self.assertEqual(self.legacy.context.failed_move_attempts, 0)

    async def test_new_prompt_generation_allows_one_action_on_return(self) -> None:
        self.farmer.reserve_telegram_action_slot = AsyncMock(return_value=True)
        first = Message(1, "Позиция: (8, 0)\nМонстры на клетке: 0", ((Button("action"),),))
        middle = Message(1, "Позиция: (7, 0)\nМонстры на клетке: 0", first.buttons)
        last = Message(1, first.raw_text, first.buttons)
        await self.farmer.enqueue_message(first)
        self.assertTrue(await self.farmer.press_button(first, 0, 0, "first action"))
        self.assertFalse(await self.farmer.press_button(first, 0, 0, "repeated action"))
        await self.farmer.enqueue_message(middle)
        await self.farmer.enqueue_message(last)
        self.assertFalse(await self.farmer.press_button(first, 0, 0, "stale action"))
        self.assertTrue(await self.farmer.press_button(last, 0, 0, "return action"))
        self.assertFalse(await self.farmer.press_button(last, 0, 0, "repeated return action"))
        self.assertEqual(first.clicks, [(0, 0)])
        self.assertEqual(last.clicks, [(0, 0)])

    async def test_countdown_edit_preserves_prompt_and_cannot_repeat_action(self) -> None:
        self.farmer.reserve_telegram_action_slot = AsyncMock(return_value=True)
        timestamp = datetime(2026, 9, 10, tzinfo=UTC)
        first = Message(1, "Раунд 8\n⏳ Осталось: 20 сек.", ((Button("action"),),), timestamp)
        edited = Message(
            1, "Раунд 8\n⏳ Осталось: 19 сек.", first.buttons, timestamp + timedelta(seconds=1)
        )
        await self.farmer.enqueue_message(first)
        self.assertTrue(await self.farmer.press_button(first, 0, 0, "action"))
        await self.farmer.enqueue_message(edited)
        self.assertEqual(self.farmer.ingress.generation, 1)
        self.assertEqual(self.farmer.ingress.qsize(), 1)
        self.assertTrue(self.farmer.is_latest_message(first))
        self.assertFalse(await self.farmer.press_button(first, 0, 0, "repeated action"))
        self.assertFalse(await self.farmer.press_button(edited, 0, 0, "edited action"))
        self.assertEqual(first.clicks, [(0, 0)])
        self.assertEqual(edited.clicks, [])

    async def test_returned_map_from_other_message_is_observed(self) -> None:
        await self.drain(
            Message(1, "Позиция: (8, 0)\nМонстры на клетке: 0"),
            Message(2, "Позиция: (7, 0)\nМонстры на клетке: 0"),
            Message(3, "Позиция: (8, 0)\nМонстры на клетке: 0"),
        )
        self.assertEqual(self.legacy.context.current_position, (8, 0))


    async def test_new_edit_reactivates_previous_map_message_after_other_map(self) -> None:
        timestamp = datetime(2026, 9, 10, tzinfo=UTC)
        first = Message(10, "Позиция: (8, 0)\nМонстры на клетке: 0", edit_date=timestamp)
        await self.drain(first, Message(11, "Позиция: (7, 0)\nМонстры на клетке: 0"))
        self.legacy.context.pending_move = MovePlan(
            (7, 0), (8, 0), "➡️", RouteDirection.DOWN, RouteDirection.DOWN
        )
        await self.drain(Message(10, first.raw_text, edit_date=timestamp + timedelta(seconds=2)))
        self.assertEqual(self.legacy.context.current_position, (8, 0))
        self.assertIsNone(self.legacy.context.pending_move)
        self.assertEqual(self.legacy.context.move_count, 1)

    async def test_duplicate_old_map_delivery_does_not_reactivate_it(self) -> None:
        timestamp = datetime(2026, 9, 10, tzinfo=UTC)
        first = Message(10, "Позиция: (8, 0)\nМонстры на клетке: 0", edit_date=timestamp)
        middle = Message(11, "Позиция: (7, 0)\nМонстры на клетке: 0")
        await self.drain(first, middle, Message(10, first.raw_text, edit_date=timestamp))
        self.assertEqual(self.legacy.context.current_position, (7, 0))
        latest = self.farmer.latest_received_message
        assert latest is not None
        self.assertIs(latest.rpc, middle)

    async def test_combat_keyboard_roundtrip_cannot_retry_uncertain_callback(self) -> None:
        from telethon.errors import BotResponseTimeoutError

        self.farmer.reserve_telegram_action_slot = AsyncMock(return_value=True)
        first = Message(1, "Раунд 8\nХод Kombat", ((Button("Атака"),),))
        first.click = AsyncMock(side_effect=BotResponseTimeoutError(request=None))
        await self.farmer.enqueue_message(first)
        self.assertFalse(await self.farmer.press_button(first, 0, 0, "attack"))
        changed = Message(1, first.raw_text, ((Button("Лечение"),),))
        await self.farmer.enqueue_message(changed)
        self.assertFalse(await self.farmer.press_button(changed, 0, 0, "another skill"))
        self.assertEqual(changed.clicks, [])
        returned = Message(1, first.raw_text, first.buttons)
        await self.farmer.enqueue_message(returned)
        self.assertFalse(await self.farmer.press_button(returned, 0, 0, "retry"))
        first.click.assert_awaited_once()
        self.assertEqual(returned.clicks, [])

    async def test_transport_disconnect_keeps_claim_across_keyboard_edit_and_requeue(self) -> None:
        self.farmer.reserve_telegram_action_slot = AsyncMock(return_value=True)
        first = Message(1, "Раунд 8", ((Button("Атака"),),))
        first.click = AsyncMock(side_effect=ConnectionResetError("connection lost"))
        await self.farmer.enqueue_message(first)
        self.assertFalse(await self.farmer.press_button(first, 0, 0, "attack"))
        changed = Message(1, first.raw_text, ((Button("Лечение"),),))
        await self.farmer.enqueue_message(changed)
        self.farmer.enqueue_latest_for_reprocessing()
        self.assertFalse(await self.farmer.press_button(changed, 0, 0, "retry"))
        first.click.assert_awaited_once()
        self.assertEqual(changed.clicks, [])
        self.assertTrue(self.farmer.running)
        self.assertEqual(self.farmer.telegram_cooldown_remaining(), 0)
        self.client.send_message.assert_not_awaited()

    async def test_map_keyboard_roundtrip_cannot_repeat_previous_action(self) -> None:
        self.farmer.reserve_telegram_action_slot = AsyncMock(return_value=True)
        first = Message(1, "Позиция: (8, 0)\nМонстры на клетке: 0", ((Button("a"),),))
        await self.farmer.enqueue_message(first)
        self.assertTrue(await self.farmer.press_button(first, 0, 0, "move"))
        changed = Message(1, first.raw_text, ((Button("b"),),))
        await self.farmer.enqueue_message(changed)
        self.assertFalse(await self.farmer.press_button(changed, 0, 0, "another move"))
        self.assertEqual(changed.clicks, [])
        returned = Message(1, first.raw_text, first.buttons)
        await self.farmer.enqueue_message(returned)
        self.assertFalse(await self.farmer.press_button(returned, 0, 0, "retry move"))
        self.assertEqual(returned.clicks, [])


    async def test_returned_map_restores_location_and_geometry_at_same_position(self) -> None:
        first = "🗺️ Темный грот\nПозиция: (1, 0)\nРазмер: 3x3\nМонстры на клетке: 0"
        transitions = (
            "🗺️ Темный грот\nПозиция: (1, 0)\nРазмер: 4x4\nМонстры на клетке: 0",
            "🗺️ Мертвый лес\nПозиция: (1, 0)\nРазмер: 3x3\nМонстры на клетке: 0",
        )
        for middle in transitions:
            with self.subTest(middle=middle):
                await self.drain(Message(1, first), Message(1, middle), Message(1, first))
                self.assertEqual(self.map.navigator.location_name, "Темный грот")
                self.assertEqual(self.map.navigator.max_x, 2)
                self.assertEqual(self.map.navigator.max_y, 2)
                self.assertEqual(self.legacy.context.current_position, (1, 0))


    async def test_keyboard_change_before_first_callback_uses_fresh_buttons(self) -> None:
        self.farmer.reserve_telegram_action_slot = AsyncMock(return_value=True)
        first = Message(1, "Раунд 8\nХод Kombat", ((Button("Атака"),),))
        changed = Message(1, first.raw_text, ((Button("Лечение"),),))
        await self.farmer.enqueue_message(first)
        await self.farmer.enqueue_message(changed)
        self.assertFalse(await self.farmer.press_button(first, 0, 0, "old skill"))
        self.assertTrue(await self.farmer.press_button(changed, 0, 0, "new skill"))
        self.assertEqual(first.clicks, [])
        self.assertEqual(changed.clicks, [(0, 0)])
