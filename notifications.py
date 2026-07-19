from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.types import ReplyKeyboardMarkup

from rich_messages import notification_rich, send_rich_with_fallback

logger = logging.getLogger("fog_farmer")
KeyboardProvider = Callable[[], Awaitable[ReplyKeyboardMarkup]]


class Notifier:
    def __init__(self, bot: Bot, admin_id: int) -> None:
        self.bot = bot
        self.admin_id = admin_id
        self._keyboard_provider: Optional[KeyboardProvider] = None

    def set_keyboard_provider(self, provider: KeyboardProvider) -> None:
        self._keyboard_provider = provider

    async def _keyboard(self) -> ReplyKeyboardMarkup | None:
        if self._keyboard_provider is None:
            return None
        try:
            return await self._keyboard_provider()
        except Exception:
            logger.exception("Не удалось получить актуальную клавиатуру")
            return None

    async def send(
        self,
        text: str,
        *,
        reply_markup: ReplyKeyboardMarkup | None = None,
    ) -> None:
        if reply_markup is None:
            reply_markup = await self._keyboard()
        try:
            await self.bot.send_message(
                chat_id=self.admin_id,
                text=text,
                reply_markup=reply_markup,
            )
        except TelegramRetryAfter as error:
            logger.warning("Telegram просит повторить уведомление через %s сек.", error.retry_after)
        except TelegramNetworkError:
            logger.warning("Уведомление не отправлено из-за сетевой ошибки")
        except Exception:
            logger.exception("Не удалось отправить уведомление")

    async def send_event(
        self,
        title: str,
        *,
        rows: list[tuple[object, object]] | None = None,
        text: str | None = None,
        silent: bool = False,
    ) -> None:
        keyboard = await self._keyboard()
        fallback = title
        if text:
            fallback += f"\n\n{text}"
        if rows:
            fallback += "\n\n" + "\n".join(f"{name}: {value}" for name, value in rows)
        try:
            await send_rich_with_fallback(
                self.bot,
                chat_id=self.admin_id,
                html=notification_rich(title, rows=rows, text=text),
                fallback_text=fallback,
                reply_markup=keyboard,
                disable_notification=silent,
            )
        except Exception:
            logger.exception("Не удалось отправить Rich Message уведомление")

    async def card_drop(
        self,
        item: str,
        position: tuple[int, int] | None,
    ) -> None:
        await self.send_event(
            "🎉 Выпала карта",
            rows=[
                ("Предмет", item),
                ("Позиция", position or "неизвестна"),
            ],
        )
