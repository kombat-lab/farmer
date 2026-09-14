from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from game_input import InboundEvent
from game_message import ClickableMessage
from message_snapshot import ButtonSnapshot, MessageSnapshot


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Immutable parser input paired with the original, separate callback capability."""

    event: InboundEvent
    rpc: ClickableMessage = field(repr=False, compare=False)

    @property
    def snapshot(self) -> MessageSnapshot:
        return self.event.snapshot

    @property
    def id(self) -> int:
        return self.snapshot.id

    @property
    def raw_text(self) -> str:
        return self.snapshot.raw_text

    @property
    def buttons(self) -> tuple[tuple[ButtonSnapshot, ...], ...]:
        return self.snapshot.buttons

    @property
    def edit_date(self) -> datetime | None:
        return self.snapshot.edit_date

    async def click(self, row: int, column: int) -> object:
        return await self.rpc.click(row, column)
