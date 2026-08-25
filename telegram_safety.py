from __future__ import annotations

import asyncio
import re
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegram_buttons import get_button_texts

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


_COUNTDOWN_RE = re.compile(r"(?im)^\s*⏳\s*Осталось:\s*\d+\s*сек\.?\s*$")


def semantic_message_text(text: str) -> str:
    """Removes volatile countdowns without hiding meaningful game changes."""
    return _COUNTDOWN_RE.sub("", text).strip()


def message_state_key(message) -> tuple:
    """Semantic UI state, independent of a no-op Telegram edit timestamp."""
    return (
        message.id,
        semantic_message_text(message.raw_text or ""),
        tuple(get_button_texts(message)),
    )


@dataclass(frozen=True, slots=True)
class PacingUpdate:
    previous_factor: float
    factor: float
    pressure: str

    @property
    def changed(self) -> bool:
        return abs(self.factor - self.previous_factor) >= 0.001


class AdaptivePacingController:
    """Slows down after real Telegram incidents and recovers gradually."""

    def __init__(
        self,
        *,
        minimum_factor: float,
        maximum_factor: float,
        adjust_interval: float,
        acceleration_lock: float,
        clock: Clock = time.monotonic,
    ) -> None:
        if not 0 < minimum_factor <= 1.0 <= maximum_factor:
            raise ValueError("Некорректные границы автоматического темпа")
        self.minimum_factor = minimum_factor
        self.maximum_factor = maximum_factor
        self.adjust_interval = max(1.0, adjust_interval)
        self.acceleration_lock = max(0.0, acceleration_lock)
        self.clock = clock
        self.factor = 1.0
        self.last_adjusted_at = self.clock()
        self.acceleration_locked_until = 0.0
        self.pressure = "normal"

    def observe(self) -> PacingUpdate:
        """Gradually restores the normal pace when Telegram reports no errors."""
        now = self.clock()
        previous = self.factor
        if now < self.acceleration_locked_until:
            self.pressure = "recovery"
            return PacingUpdate(previous, self.factor, self.pressure)
        self.pressure = "normal"
        if now - self.last_adjusted_at < self.adjust_interval:
            return PacingUpdate(previous, self.factor, self.pressure)

        self.factor = max(self.minimum_factor, self.factor - 0.02)
        self.last_adjusted_at = now
        return PacingUpdate(previous, self.factor, self.pressure)

    def register_incident(self, *, severe: bool = False) -> PacingUpdate:
        now = self.clock()
        previous = self.factor
        floor = 1.35 if severe else 1.20
        self.factor = min(self.maximum_factor, max(floor, self.factor + 0.15))
        self.pressure = "cooldown"
        self.last_adjusted_at = now
        self.acceleration_locked_until = max(
            self.acceleration_locked_until,
            now + self.acceleration_lock,
        )
        return PacingUpdate(previous, self.factor, self.pressure)


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
    """Serializes outbound actions and smooths only immediate bursts."""

    def __init__(
        self,
        *,
        min_interval: float,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if min_interval < 0:
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
