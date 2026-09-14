from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol


class GameButton(Protocol):
    @property
    def text(self) -> str: ...


class ReadableGameMessage(Protocol):
    """Text and buttons available to pure game parsers and policies."""

    @property
    def raw_text(self) -> str | None: ...

    @property
    def buttons(self) -> Sequence[Sequence[GameButton]] | None: ...


class ClickableMessage(Protocol):
    """A raw callback capability, with no dependency on a transport library."""

    async def click(self, row: int, column: int) -> object: ...


class GameMessage(ReadableGameMessage, ClickableMessage, Protocol):
    """The Telegram surface consumed by the runtime and offline tests."""

    @property
    def id(self) -> int: ...

    @property
    def edit_date(self) -> datetime | None: ...
