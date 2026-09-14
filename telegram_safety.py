from __future__ import annotations

import asyncio
import math
import re
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from typing import NamedTuple, TypedDict

from game_message import GameMessage

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


_COUNTDOWN_RE = re.compile(r"(?im)^\s*⏳\s*Осталось:\s*\d+\s*сек\.?\s*$")


def semantic_message_text(text: str) -> str:
    """Removes volatile countdowns without hiding meaningful game changes."""
    return _COUNTDOWN_RE.sub("", text).strip()


class MessageStateKey(NamedTuple):
    message_id: int
    text: str
    buttons: tuple[tuple[str, ...], ...]


class MessageFactKey(NamedTuple):
    message_id: int
    text: str


def message_fact_key(message: GameMessage) -> MessageFactKey:
    """UI-only keyboard changes are not new damage, healing, or movement facts."""
    return MessageFactKey(message.id, semantic_message_text(message.raw_text or ""))


def message_state_key(message: GameMessage) -> MessageStateKey:
    """Ignore countdown edits, but preserve the layout of actionable buttons."""
    return MessageStateKey(
        message.id,
        semantic_message_text(message.raw_text or ""),
        tuple(tuple(button.text for button in row) for row in (message.buttons or ())),
    )


class StateRefreshGate:
    """One RPC in flight; retry unanswered state requests after a deadline.

    Every reservation must be finished, including cancelled or failed requests.
    New inbound generations bypass the deadline, never an in-flight request.
    """

    def __init__(self, *, retry_after: float = 30.0, clock: Clock = time.monotonic) -> None:
        if not math.isfinite(retry_after) or retry_after <= 0:
            raise ValueError("Refresh retry interval must be positive and finite")
        self._clock = clock
        self._retry_after = retry_after
        self._last_generation: int | None = None
        self._retry_at = 0.0
        self._in_flight = False

    def reserve(self, generation: int, *, force: bool = False) -> bool:
        now = self._clock()
        if self._in_flight:
            return False
        if not force and generation == self._last_generation and now < self._retry_at:
            return False
        self._last_generation = generation
        self._in_flight = True
        return True

    def finish(self, *, sent: bool) -> None:
        if not self._in_flight:
            raise RuntimeError("No state refresh reservation to finish")
        self._in_flight = False
        self._retry_at = self._clock() + self._retry_after if sent else 0.0


class TelegramActivitySnapshot(TypedDict):
    total: int
    last_minute: int
    last_ten_minutes: int
    by_kind: dict[str, int]


class RollingAttemptGuard:
    """Bounds exceptional recovery attempts independently of normal progress."""

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: float,
        clock: Clock = time.monotonic,
    ) -> None:
        if max_attempts < 1 or not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("Некорректные параметры ограничителя попыток")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.clock = clock
        self._timestamps: deque[float] = deque()

    def can_attempt(self) -> bool:
        """Check capacity without charging a request that may still be deferred."""
        cutoff = self.clock() - self.window_seconds
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        return len(self._timestamps) < self.max_attempts

    def allow(self) -> bool:
        if not self.can_attempt():
            return False
        self._timestamps.append(self.clock())
        return True


class TelegramActionTelemetry:
    """Counts outgoing user actions locally without making Telegram requests."""

    def __init__(self, *, clock: Clock = time.monotonic) -> None:
        self.clock = clock
        self.total = 0
        self.by_kind: Counter[str] = Counter()
        self._timestamps: deque[float] = deque()

    def record(self, kind: str) -> TelegramActivitySnapshot:
        now = self.clock()
        self.total += 1
        self.by_kind[kind] += 1
        self._timestamps.append(now)
        return self.snapshot(now=now)

    def snapshot(self, *, now: float | None = None) -> TelegramActivitySnapshot:
        current = self.clock() if now is None else now
        cutoff = current - 600.0
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        return {
            "total": self.total,
            "last_minute": sum(stamp > current - 60.0 for stamp in self._timestamps),
            "last_ten_minutes": len(self._timestamps),
            "by_kind": dict(self.by_kind),
        }


class TelegramActionLimiter:
    """Serializes outbound actions and smooths only immediate bursts."""

    def __init__(
        self,
        *,
        min_interval: float,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError("Некорректные параметры ограничителя Telegram-действий")
        self.min_interval = min_interval
        self.clock = clock
        self.sleep = sleep
        self._last_action_at: float | None = None
        self._lock = asyncio.Lock()
        self._pending = 0

    @property
    def pending(self) -> bool:
        return self._pending > 0

    def _required_delay(self, now: float) -> float:
        if self._last_action_at is None:
            return 0.0
        return max(0.0, self._last_action_at + self.min_interval - now)

    async def acquire(self) -> float:
        """Waits for a safe slot and returns the total imposed delay."""
        self._pending += 1
        try:
            waited = 0.0
            async with self._lock:
                while True:
                    now = self.clock()
                    delay = self._required_delay(now)

                    if delay <= 0:
                        self._last_action_at = now
                        return waited

                    await self.sleep(delay)
                    waited += delay
        finally:
            self._pending -= 1
