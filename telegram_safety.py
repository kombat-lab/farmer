from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegram_buttons import get_button_texts

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class FloodWaitDecision:
    retry: bool
    server_wait_seconds: int
    pause_seconds: float


def decide_flood_wait(
    seconds: int,
    *,
    retries_used: int,
    short_wait_max: int,
    safety_buffer: float,
    max_retries: int,
) -> FloodWaitDecision:
    """Returns a bounded recovery decision for a Telegram FLOOD_WAIT."""
    server_wait = max(1, int(seconds))
    can_retry = server_wait <= short_wait_max and retries_used < max_retries
    return FloodWaitDecision(
        retry=can_retry,
        server_wait_seconds=server_wait,
        pause_seconds=server_wait + max(0.0, safety_buffer) if can_retry else 0.0,
    )


def message_state_key(message) -> tuple:
    """Semantic UI state, independent of a no-op Telegram edit timestamp."""
    return (
        message.id,
        message.raw_text or "",
        tuple(get_button_texts(message)),
    )


class StateRefreshGate:
    """Allows at most one state-refresh request per received semantic state."""

    def __init__(self) -> None:
        self._last_generation: int | None = None

    def reserve(self, generation: int) -> bool:
        if generation == self._last_generation:
            return False
        self._last_generation = generation
        return True


class RollingAttemptGuard:
    """Bounds exceptional recovery attempts independently of normal progress."""

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: float,
        clock: Clock = time.monotonic,
    ) -> None:
        if max_attempts < 1 or window_seconds <= 0:
            raise ValueError("Некорректные параметры ограничителя попыток")
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.clock = clock
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        now = self.clock()
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_attempts:
            return False
        self._timestamps.append(now)
        return True


class TelegramActionTelemetry:
    """Counts outgoing user actions locally without making Telegram requests."""

    def __init__(self, *, clock: Clock = time.monotonic) -> None:
        self.clock = clock
        self.total = 0
        self.by_kind: Counter[str] = Counter()
        self._timestamps: deque[float] = deque()

    def record(self, kind: str) -> dict[str, object]:
        now = self.clock()
        self.total += 1
        self.by_kind[kind] += 1
        self._timestamps.append(now)
        cutoff = now - 600.0
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        return self.snapshot(now=now)

    def snapshot(self, *, now: float | None = None) -> dict[str, object]:
        current = self.clock() if now is None else now
        return {
            "total": self.total,
            "last_minute": sum(stamp > current - 60.0 for stamp in self._timestamps),
            "last_ten_minutes": len(self._timestamps),
            "by_kind": dict(self.by_kind),
        }


class TelegramActionLimiter:
    """Serializes user actions and enforces a conservative sliding-window cap."""

    def __init__(
        self,
        *,
        min_interval: float,
        max_actions: int,
        window_seconds: float,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if min_interval < 0 or max_actions < 1 or window_seconds <= 0:
            raise ValueError("Некорректные параметры ограничителя Telegram-действий")
        self.min_interval = min_interval
        self.max_actions = max_actions
        self.window_seconds = window_seconds
        self.clock = clock
        self.sleep = sleep
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._pending = 0

    @property
    def pending(self) -> bool:
        return self._pending > 0

    async def acquire(self) -> float:
        """Waits for a safe slot and returns the total imposed delay."""
        self._pending += 1
        try:
            waited = 0.0
            async with self._lock:
                while True:
                    now = self.clock()
                    cutoff = now - self.window_seconds
                    while self._timestamps and self._timestamps[0] <= cutoff:
                        self._timestamps.popleft()

                    delay = 0.0
                    if self._timestamps:
                        delay = max(delay, self._timestamps[-1] + self.min_interval - now)
                    if len(self._timestamps) >= self.max_actions:
                        delay = max(delay, self._timestamps[0] + self.window_seconds - now)

                    if delay <= 0:
                        self._timestamps.append(now)
                        return waited

                    await self.sleep(delay)
                    waited += delay
        finally:
            self._pending -= 1
