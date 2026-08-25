from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class AdminOnlyMiddleware(BaseMiddleware):
    """Разрешает управление только владельцу в личном чате."""

    def __init__(self, admin_user_id: int) -> None:
        self.admin_user_id = admin_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            if (
                event.from_user is None
                or event.from_user.id != self.admin_user_id
                or event.chat.type != "private"
            ):
                return None
            return await handler(event, data)

        if not isinstance(event, CallbackQuery):
            return None
        if (
            event.from_user.id != self.admin_user_id
            or event.message is None
            or event.message.chat.type != "private"
        ):
            return None

        return await handler(event, data)
