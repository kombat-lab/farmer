from __future__ import annotations

import random
import re

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
