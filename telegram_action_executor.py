from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from enum import Enum, auto

from telethon.errors import BotResponseTimeoutError, FloodWaitError, RPCError

from game_message import ClickableMessage


class CallbackStatus(Enum):
    SENT = auto()
    FLOOD_WAIT = auto()
    DELIVERY_UNKNOWN = auto()
    RPC_REJECTED = auto()


@dataclass(frozen=True, slots=True)
class CallbackResult:
    status: CallbackStatus
    flood_wait_seconds: int | None = None
    error_type: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, CallbackStatus):
            raise ValueError("Callback result requires CallbackStatus")
        if self.flood_wait_seconds is not None and (
            type(self.flood_wait_seconds) is not int or self.flood_wait_seconds < 0
        ):
            raise ValueError("Flood wait must be a nonnegative integer or None")
        for value in (self.error_type, self.detail):
            if value is not None and not isinstance(value, str):
                raise ValueError("Callback error metadata must be strings or None")
        if self.status is CallbackStatus.SENT:
            if any(
                value is not None
                for value in (self.flood_wait_seconds, self.error_type, self.detail)
            ):
                raise ValueError("Successful callback cannot contain error metadata")
            return
        if not self.error_type:
            raise ValueError("Failed callback requires an error type")
        if self.status is CallbackStatus.FLOOD_WAIT:
            if self.flood_wait_seconds is None:
                raise ValueError("Flood wait result requires its server duration")
        elif self.flood_wait_seconds is not None:
            raise ValueError("Only flood wait results may contain a server duration")


class TelegramActionExecutor:
    """Perform one callback RPC; policy, persistence, and retries belong to the caller."""

    def __init__(self, timeout: float) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Callback timeout must be positive and finite")
        self._timeout = timeout

    async def execute(
        self,
        message: ClickableMessage,
        row: int,
        column: int,
    ) -> CallbackResult:
        if type(row) is not int or row < 0 or type(column) is not int or column < 0:
            raise ValueError("Callback coordinates must be nonnegative integers")
        try:
            await asyncio.wait_for(message.click(row, column), timeout=self._timeout)
        except FloodWaitError as error:
            return CallbackResult(
                CallbackStatus.FLOOD_WAIT,
                flood_wait_seconds=error.seconds,
                error_type=type(error).__name__,
                detail=str(error),
            )
        except (BotResponseTimeoutError, OSError) as error:
            return CallbackResult(
                CallbackStatus.DELIVERY_UNKNOWN,
                error_type=type(error).__name__,
                detail=str(error),
            )
        except RPCError as error:
            return CallbackResult(
                CallbackStatus.RPC_REJECTED,
                error_type=type(error).__name__,
                detail=str(error),
            )
        return CallbackResult(CallbackStatus.SENT)
