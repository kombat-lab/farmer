from __future__ import annotations

import time

from models import BotState


class ProgressWatchdog:
    def __init__(self) -> None:
        self.last_progress_at = time.monotonic()
        self.recovery_attempts = 0
        self.reason = "запуск"

    def mark_progress(self, reason: str) -> None:
        self.last_progress_at = time.monotonic()
        self.recovery_attempts = 0
        self.reason = reason

    def elapsed(self) -> float:
        return time.monotonic() - self.last_progress_at

    def timeout_for_state(
        self,
        state: BotState,
        *,
        move_timeout: float,
        target_timeout: float,
        combat_timeout: float,
        general_timeout: float,
        recovery_timeout: float,
    ) -> float:
        if state is BotState.MOVING:
            return move_timeout
        if state is BotState.TARGET_SELECTION:
            return target_timeout
        if state is BotState.COMBAT:
            return combat_timeout
        if state is BotState.RECOVERY:
            return recovery_timeout
        return general_timeout

    def should_recover(
        self,
        state: BotState,
        **timeouts,
    ) -> bool:
        return self.elapsed() >= self.timeout_for_state(
            state,
            **timeouts,
        )

    def begin_recovery_attempt(self) -> int:
        self.recovery_attempts += 1
        self.last_progress_at = time.monotonic()
        return self.recovery_attempts
