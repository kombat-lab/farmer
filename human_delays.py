from __future__ import annotations

import random
import re
import time
from collections.abc import Callable

REMAINING_SECONDS_RE = re.compile(r"Осталось:\s*(\d+)\s*сек", re.IGNORECASE)
TURN_SAFETY_SECONDS = 6.0


def parse_remaining_seconds(text: str) -> int | None:
    match = REMAINING_SECONDS_RE.search(text or "")
    return int(match.group(1)) if match else None


class HumanDelayModel:
    """Produces bounded, non-uniform delays without changing action count."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.tempo = 1.0
        self.tempo_actions_remaining = 0
        self.moves_since_long_pause = 0

    def _advance_tempo(self) -> None:
        if self.tempo_actions_remaining <= 0:
            self.tempo = self.rng.triangular(0.9, 1.1, 1.0)
            self.tempo_actions_remaining = self.rng.randint(3, 8)
        self.tempo_actions_remaining -= 1

    def action_delay(
        self,
        minimum: float,
        maximum: float,
        *,
        urgent: bool = False,
        remaining_seconds: int | None = None,
    ) -> float:
        minimum = max(0.0, float(minimum))
        maximum = max(minimum, float(maximum))
        self._advance_tempo()

        effective_maximum = maximum
        if urgent:
            effective_maximum = min(maximum, minimum + max(1.0, (maximum - minimum) * 0.4))

        mode = minimum + (effective_maximum - minimum) * (0.35 if urgent else 0.42)
        delay = self.rng.triangular(minimum, effective_maximum, mode) * self.tempo
        delay = min(effective_maximum, max(minimum, delay))

        if remaining_seconds is not None:
            timer_cap = max(0.25, float(remaining_seconds) - TURN_SAFETY_SECONDS)
            delay = min(delay, timer_cap)

        return delay

    def should_take_long_pause(self, configured_chance: float) -> bool:
        """Clusters pauses after several moves instead of independent coin flips."""
        chance = min(1.0, max(0.0, float(configured_chance)))
        self.moves_since_long_pause += 1
        if chance <= 0.0 or self.moves_since_long_pause < 3:
            return False

        adjusted_chance = min(0.8, chance * (0.5 + self.moves_since_long_pause / 6.0))
        if self.rng.random() >= adjusted_chance:
            return False

        self.moves_since_long_pause = 0
        return True


class ActivityBreakPlanner:
    """Schedules sparse safe breaks without polling Telegram while idle."""

    def __init__(
        self,
        rng: random.Random | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rng = rng or random.Random()
        self.clock = clock
        self.next_move: int | None = None
        self.deadline: float | None = None
        self.break_pending = False

    def reset(self) -> None:
        self.next_move = None
        self.deadline = None
        self.break_pending = False

    def _arm(
        self,
        current_move: int,
        *,
        moves_min: int,
        moves_max: int,
        work_min: float,
        work_max: float,
    ) -> None:
        moves_min = max(1, int(moves_min))
        moves_max = max(moves_min, int(moves_max))
        work_min = max(1.0, float(work_min))
        work_max = max(work_min, float(work_max))
        self.next_move = current_move + self.rng.randint(moves_min, moves_max)
        self.deadline = self.clock() + self.rng.uniform(work_min, work_max)

    def is_due(
        self,
        current_move: int,
        *,
        moves_min: int,
        moves_max: int,
        work_min: float,
        work_max: float,
    ) -> bool:
        if self.break_pending:
            return False
        if self.next_move is None or self.deadline is None:
            self._arm(
                current_move,
                moves_min=moves_min,
                moves_max=moves_max,
                work_min=work_min,
                work_max=work_max,
            )
            return False
        if current_move < self.next_move and self.clock() < self.deadline:
            return False
        self.break_pending = True
        return True

    def duration(self, minimum: float, maximum: float) -> float:
        minimum = max(1.0, float(minimum))
        maximum = max(minimum, float(maximum))
        return self.rng.triangular(minimum, maximum, minimum + (maximum - minimum) * 0.4)

    def complete(
        self,
        current_move: int,
        *,
        moves_min: int,
        moves_max: int,
        work_min: float,
        work_max: float,
    ) -> None:
        self.break_pending = False
        self._arm(
            current_move,
            moves_min=moves_min,
            moves_max=moves_max,
            work_min=work_min,
            work_max=work_max,
        )
