from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol


class TelegramClientPort(Protocol):
    """Borrowed transport: the runtime cannot close the owning session."""

    @property
    def disconnected(self) -> asyncio.Future[object]: ...

    async def connect(self) -> object: ...

    async def is_user_authorized(self) -> bool: ...

    async def get_input_entity(self, peer: str) -> object: ...

    async def send_message(self, peer: object, text: str) -> object: ...

    def add_event_handler(
        self, callback: Callable[..., Awaitable[None]], event: object
    ) -> None: ...


class OwnedTelegramClient(TelegramClientPort, Protocol):
    """Exclusive session owner capability, reserved for the supervisor."""

    async def disconnect(self) -> object: ...
