from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

from runtime_state import BotState
from tests.test_legacy_map_controller import AlternativeInputPolicy, AlternativeRuntime
from tests.test_shutdown import make_farmer, make_supervisor


class CycleRestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.farmer, _ = make_farmer()
        self.farmer.settings._snapshot = replace(
            self.farmer.settings.values, cycles_count=5,
            cycle_rest_min=600, cycle_rest_max=600,
        )
        self.farmer.mechanisms = AlternativeRuntime(AlternativeInputPolicy())
        await self.farmer.mechanisms.initialize()
        self.farmer._mechanisms_initialized = True
        self.farmer.start_cycle()
        self.farmer.storage.get_state.return_value = {}
        self.farmer.mark_progress = Mock()
        self.farmer.process_latest_state = AsyncMock()

    async def asyncTearDown(self) -> None:
        await self.farmer.stop("test cleanup")

    async def begin_rest(self) -> str:
        await self.farmer.complete_cycle()
        token = self.farmer.rest_token
        self.assertIsInstance(token, str)
        self.assertEqual(len(token), 32)
        return token

    async def test_notice_carries_unique_action_and_persisted_deadline(self) -> None:
        before = datetime.now(UTC)
        token = await self.begin_rest()
        call = self.farmer.notifier.send_event.await_args
        self.assertEqual(call.kwargs["action"].callback_data, f"rest:skip:{token}")
        rest_state = self.farmer.storage.update_state.await_args.kwargs
        self.assertEqual(rest_state["game_state"], "RESTING")
        self.assertGreater(datetime.fromisoformat(rest_state["rest_until"]), before)
        timer = self.farmer.rest_task
        await self.farmer.complete_cycle()
        self.assertIs(self.farmer.rest_task, timer)
        self.assertEqual(self.farmer.rest_token, token)
        self.farmer.notifier.send_event.assert_awaited_once()

    async def test_duplicate_clicks_and_timer_advance_only_once(self) -> None:
        token = await self.begin_rest()
        timer = self.farmer.rest_task
        self.farmer.start_cycle = Mock(wraps=self.farmer.start_cycle)
        first, second, _ = await asyncio.gather(
            self.farmer.skip_rest(token), self.farmer.skip_rest(token),
            self.farmer.rest_between_cycles(0, token),
        )
        self.assertEqual([first[0], second[0]], [True, False])
        self.assertEqual(self.farmer.current_cycle, 2)
        self.farmer.start_cycle.assert_called_once()
        self.farmer.process_latest_state.assert_awaited_once()
        self.assertIsNone(self.farmer.rest_token)
        self.assertIsNone(self.farmer.rest_task)
        self.assertTrue(timer.done())
        self.assertIsNone(self.farmer.storage.update_state.await_args.kwargs["rest_until"])

    async def test_natural_completion_starts_next_cycle_once(self) -> None:
        token = await self.begin_rest()
        await self.farmer.rest_between_cycles(0, token)
        self.assertEqual(self.farmer.current_cycle, 2)
        self.farmer.process_latest_state.assert_awaited_once()
        self.assertFalse((await self.farmer.skip_rest(token))[0])

    async def test_old_button_and_old_timer_cannot_skip_new_rest(self) -> None:
        old_token = await self.begin_rest()
        await self.farmer.skip_rest(old_token)
        new_token = await self.begin_rest()
        timer = self.farmer.rest_task
        self.assertNotEqual(new_token, old_token)
        self.assertFalse((await self.farmer.skip_rest(old_token))[0])
        await self.farmer.rest_between_cycles(0, old_token)
        self.assertEqual(self.farmer.rest_token, new_token)
        self.assertEqual(self.farmer.current_cycle, 2)
        self.assertIs(self.farmer.rest_task, timer)
        self.assertFalse((await self.farmer.resume())[0])
        self.assertIs(self.farmer.state, BotState.RESTING)

    async def test_pause_invalidates_button_and_resume_starts_next_cycle(self) -> None:
        token = await self.begin_rest()
        await self.farmer.request_pause()
        self.assertIs(self.farmer.state, BotState.PAUSED)
        self.assertIsNone(self.farmer.rest_token)
        self.assertFalse((await self.farmer.skip_rest(token))[0])
        self.assertIs(self.farmer.state, BotState.PAUSED)
        self.farmer.process_latest_state.assert_not_awaited()
        self.assertTrue((await self.farmer.resume())[0])
        self.assertEqual(self.farmer.current_cycle, 2)
        self.farmer.process_latest_state.assert_awaited_once()

    async def test_skip_preserves_telegram_cooldown_and_sends_no_game_action(self) -> None:
        token = await self.begin_rest()
        cooldown = time.monotonic() + 300
        self.farmer.telegram_cooldown_until = cooldown
        self.farmer.telegram_cooldown_reason = "Telegram FLOOD_WAIT"
        del self.farmer.process_latest_state
        self.farmer.mechanisms.request_state = AsyncMock()
        self.assertTrue((await self.farmer.skip_rest(token))[0])
        self.assertEqual(self.farmer.telegram_cooldown_until, cooldown)
        self.assertEqual(self.farmer.telegram_cooldown_reason, "Telegram FLOOD_WAIT")
        self.farmer.mechanisms.request_state.assert_not_awaited()

    async def test_click_during_notification_does_not_install_stale_timer(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def notify(*args, **kwargs):
            if "action" in kwargs:
                entered.set()
                await release.wait()

        self.farmer.notifier.send_event.side_effect = notify
        completing = asyncio.create_task(self.farmer.complete_cycle())
        await entered.wait()
        token = self.farmer.rest_token
        self.assertTrue((await self.farmer.skip_rest(token))[0])
        release.set()
        await completing
        self.assertIsNone(self.farmer.rest_task)
        self.assertIsNone(self.farmer.rest_token)
        self.assertEqual(self.farmer.current_cycle, 2)

    async def test_pause_during_skip_prevents_refresh(self) -> None:
        token = await self.begin_rest()
        entered, release = asyncio.Event(), asyncio.Event()

        async def notify(*args, **kwargs):
            entered.set()
            await release.wait()

        self.farmer.notifier.send_event.side_effect = notify
        skipping = asyncio.create_task(self.farmer.skip_rest(token))
        await entered.wait()
        await self.farmer.request_pause()
        release.set()
        await skipping
        self.assertIs(self.farmer.state, BotState.PAUSED)
        self.farmer.process_latest_state.assert_not_awaited()

    async def test_cancelled_click_request_does_not_interrupt_cycle_transition(self) -> None:
        token = await self.begin_rest()
        entered, release = asyncio.Event(), asyncio.Event()

        async def notify(*args, **kwargs):
            entered.set()
            await release.wait()

        self.farmer.notifier.send_event.side_effect = notify
        skipping = asyncio.create_task(self.farmer.skip_rest(token))
        await entered.wait()
        skipping.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await skipping
        transition = next(
            task for task in self.farmer.task_scope.snapshot() if task.get_name() == "cycle-start"
        )
        release.set()
        await transition
        self.farmer.process_latest_state.assert_awaited_once()
        self.assertEqual(self.farmer.current_cycle, 2)

    async def test_stop_cancels_transition_and_invalidates_old_button(self) -> None:
        token = await self.begin_rest()
        entered = asyncio.Event()

        async def notify(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.farmer.notifier.send_event.side_effect = notify
        skipping = asyncio.create_task(self.farmer.skip_rest(token))
        await entered.wait()
        await self.farmer.stop("test stop")
        self.assertFalse((await skipping)[0])
        self.assertFalse((await self.farmer.skip_rest(token))[0])
        self.assertIs(self.farmer.state, BotState.STOPPED)
        self.assertIsNone(self.farmer.rest_token)
        self.farmer.process_latest_state.assert_not_awaited()

    async def test_supervisor_rejects_skip_when_stopped_and_exposes_active_token(self) -> None:
        token = await self.begin_rest()
        supervisor = make_supervisor(self.farmer)
        self.assertFalse((await supervisor.skip_rest(token))[0])
        task = asyncio.create_task(asyncio.Event().wait())
        supervisor.task = task
        try:
            state = await supervisor.status()
            self.assertEqual(state["rest_token"], token)
            self.assertTrue((await supervisor.skip_rest(token))[0])
            self.assertIsNone((await supervisor.status())["rest_token"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
