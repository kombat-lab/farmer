from __future__ import annotations

import asyncio
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from notifications import NotificationAction, NotificationDelivery, NotificationStatus, Notifier
from rich_messages import send_rich_with_fallback


def retry_after(seconds: int = 19) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SendMessage(chat_id=1, text="notification"),
        message="Too many requests",
        retry_after=seconds,
    )


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(
        method=SendMessage(chat_id=1, text="notification"), message="Connection lost"
    )


class NotificationDeliveryTests(unittest.TestCase):
    def test_frozen_validated_delivery(self) -> None:
        sent = NotificationDelivery(NotificationStatus.SENT)
        self.assertIsNone(sent.error)
        with self.assertRaises(FrozenInstanceError):
            sent.error = "changed"
        failed = NotificationDelivery(
            NotificationStatus.RETRYABLE_FAILURE, retry_after_seconds=0, error=" failure "
        )
        self.assertEqual(failed.error, "failure")
        self.assertEqual(failed.retry_after_seconds, 0)

    def test_invalid_delivery_combinations_are_rejected(self) -> None:
        cases = (
            {"status": "sent"},
            {"status": None},
            {"status": NotificationStatus.SENT, "error": "failure"},
            {"status": NotificationStatus.SENT, "retry_after_seconds": 0},
            {"status": NotificationStatus.RETRYABLE_FAILURE},
            {"status": NotificationStatus.RETRYABLE_FAILURE, "error": " "},
            {"status": NotificationStatus.RETRYABLE_FAILURE, "error": 2},
        )
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                NotificationDelivery(**fields)
        for delay in (-1, float("nan"), float("inf"), True, "5", 10**1000):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                NotificationDelivery(
                    NotificationStatus.RETRYABLE_FAILURE, retry_after_seconds=delay, error="bad"
                )


class NotifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock(spec=Bot)
        self.bot.send_message = AsyncMock()
        self.bot.send_rich_message = AsyncMock()
        self.notifier = Notifier(self.bot, 42)

    async def test_plain_send_returns_confirmed_delivery(self) -> None:
        result = await self.notifier.send("hello")
        self.assertEqual(result, NotificationDelivery(NotificationStatus.SENT))
        self.bot.send_message.assert_awaited_once_with(chat_id=42, text="hello")

    async def test_rich_event_returns_confirmed_delivery(self) -> None:
        result = await self.notifier.send_event("title", text="body", rows=[("HP", 1)])
        self.assertIs(result.status, NotificationStatus.SENT)
        self.bot.send_rich_message.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_action_is_delivered_in_rich_and_fallback_formats(self) -> None:
        action = NotificationAction("⏭ Пропустить передышку", "rest:skip:" + "a" * 32)
        result = await self.notifier.send_event("Передышка", action=action)
        self.assertIs(result.status, NotificationStatus.SENT)
        html = self.bot.send_rich_message.await_args.kwargs["rich_message"].html
        self.assertIn(f'data="{action.callback_data}"', html)
        self.assertIn(action.label, html)

        self.bot.send_rich_message.side_effect = TelegramBadRequest(
            method=SendMessage(chat_id=1, text="n"), message="unsupported format"
        )
        with self.assertLogs("fog_farmer", level="WARNING"):
            result = await self.notifier.send_event("Передышка", action=action)
        self.assertIs(result.status, NotificationStatus.SENT)
        markup = self.bot.send_message.await_args.kwargs["reply_markup"]
        button = markup.inline_keyboard[0][0]
        self.assertEqual(button.text, action.label)
        self.assertEqual(button.callback_data, action.callback_data)
        self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)

    async def test_card_drop_propagates_typed_result_and_position(self) -> None:
        failed = NotificationDelivery(
            NotificationStatus.RETRYABLE_FAILURE, retry_after_seconds=19, error="retry"
        )
        self.notifier.send_event = AsyncMock(return_value=failed)
        self.assertIs(await self.notifier.card_drop("Card", (2, 3)), failed)
        self.notifier.send_event.assert_awaited_once_with(
            "🎉 Выпала карта", rows=[("Предмет", "Card"), ("Позиция", (2, 3))]
        )

    async def test_retry_after_is_preserved_for_plain_and_rich_delivery(self) -> None:
        for operation, method in (
            (self.notifier.send, self.bot.send_message),
            (self.notifier.send_event, self.bot.send_rich_message),
        ):
            with self.subTest(operation=operation):
                method.side_effect = retry_after(19)
                with self.assertLogs("fog_farmer", level="WARNING"):
                    result = await operation("notice")
                self.assertIs(result.status, NotificationStatus.RETRYABLE_FAILURE)
                self.assertEqual(result.retry_after_seconds, 19)
                self.assertIn("TelegramRetryAfter", result.error)
                method.side_effect = None

    async def test_network_failure_is_retryable_without_a_hidden_fallback_send(self) -> None:
        self.bot.send_rich_message.side_effect = network_error()
        with self.assertLogs("fog_farmer", level="WARNING"):
            result = await self.notifier.card_drop("Card", None)
        self.assertIs(result.status, NotificationStatus.RETRYABLE_FAILURE)
        self.assertIsNone(result.retry_after_seconds)
        self.assertIn("TelegramNetworkError", result.error)
        self.bot.send_message.assert_not_awaited()
        self.bot.send_rich_message.assert_awaited_once()

    async def test_plain_network_failure_is_retryable(self) -> None:
        self.bot.send_message.side_effect = network_error()
        with self.assertLogs("fog_farmer", level="WARNING"):
            result = await self.notifier.send("notice")
        self.assertIs(result.status, NotificationStatus.RETRYABLE_FAILURE)
        self.assertIn("TelegramNetworkError", result.error)

    async def test_shared_rich_helper_preserves_existing_default_network_fallback(self) -> None:
        self.bot.send_rich_message.side_effect = network_error()
        with self.assertLogs("fog_farmer", level="WARNING"):
            await send_rich_with_fallback(
                self.bot, chat_id=42, html="<h2>notice</h2>", fallback_text="notice"
            )
        self.bot.send_rich_message.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()

    async def test_rejected_rich_format_can_use_one_confirmed_fallback(self) -> None:
        self.bot.send_rich_message.side_effect = TelegramBadRequest(
            method=SendMessage(chat_id=1, text="n"), message="unsupported format"
        )
        with self.assertLogs("fog_farmer", level="WARNING"):
            result = await self.notifier.send_event("notice")
        self.assertIs(result.status, NotificationStatus.SENT)
        self.bot.send_message.assert_awaited_once()

    async def test_other_failure_is_logged_and_retryable(self) -> None:
        for operation, method in (
            (self.notifier.send, self.bot.send_message),
            (self.notifier.send_event, self.bot.send_rich_message),
        ):
            with self.subTest(operation=operation):
                method.side_effect = RuntimeError("offline failure")
                with self.assertLogs("fog_farmer", level="ERROR"):
                    result = await operation("notice")
                self.assertIs(result.status, NotificationStatus.RETRYABLE_FAILURE)
                self.assertIn("RuntimeError", result.error)
                method.side_effect = None

    async def test_cancellation_propagates_for_every_delivery_entrypoint(self) -> None:
        self.bot.send_message.side_effect = asyncio.CancelledError
        self.bot.send_rich_message.side_effect = asyncio.CancelledError
        for operation in (self.notifier.send, self.notifier.send_event):
            with self.subTest(operation=operation), self.assertRaises(asyncio.CancelledError):
                await operation("notice")
        with self.assertRaises(asyncio.CancelledError):
            await self.notifier.card_drop("Card", None)

    async def test_rich_fallback_escapes_dynamic_html_values(self) -> None:
        self.bot.send_rich_message.side_effect = TelegramBadRequest(
            method=SendMessage(chat_id=1, text="n"),
            message="unsupported format",
        )

        with self.assertLogs("fog_farmer", level="WARNING"):
            result = await self.notifier.send_event(
                "<notice>",
                text="A & B",
                rows=[("<item>", "x > y")],
            )

        self.assertIs(result.status, NotificationStatus.SENT)
        self.bot.send_message.assert_awaited_once_with(
            chat_id=42,
            text="&lt;notice&gt;\n\nA &amp; B\n\n&lt;item&gt;: x &gt; y",
            reply_markup=None,
            disable_notification=False,
        )


if __name__ == "__main__":
    unittest.main()
