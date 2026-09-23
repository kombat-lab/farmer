from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from rich_messages import (
    notification_rich,
    rich_button,
    rich_button_row,
    send_rich_with_fallback,
)

logger = logging.getLogger("fog_farmer")


@dataclass(frozen=True, slots=True)
class NotificationAction:
    label: str
    callback_data: str


class NotificationStatus(Enum):
    SENT = "sent"
    RETRYABLE_FAILURE = "retryable_failure"


@dataclass(frozen=True, slots=True)
class NotificationDelivery:
    """A confirmed delivery or a failed attempt that its caller may retry later."""

    status: NotificationStatus
    retry_after_seconds: float | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, NotificationStatus):
            raise ValueError("status must be a NotificationStatus")
        if self.status is NotificationStatus.SENT:
            if self.retry_after_seconds is not None or self.error is not None:
                raise ValueError("A sent notification cannot have retry or error details")
            return
        if not isinstance(self.error, str) or not self.error.strip():
            raise ValueError("A failed notification must have a nonblank error")
        object.__setattr__(self, "error", self.error.strip())
        delay = self.retry_after_seconds
        if delay is not None:
            if isinstance(delay, bool) or not isinstance(delay, (int, float)):
                raise ValueError("retry_after_seconds must be a finite nonnegative number")
            try:
                valid = math.isfinite(delay) and delay >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ValueError("retry_after_seconds must be a finite nonnegative number")


def _failed_delivery(error: Exception) -> NotificationDelivery:
    detail = f"{type(error).__name__}: {error}"
    if isinstance(error, TelegramRetryAfter):
        logger.warning("Telegram просит повторить уведомление через %s сек.", error.retry_after)
        return NotificationDelivery(
            NotificationStatus.RETRYABLE_FAILURE,
            retry_after_seconds=error.retry_after,
            error=detail,
        )
    if isinstance(error, TelegramNetworkError):
        logger.warning("Уведомление не подтверждено из-за сетевой ошибки: %s", error)
    else:
        logger.error("Не удалось отправить уведомление", exc_info=error)
    return NotificationDelivery(NotificationStatus.RETRYABLE_FAILURE, error=detail)


def _fallback_message(
    title: str,
    *,
    rows: list[tuple[object, object]] | None,
    text: str | None,
) -> str:
    """Build safe HTML because the shared Bot defaults plain sends to HTML parse mode."""
    fallback = escape(title)
    if text:
        fallback += f"\n\n{escape(text)}"
    if rows:
        fallback += "\n\n" + "\n".join(
            f"{escape(str(name))}: {escape(str(value))}" for name, value in rows
        )
    return fallback


class Notifier:
    """Отправляет редкие уведомления; решение о повторе остаётся у вызывающего кода."""

    def __init__(self, bot: Bot, admin_id: int) -> None:
        self.bot = bot
        self.admin_id = admin_id

    async def send(self, text: str) -> NotificationDelivery:
        try:
            await self.bot.send_message(chat_id=self.admin_id, text=text)
        except Exception as error:
            return _failed_delivery(error)
        return NotificationDelivery(NotificationStatus.SENT)

    async def send_event(
        self,
        title: str,
        *,
        rows: list[tuple[object, object]] | None = None,
        text: str | None = None,
        silent: bool = False,
        action: NotificationAction | None = None,
    ) -> NotificationDelivery:
        html = notification_rich(title, rows=rows, text=text)
        markup = None
        if action is not None:
            html += rich_button_row(
                rich_button(action.label, action.callback_data, style="success")
            )
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=action.label, callback_data=action.callback_data, style="success"
                )
            ]])
        try:
            await send_rich_with_fallback(
                self.bot,
                chat_id=self.admin_id,
                html=html,
                fallback_text=_fallback_message(title, rows=rows, text=text),
                fallback_reply_markup=markup,
                disable_notification=silent,
                fallback_on_network_error=False,
            )
        except Exception as error:
            return _failed_delivery(error)
        return NotificationDelivery(NotificationStatus.SENT)

    async def card_drop(
        self,
        item: str,
        position: tuple[int, int] | None,
    ) -> NotificationDelivery:
        return await self.send_event(
            "🎉 Выпала карта",
            rows=[
                ("Предмет", item),
                ("Позиция", position or "неизвестна"),
            ],
        )
