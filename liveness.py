from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto


class LivenessPhase(Enum):
    GENERAL = auto()
    DISCOVERY_ACTION = auto()
    TARGET_SELECTION = auto()
    COMBAT = auto()
    RECOVERY = auto()


def _finite_nonnegative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite nonnegative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return number


def _positive_timeout(value: object, name: str) -> float:
    timeout = _finite_nonnegative(value, name)
    if timeout == 0:
        raise ValueError(f"{name} must be positive and finite")
    return timeout


@dataclass(frozen=True, slots=True)
class LivenessPolicy:
    general_timeout: float
    discovery_timeout: float
    target_timeout: float
    combat_timeout: float
    recovery_timeout: float

    def __post_init__(self) -> None:
        for name in (
            "general_timeout",
            "discovery_timeout",
            "target_timeout",
            "combat_timeout",
            "recovery_timeout",
        ):
            object.__setattr__(self, name, _positive_timeout(getattr(self, name), name))

    def timeout_for(self, phase: LivenessPhase) -> float:
        if not isinstance(phase, LivenessPhase):
            raise ValueError("phase must be LivenessPhase")
        if phase is LivenessPhase.DISCOVERY_ACTION:
            return self.discovery_timeout
        if phase is LivenessPhase.TARGET_SELECTION:
            return self.target_timeout
        if phase is LivenessPhase.COMBAT:
            return self.combat_timeout
        if phase is LivenessPhase.RECOVERY:
            return self.recovery_timeout
        return self.general_timeout


class ProgressMonitor:
    """Tracks progress without knowing game states or recovery actions."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._clock = clock
        self._last_progress_at = self._read_clock()
        self._generation = 0
        self._recovery_attempts = 0
        self._reason = "запуск"

    def _read_clock(self) -> float:
        return _finite_nonnegative(self._clock(), "Monotonic clock value")

    @property
    def last_progress_at(self) -> float:
        return self._last_progress_at

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def recovery_attempts(self) -> int:
        return self._recovery_attempts

    @property
    def reason(self) -> str:
        return self._reason

    def mark_progress(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Progress reason must be a nonempty string")
        self._generation += 1
        self._last_progress_at = self._read_clock()
        self._recovery_attempts = 0
        self._reason = reason.strip()

    def elapsed(self) -> float:
        elapsed = self._read_clock() - self._last_progress_at
        if elapsed < 0:
            raise RuntimeError("Monotonic clock moved backwards")
        return elapsed

    def should_recover(self, phase: LivenessPhase, policy: LivenessPolicy) -> bool:
        if not isinstance(policy, LivenessPolicy):
            raise ValueError("policy must be LivenessPolicy")
        return self.elapsed() >= policy.timeout_for(phase)

    def begin_recovery_attempt(self) -> int:
        self._recovery_attempts += 1
        self._last_progress_at = self._read_clock()
        return self._recovery_attempts

    def restore(
        self,
        *,
        last_progress_at: float | None = None,
        recovery_attempts: int | None = None,
        reason: str | None = None,
    ) -> None:
        """Explicitly restore validated state without bypassing generation tracking."""
        if recovery_attempts is not None and (
            type(recovery_attempts) is not int or recovery_attempts < 0
        ):
            raise ValueError("Recovery attempts must be a nonnegative integer")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError("Progress reason must be a nonempty string")
        if last_progress_at is not None:
            self._last_progress_at = _finite_nonnegative(
                last_progress_at, "Last progress timestamp"
            )
        if recovery_attempts is not None:
            self._recovery_attempts = recovery_attempts
        if reason is not None:
            self._reason = reason.strip()
        self._generation += 1