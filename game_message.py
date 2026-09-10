from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol


class GameButton(Protocol):
    @property
    def text(self) -> str: ...


class GameMessage(Protocol):
    """The Telegram surface consumed by the game engine and offline tests."""

    @property
    def id(self) -> int: ...

    @property
    def raw_text(self) -> str | None: ...

    @property
    def edit_date(self) -> datetime | None: ...

    @property
    def buttons(self) -> Sequence[Sequence[GameButton]] | None: ...

    async def click(self, row: int, column: int) -> object: ...
