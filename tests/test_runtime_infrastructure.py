from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.errors import BotResponseTimeoutError, FloodWaitError, RPCError

from automation_policy import DelayRange
from game_input import ActionOutcome
from game_mechanisms import MechanismSnapshot
from liveness import LivenessPhase, LivenessPolicy, ProgressMonitor
from models import BotState
from telegram_safety import RollingAttemptGuard, StateRefreshGate
from tests.test_shutdown import make_farmer, make_supervisor
from tests.test_telegram_reliability import Button, Message


class GenericDiscoveryInfrastructureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.farmer, self.client = make_farmer()
        self.request_state = AsyncMock(return_value=ActionOutcome.SENT)
        self.mechanism_snapshot = MagicMock(
            return_value=MechanismSnapshot(
                phase_name="DISCOVERY",
                position=None,
                location_name="search-zone",
                current_hp=None,
                max_hp=None,
                active_target=None,
                total_progress_units=5,
                cycle_progress_units=1,
                liveness_phase=LivenessPhase.GENERAL,
                liveness_suspended=False,
            )
        )
        self.farmer.mechanisms.request_state = self.request_state
        self.farmer.mechanisms.snapshot = self.mechanism_snapshot
        self.farmer._mechanisms_initialized = True
        self.farmer.reserve_telegram_action_slot = AsyncMock(return_value=True)
        self.now = 0.0
        self.farmer.state_refresh_gate = StateRefreshGate(clock=lambda: self.now)
        self.farmer.recovery_attempt_guard = RollingAttemptGuard(
            max_attempts=10, window_seconds=300, clock=lambda: self.now
        )

    async def asyncTearDown(self) -> None:
        await self.farmer.stop("test cleanup")

    async def test_alternative_controller_stops_after_three_unanswered_requests(self) -> None:
        for index in range(3):
            self.now = index * 31.0
            self.assertTrue(await self.farmer.recover_latest_state("no response"))
        self.now = 93.0
        self.assertFalse(await self.farmer.recover_latest_state("still no response"))
        self.assertEqual(self.request_state.await_count, 3)
        self.assertEqual(self.farmer.watchdog.recovery_attempts, 3)
        self.assertFalse(self.farmer.running)
        self.assertTrue(self.farmer.shutdown_complete)

    async def test_same_generation_and_concurrent_force_do_not_overlap_requests(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def request() -> ActionOutcome:
            entered.set()
            await release.wait()
            return ActionOutcome.SENT

        self.request_state.side_effect = request
        first = asyncio.create_task(self.farmer.recover_latest_state("watchdog"))
        try:
            await entered.wait()
            self.assertFalse(await self.farmer.request_current_state(force=True))
            self.assertFalse(await self.farmer.recover_latest_state("other watchdog"))
        finally:
            release.set()
            await first
        self.assertEqual(self.request_state.await_count, 1)
        self.assertEqual(self.farmer.watchdog.recovery_attempts, 1)

    async def test_unknown_timeout_keeps_retry_deadline_for_alternative_controller(self) -> None:
        async def hang() -> ActionOutcome:
            await asyncio.Event().wait()
            return ActionOutcome.SENT

        self.request_state.side_effect = hang
        with patch("farmer.TELEGRAM_STATE_RPC_TIMEOUT", 0.01):
            self.assertFalse(await self.farmer.request_current_state())
        self.request_state.side_effect = None
        self.assertFalse(await self.farmer.request_current_state())
        self.assertEqual(self.request_state.await_count, 1)
        self.now = 31.0
        self.assertTrue(await self.farmer.request_current_state())
        self.assertEqual(self.request_state.await_count, 2)

    async def test_unknown_transport_error_keeps_retry_deadline(self) -> None:
        self.request_state.side_effect = ConnectionResetError("lost reply")
        self.assertFalse(await self.farmer.recover_latest_state("watchdog"))
        self.request_state.side_effect = None
        self.assertFalse(await self.farmer.recover_latest_state("watchdog"))
        self.assertEqual(self.request_state.await_count, 1)
        self.assertEqual(self.farmer.watchdog.recovery_attempts, 1)

    async def test_flood_wait_passes_exact_duration_and_releases_retry_gate(self) -> None:
        self.request_state.side_effect = FloodWaitError(None, capture=37)
        self.farmer.pause_for_flood_wait = AsyncMock()
        self.assertFalse(await self.farmer.request_current_state())
        self.farmer.pause_for_flood_wait.assert_awaited_once_with(
            37, "запрос состояния", resume_mode="refresh"
        )
        self.request_state.side_effect = None
        self.assertTrue(await self.farmer.request_current_state())

    async def test_cooldown_and_queued_input_block_controller_without_spending_budget(self) -> None:
        with patch.object(self.farmer, "telegram_cooldown_remaining", return_value=10.0):
            self.assertFalse(await self.farmer.recover_latest_state("watchdog"))
        await self.farmer.enqueue_message(Message(1, "fresh input"))
        self.assertFalse(await self.farmer.recover_latest_state("watchdog"))
        self.request_state.assert_not_awaited()
        self.assertEqual(self.farmer.watchdog.recovery_attempts, 0)

    async def test_declined_effect_does_not_spend_recovery_budget(self) -> None:
        self.request_state.return_value = ActionOutcome.DEFERRED
        for _ in range(5):
            self.assertFalse(await self.farmer.recover_latest_state("watchdog"))
        self.assertEqual(self.farmer.watchdog.recovery_attempts, 0)
        self.assertTrue(self.farmer.recovery_attempt_guard.can_attempt())
        self.assertEqual(self.request_state.await_count, 5)

    async def test_response_during_request_keeps_progress_reset(self) -> None:
        self.farmer.watchdog = ProgressMonitor(clock=lambda: 0.0)
        self.farmer.watchdog.restore(recovery_attempts=2)

        async def responds() -> ActionOutcome:
            self.farmer.mark_progress("server response")
            return ActionOutcome.SENT

        self.request_state.side_effect = responds
        self.assertTrue(await self.farmer.recover_latest_state("watchdog"))
        self.assertEqual(self.farmer.watchdog.recovery_attempts, 0)
        self.assertEqual(self.farmer.watchdog.reason, "server response")

    async def test_button_discovery_reserves_only_one_outgoing_slot(self) -> None:
        source = Message(1, "search", ((Button("Поиск"),),))
        await self.farmer.enqueue_message(source)
        queued = await self.farmer.ingress.get()
        self.farmer.ingress.task_done()
        self.assertEqual(queued.snapshot.id, 1)
        self.farmer.action_delay = MagicMock(return_value=0.0)

        async def search_with_button() -> ActionOutcome:
            return await self.farmer.click_button_outcome(
                source,
                description="Поиск",
                delay_range=DelayRange(0, 0),
                exact="Поиск",
            )

        self.request_state.side_effect = search_with_button
        self.assertTrue(await self.farmer.request_current_state())
        self.farmer.reserve_telegram_action_slot.assert_awaited_once_with("Поиск")
        self.assertEqual(source.clicks, [(0, 0)])

    async def test_button_unknown_keeps_budget_and_deadline_without_repeating_rpc(self) -> None:
        source = Message(1, "search", ((Button("Поиск"),),))
        source.click = AsyncMock(side_effect=ConnectionResetError("reply lost"))
        await self.farmer.enqueue_message(source)
        await self.farmer.ingress.get()
        self.farmer.ingress.task_done()
        self.farmer.action_delay = MagicMock(return_value=0.0)

        async def search_with_button() -> ActionOutcome:
            return await self.farmer.click_button_outcome(
                source,
                description="Поиск",
                delay_range=DelayRange(0, 0),
                exact="Поиск",
            )

        self.request_state.side_effect = search_with_button
        self.assertFalse(await self.farmer.recover_latest_state("watchdog"))
        self.assertEqual(self.farmer.watchdog.recovery_attempts, 1)
        self.assertFalse(await self.farmer.recover_latest_state("immediate retry"))
        self.assertEqual(self.request_state.await_count, 1)
        self.now = 31.0
        self.assertFalse(await self.farmer.recover_latest_state("retry after deadline"))
        self.assertEqual(self.request_state.await_count, 2)
        self.assertEqual(self.farmer.watchdog.recovery_attempts, 1)
        source.click.assert_awaited_once()
        self.farmer.reserve_telegram_action_slot.assert_awaited_once()

    async def test_structured_callback_reports_each_rpc_outcome(self) -> None:
        self.farmer.pause_for_flood_wait = AsyncMock()
        cases = (
            (None, ActionOutcome.SENT),
            (FloodWaitError(None, capture=12), ActionOutcome.DEFERRED),
            (RPCError(None, "BUTTON_INVALID", 400), ActionOutcome.REJECTED),
            (BotResponseTimeoutError(None), ActionOutcome.DELIVERY_UNKNOWN),
            (OSError("offline"), ActionOutcome.DELIVERY_UNKNOWN),
        )
        sources = []
        for message_id, (error, expected) in enumerate(cases, start=1):
            with self.subTest(outcome=expected, error=type(error).__name__):
                source = Message(message_id, "prompt", ((Button("action"),),))
                source.click = AsyncMock(side_effect=error)
                sources.append(source)
                await self.farmer.enqueue_message(source)
                result = await self.farmer.press_button_outcome(source, 0, 0, "action")
                self.assertEqual(result, expected)
                source.click.assert_awaited_once()
        self.assertEqual(
            await self.farmer.press_button_outcome(sources[0], 0, 0, "stale"), ActionOutcome.STALE
        )
        self.assertEqual(
            await self.farmer.press_button_outcome(sources[-1], 0, 0, "repeat"),
            ActionOutcome.DUPLICATE,
        )
        with patch.object(self.farmer, "telegram_cooldown_remaining", return_value=10.0):
            self.assertEqual(
                await self.farmer.press_button_outcome(sources[-1], 0, 0, "cooldown"),
                ActionOutcome.DEFERRED,
            )

    async def test_unexpected_closed_ingress_terminates_runner(self) -> None:
        async def session() -> None:
            self.farmer.worker_task = self.farmer._start_background(
                self.farmer.event_worker(), name="closed-ingress-worker"
            )
            self.farmer.ingress.close()
            await asyncio.Event().wait()

        self.farmer._run_session = session
        with self.assertLogs("fog_farmer", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "Input stream closed"):
                await self.farmer.run()
        self.assertTrue(self.farmer.shutdown_complete)
        self.assertFalse(self.farmer.running)

    async def test_message_effect_rechecks_generation_after_single_reservation(self) -> None:
        self.client.send_message = AsyncMock()

        async def new_input_during_slot(_action: str) -> bool:
            await self.farmer.enqueue_message(Message(2, "new state"))
            return True

        self.farmer.reserve_telegram_action_slot = AsyncMock(side_effect=new_input_during_slot)

        async def request() -> ActionOutcome:
            return await self.farmer.send_game_message("Поиск", "search_message")

        self.request_state.side_effect = request
        self.assertFalse(await self.farmer.recover_latest_state("watchdog"))
        self.client.send_message.assert_not_awaited()
        self.farmer.reserve_telegram_action_slot.assert_awaited_once_with("search_message")
        self.assertEqual(self.farmer.watchdog.recovery_attempts, 0)

    async def test_application_pause_suspends_an_active_alternative_snapshot(self) -> None:
        self.farmer.state = BotState.PAUSED
        self.farmer.storage.get_setting.side_effect = [False, True]
        self.farmer.recover_latest_state = AsyncMock(return_value=True)

        with patch("farmer.WATCHDOG_CHECK_INTERVAL", 0):
            await self.farmer.watchdog_loop()

        self.farmer.recover_latest_state.assert_not_awaited()
        self.assertTrue(self.farmer.shutdown_complete)

    async def test_supervisor_status_uses_neutral_snapshot(self) -> None:
        self.farmer.storage.get_state.return_value = {}
        supervisor = make_supervisor(self.farmer)
        result = await supervisor.status()
        self.assertEqual(result["location_name"], "search-zone")
        self.mechanism_snapshot.assert_called_once()


class LivenessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchdog_uses_generic_monitor_and_injected_phase_policy(self) -> None:
        farmer, _ = make_farmer()
        now = 0.0
        farmer.watchdog = ProgressMonitor(clock=lambda: now)
        farmer.liveness_policy = LivenessPolicy(30, 20, 15, 5, 60)
        farmer.state = BotState.COMBAT
        farmer.storage.get_setting.side_effect = [False, True]
        farmer.recover_latest_state = AsyncMock(return_value=True)
        now = 5.0
        with patch("farmer.WATCHDOG_CHECK_INTERVAL", 0):
            await farmer.watchdog_loop()
        farmer.recover_latest_state.assert_awaited_once()
        self.assertTrue(farmer.shutdown_complete)

    async def test_legacy_suspended_phase_does_not_request_recovery(self) -> None:
        farmer, _ = make_farmer()
        farmer.watchdog.restore(last_progress_at=0.0)
        farmer.state = BotState.PAUSED
        farmer.storage.get_setting.side_effect = [False, True]
        farmer.recover_latest_state = AsyncMock(return_value=True)
        with patch("farmer.WATCHDOG_CHECK_INTERVAL", 0):
            await farmer.watchdog_loop()
        farmer.recover_latest_state.assert_not_awaited()
        self.assertTrue(farmer.shutdown_complete)


if __name__ == "__main__":
    unittest.main()
