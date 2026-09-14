from __future__ import annotations

import asyncio
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock

from telethon.errors import BotResponseTimeoutError, FloodWaitError, RPCError

from telegram_action_executor import CallbackResult, CallbackStatus, TelegramActionExecutor


class FakeMessage:
    def __init__(self) -> None:
        self.click = AsyncMock(return_value=object())


class TelegramActionExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.executor = TelegramActionExecutor(timeout=1)
        self.message = FakeMessage()

    async def test_success_calls_exact_coordinates_once(self) -> None:
        result = await self.executor.execute(self.message, 2, 3)
        self.assertEqual(result, CallbackResult(CallbackStatus.SENT))
        self.message.click.assert_awaited_once_with(2, 3)

    async def test_flood_wait_preserves_exact_server_duration(self) -> None:
        self.message.click.side_effect = FloodWaitError(request=None, capture=37)
        result = await self.executor.execute(self.message, 0, 1)
        self.assertEqual(result.status, CallbackStatus.FLOOD_WAIT)
        self.assertEqual(result.flood_wait_seconds, 37)
        self.assertEqual(result.error_type, 'FloodWaitError')
        self.assertTrue(result.detail)
        self.message.click.assert_awaited_once_with(0, 1)

    async def test_zero_flood_wait_is_not_replaced_with_a_default(self) -> None:
        self.message.click.side_effect = FloodWaitError(request=None, capture=0)
        result = await self.executor.execute(self.message, 0, 0)
        self.assertEqual(result.flood_wait_seconds, 0)
        self.message.click.assert_awaited_once()

    async def test_bot_timeout_is_uncertain_delivery(self) -> None:
        self.message.click.side_effect = BotResponseTimeoutError(request=None)
        result = await self.executor.execute(self.message, 0, 0)
        self.assertEqual(result.status, CallbackStatus.DELIVERY_UNKNOWN)
        self.assertEqual(result.error_type, 'BotResponseTimeoutError')
        self.assertIsNone(result.flood_wait_seconds)
        self.message.click.assert_awaited_once()

    async def test_rpc_timeout_is_uncertain_delivery(self) -> None:
        self.message.click.side_effect = TimeoutError('transport timeout')
        result = await self.executor.execute(self.message, 0, 0)
        self.assertEqual(result.status, CallbackStatus.DELIVERY_UNKNOWN)
        self.assertEqual(result.error_type, 'TimeoutError')
        self.assertEqual(result.detail, 'transport timeout')
        self.message.click.assert_awaited_once()

    async def test_transport_disconnect_is_uncertain_delivery_without_retry(self) -> None:
        for error in (OSError("transport failed"), ConnectionResetError("connection lost")):
            with self.subTest(error=type(error).__name__):
                self.message.click.reset_mock()
                self.message.click.side_effect = error
                result = await self.executor.execute(self.message, 0, 0)
                self.assertEqual(result.status, CallbackStatus.DELIVERY_UNKNOWN)
                self.assertEqual(result.error_type, type(error).__name__)
                self.assertEqual(result.detail, str(error))
                self.message.click.assert_awaited_once()

    async def test_local_deadline_is_uncertain_and_cancels_rpc(self) -> None:
        cancelled = asyncio.Event()

        async def blocked_click(row: int, column: int) -> object:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return None

        self.message.click.side_effect = blocked_click
        executor = TelegramActionExecutor(timeout=0.01)
        result = await executor.execute(self.message, 0, 0)
        self.assertEqual(result.status, CallbackStatus.DELIVERY_UNKNOWN)
        self.assertEqual(result.error_type, 'TimeoutError')
        self.assertTrue(cancelled.is_set())
        self.message.click.assert_awaited_once()

    async def test_rpc_error_is_rejected_without_retry(self) -> None:
        self.message.click.side_effect = RPCError(None, 'BUTTON_DATA_INVALID', 400)
        result = await self.executor.execute(self.message, 0, 0)
        self.assertEqual(result.status, CallbackStatus.RPC_REJECTED)
        self.assertEqual(result.error_type, 'RPCError')
        self.assertIn('BUTTON_DATA_INVALID', result.detail or '')
        self.assertIsNone(result.flood_wait_seconds)
        self.message.click.assert_awaited_once()

    async def test_cancellation_propagates_and_cancels_rpc(self) -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked_click(row: int, column: int) -> object:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return None

        self.message.click.side_effect = blocked_click
        executing = asyncio.create_task(self.executor.execute(self.message, 1, 2))
        await asyncio.wait_for(entered.wait(), timeout=1)
        executing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await executing
        self.assertTrue(cancelled.is_set())
        self.message.click.assert_awaited_once_with(1, 2)

    async def test_cancellation_from_rpc_is_not_an_outcome(self) -> None:
        self.message.click.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.executor.execute(self.message, 0, 0)
        self.message.click.assert_awaited_once()

    async def test_unexpected_programming_error_propagates(self) -> None:
        self.message.click.side_effect = ValueError('invalid local action')
        with self.assertRaisesRegex(ValueError, 'invalid local action'):
            await self.executor.execute(self.message, 0, 0)
        self.message.click.assert_awaited_once()

    def test_timeout_must_be_positive_and_finite(self) -> None:
        for timeout in (0, -1, float('nan'), float('inf'), float('-inf')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                TelegramActionExecutor(timeout)

    async def test_invalid_coordinates_fail_before_rpc(self) -> None:
        for row, column in ((-1, 0), (0, -1), (True, 0), (0, 1.5)):
            with self.subTest(row=row, column=column), self.assertRaises(ValueError):
                await self.executor.execute(self.message, row, column)
        self.message.click.assert_not_awaited()

    def test_result_rejects_inconsistent_metadata(self) -> None:
        invalid = (
            lambda: CallbackResult(CallbackStatus.SENT, flood_wait_seconds=1),
            lambda: CallbackResult(CallbackStatus.FLOOD_WAIT, error_type="FloodWaitError"),
            lambda: CallbackResult(
                CallbackStatus.DELIVERY_UNKNOWN,
                flood_wait_seconds=1,
                error_type="TimeoutError",
            ),
            lambda: CallbackResult(CallbackStatus.RPC_REJECTED),
            lambda: CallbackResult(
                CallbackStatus.FLOOD_WAIT,
                flood_wait_seconds=-1,
                error_type="FloodWaitError",
            ),
        )
        for factory in invalid:
            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory()

    def test_result_is_immutable(self) -> None:
        result = CallbackResult(CallbackStatus.SENT)
        with self.assertRaises(FrozenInstanceError):
            result.status = CallbackStatus.RPC_REJECTED


if __name__ == '__main__':
    unittest.main()
