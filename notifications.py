from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from rich_messages import notification_rich, send_rich_with_fallback

logger = logging.getLogger("fog_farmer")


class Notifier:
    """Отправляет редкие самостоятельные уведомления без постоянной клавиатуры."""

    def __init__(self, bot: Bot, admin_id: int) -> None:
        self.bot = bot
        self.admin_id = admin_id

    async def send(self, text: str) -> None:
        try:
            await self.bot.send_message(chat_id=self.admin_id, text=text)
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
                disable_notification=silent,
            )
        except Exception:
            logger.exception("Не удалось отправить RichMessage-уведомление")

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
